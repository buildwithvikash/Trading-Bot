"""Daily self-optimizer for the auto-trade strategies (paper/demo wallets).

Once a day it reviews each saved strategy's recent closed trades and, when
the evidence is strong enough, applies ONE small bounded change per
strategy, then records what it found, what it changed, why, and what it
expects to happen (tables optimizer_runs / optimizer_actions).

Deliberately conservative — it only touches a strategy's fixed take-profit
RR (`rr`, or a custom rule's take_profit.rr) and its risk-per-trade %, or
pauses a strategy that is clearly losing. It never rewrites entry logic.
Only trades closed since that strategy's last optimizer change are counted,
so one change is judged on its own results before another is stacked on it.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from gold_bot.strategies import STRATEGY_REGISTRY
from webapp import db
from webapp.autotrade import ACTIVITY, AUTOTRADERS

LOOKBACK_DAYS = 30
MIN_TRADES = 10          # below this a strategy is left alone
PAUSE_MIN_TRADES = 20
RR_STEP, RR_MIN, RR_MAX = 0.25, 1.0, 3.0
RISK_CUT, RISK_RAISE = 0.75, 1.2
RISK_FLOOR, RISK_CAP = 0.25, 1.0   # percent of balance per trade
RUN_HOUR_UTC = int(os.environ.get("OPTIMIZER_HOUR_UTC", "22"))  # after the gold daily break starts


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_enabled() -> bool:
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT enabled FROM optimizer_settings WHERE id = 1").fetchone()
        return True if row is None else bool(row["enabled"])
    finally:
        conn.close()


def set_enabled(value: bool) -> None:
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO optimizer_settings (id, enabled) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET enabled = excluded.enabled",
            (1 if value else 0,),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------- #
def _get_rr(strategy_id: str, params: dict) -> tuple[float, str] | None:
    """(current rr, param path) for the one target-RR knob we know how to
    tune safely, or None if this strategy has no such fixed knob."""
    if strategy_id == "custom_rule":
        tp = (params.get("spec") or {}).get("take_profit") or {}
        if tp.get("type") == "rr" and tp.get("rr") is not None:
            return float(tp["rr"]), "spec.take_profit.rr"
        return None
    cls = STRATEGY_REGISTRY.get(strategy_id)
    if cls is None or not dataclasses.is_dataclass(cls):
        return None
    for f in dataclasses.fields(cls):
        if f.name == "rr":
            cur = params.get("rr", f.default)
            return (float(cur), "rr") if cur is not None and cur is not dataclasses.MISSING else None
    return None


def _with_rr(params: dict, path: str, value: float) -> dict:
    out = copy.deepcopy(params)
    if path == "spec.take_profit.rr":
        out["spec"]["take_profit"]["rr"] = value
    else:
        out[path] = value
    return out


def _metrics(rows: list) -> dict:
    n = len(rows)
    pnl = [r["net_pnl"] for r in rows]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p < 0]
    rs = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]
    gross_loss = -sum(losses)
    return {
        "trades": n,
        "wins": len(wins),
        "win_rate": (len(wins) / n) if n else None,
        "profit_factor": (sum(wins) / gross_loss) if gross_loss > 0 else (None if not wins else 99.0),
        "expectancy_r": (sum(rs) / len(rs)) if rs else None,
        "net_pnl": sum(pnl),
    }


def _decide(strategy_id: str, params: dict, risk_pct: float, m: dict) -> dict:
    """Pure decision step: metrics in, one action (or none) out."""
    n, wr, pf, ex = m["trades"], m["win_rate"], m["profit_factor"], m["expectancy_r"]
    if n < MIN_TRADES or wr is None or ex is None:
        return {"action": "none", "finding": f"Only {n} closed trade(s) since the last review/change — need {MIN_TRADES} before judging.",
                "reason": "Too few trades for the result to mean anything; changing settings now would just be reacting to noise.",
                "expected_effect": "No change."}

    pf_txt = "no losing trades" if pf is None else f"{pf:.2f}"
    base = (f"{n} trades, win rate {wr:.0%}, profit factor {pf_txt}, "
            f"avg {ex:+.2f}R per trade, net ${m['net_pnl']:+.2f}.")
    rr = _get_rr(strategy_id, params)
    pf_v = pf if pf is not None else 99.0

    if n >= PAUSE_MIN_TRADES and pf_v < 0.5 and ex < -0.3:
        return {"action": "pause", "finding": base + " Clearly losing.",
                "reason": "Expectancy is strongly negative over a meaningful sample; continuing to trade it would just bleed the wallet.",
                "expected_effect": "Strategy stops opening new trades (existing positions still close on their own SL/TP). Re-start it from the Trade tab after reviewing/adjusting."}

    if ex < 0 and pf_v < 0.9:
        if wr <= 0.35 and rr and rr[0] - RR_STEP >= RR_MIN:
            new = round(rr[0] - RR_STEP, 2)
            return {"action": "adjust_rr", "param": rr[1], "old": rr[0], "new": new, "finding": base + " Losing, with a low win rate.",
                    "reason": f"Few trades reach the {rr[0]}R target. A closer target should be hit more often.",
                    "expected_effect": f"Win rate should rise and each win gets smaller (target {rr[0]}R -> {new}R); worthwhile only if the extra wins outweigh the smaller payout."}
        new = round(max(RISK_FLOOR, risk_pct * RISK_CUT), 3)
        if new < risk_pct:
            return {"action": "adjust_risk", "param": "risk_pct", "old": risk_pct, "new": new, "finding": base + " Losing.",
                    "reason": "Negative expectancy — shrink exposure while it proves itself.",
                    "expected_effect": f"Each trade risks {risk_pct}% -> {new}% of the wallet, so losses (and any gains) are about {round((1 - RISK_CUT) * 100)}% smaller until it turns profitable."}
        return {"action": "none", "finding": base + " Losing, already at the minimum risk.",
                "reason": "Risk is already at its floor and there is no safe target tweak available.",
                "expected_effect": "No change. If this persists it will be paused once it has 20+ trades."}

    if ex > 0.15 and pf_v > 1.3:
        if wr >= 0.6 and rr and rr[0] + RR_STEP <= RR_MAX:
            new = round(rr[0] + RR_STEP, 2)
            return {"action": "adjust_rr", "param": rr[1], "old": rr[0], "new": new, "finding": base + " Profitable with a high win rate.",
                    "reason": "It wins most of the time, so there is room to let winners run further.",
                    "expected_effect": f"Target {rr[0]}R -> {new}R: each winning trade pays more, win rate may dip slightly."}
        new = round(min(RISK_CAP, risk_pct * RISK_RAISE), 3)
        if new > risk_pct:
            return {"action": "adjust_risk", "param": "risk_pct", "old": risk_pct, "new": new, "finding": base + " Profitable.",
                    "reason": "Positive expectancy with a healthy profit factor — scale up modestly.",
                    "expected_effect": f"Risk per trade {risk_pct}% -> {new}%: profits and losses both about {round((RISK_RAISE - 1) * 100)}% larger."}

    return {"action": "none", "finding": base, "reason": "Results are within a normal range — no evidence-backed change to make.",
            "expected_effect": "No change."}


# ---------------------------------------------------------------------- #
def _apply(conn, wallet_key: str, strategy_id: str, params: dict, risk_pct, decision: dict) -> None:
    action = decision["action"]
    if action == "pause":
        if AUTOTRADERS.runners.get(wallet_key) is not None:
            AUTOTRADERS.stop(wallet_key)
        conn.execute("DELETE FROM autotrade_saved WHERE wallet_key = ?", (wallet_key,))
        return
    new_params, new_risk = params, risk_pct
    if action == "adjust_rr":
        new_params = _with_rr(params, decision["param"], decision["new"])
    elif action == "adjust_risk":
        new_risk = decision["new"]
    conn.execute(
        "UPDATE autotrade_saved SET params_json = ?, risk_pct = ?, updated_at = ? WHERE wallet_key = ?",
        (json.dumps(new_params), new_risk, _now().isoformat(), wallet_key),
    )
    runner = AUTOTRADERS.runners.get(wallet_key)
    if runner is not None and runner.enabled:
        AUTOTRADERS.stop(wallet_key)
        AUTOTRADERS.start(strategy_id, new_params, risk_pct=new_risk)


def run_review(trigger: str = "scheduled") -> int:
    conn = db.get_conn()
    try:
        default_risk = conn.execute("SELECT risk_per_trade_pct FROM risk_settings WHERE id = 1").fetchone()[0]
        saved = [dict(r) for r in conn.execute("SELECT * FROM autotrade_saved").fetchall()]
        since_default = (_now() - timedelta(days=LOOKBACK_DAYS)).isoformat()
        actions, applied_count = [], 0

        for row in saved:
            wk, sid = row["wallet_key"], row["strategy_id"]
            params = json.loads(row["params_json"])
            risk = row["risk_pct"] if row["risk_pct"] is not None else default_risk
            last = conn.execute(
                "SELECT MAX(r.run_at) FROM optimizer_actions a JOIN optimizer_runs r ON r.id = a.run_id "
                "WHERE a.wallet_key = ? AND a.applied = 1", (wk,)).fetchone()[0]
            since = max(since_default, last) if last else since_default
            trades = conn.execute(
                "SELECT net_pnl, r_multiple FROM paper_trade_history WHERE COALESCE(tag,'manual') = ? AND exit_time >= ?",
                (wk, since)).fetchall()
            m = _metrics(trades)
            d = _decide(sid, params, risk, m)

            if d["action"] != "none" and conn.execute(
                    "SELECT 1 FROM paper_positions WHERE COALESCE(tag,'manual') = ? LIMIT 1", (wk,)).fetchone():
                d = {**d, "action": "deferred", "finding": d["finding"],
                     "reason": "Wanted to change something, but a position is open right now — will retry at the next review so the open trade isn't disturbed.",
                     "expected_effect": "No change today."}

            applied = 0
            if d["action"] in ("adjust_rr", "adjust_risk", "pause"):
                try:
                    _apply(conn, wk, sid, params, row["risk_pct"], d)
                    applied = 1
                    applied_count += 1
                except Exception as exc:  # one bad strategy must not abort the whole review
                    d = {**d, "action": "none", "reason": d["reason"] + f" (Could not apply: {exc!r})"}
            actions.append((wk, sid, m, d, applied))

        summary = f"Reviewed {len(saved)} strateg{'y' if len(saved) == 1 else 'ies'}; applied {applied_count} change(s)."
        run_id = conn.execute(
            "INSERT INTO optimizer_runs (run_at, trigger, strategies_reviewed, changes_applied, summary) VALUES (?,?,?,?,?)",
            (_now().isoformat(), trigger, len(saved), applied_count, summary)).lastrowid
        for wk, sid, m, d, applied in actions:
            conn.execute(
                """INSERT INTO optimizer_actions (run_id, wallet_key, strategy_id, trades, win_rate, profit_factor,
                   expectancy_r, net_pnl, finding, action, param, old_value, new_value, reason, expected_effect, applied)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, wk, sid, m["trades"], m["win_rate"], m["profit_factor"], m["expectancy_r"], m["net_pnl"],
                 d["finding"], d["action"], d.get("param"),
                 None if d.get("old") is None else str(d["old"]), None if d.get("new") is None else str(d["new"]),
                 d["reason"], d["expected_effect"], applied))
        conn.commit()
    finally:
        conn.close()
    ACTIVITY.add("info", f"Daily optimizer ({trigger}): {summary}")
    return run_id


# ---------------------------------------------------------------------- #
def _already_ran_today() -> bool:
    conn = db.get_conn()
    try:
        today = _now().date().isoformat()
        return conn.execute(
            "SELECT 1 FROM optimizer_runs WHERE trigger = 'scheduled' AND substr(run_at, 1, 10) = ?", (today,)
        ).fetchone() is not None
    finally:
        conn.close()


def _loop() -> None:
    while True:
        try:
            if is_enabled() and _now().hour >= RUN_HOUR_UTC and not _already_ran_today():
                run_review("scheduled")
        except Exception as exc:
            print(f"[optimizer] review failed: {exc!r}")
        time.sleep(300)


def start_scheduler() -> None:
    threading.Thread(target=_loop, daemon=True, name="optimizer").start()
