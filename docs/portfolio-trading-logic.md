# Portfolio Trading Logic

How the auto-trading bot trades the shared **portfolio** wallet: which strategies run, when they may enter, what limits apply, how trades are sized and closed, and how to start over.

Everything here is paper trading. No order ever reaches a real broker.

Code: [webapp/portfolio.py](../webapp/portfolio.py) (wallet, limits, one-time setup), [webapp/autotrade.py](../webapp/autotrade.py) (per-bar loop), [gold_bot/rule_strategy.py](../gold_bot/rule_strategy.py) (entry rules, stops, targets), [webapp/routers/paper.py](../webapp/routers/paper.py) (fills, exits, wallets, reset).

---

## 1. The wallet

| | |
|---|---|
| Wallet name | `portfolio` |
| Starting balance | $1,000 |
| Used by | Every auto-trading strategy |
| Not used by | Manual trades from the order ticket (they keep the `manual` wallet) |

All running strategies size their trades from this one balance and share the limits in section 4. Each trade still records which strategy took it (`tag`), so History and per-strategy stats keep working.

Trades made before the shared wallet existed (October 2026) stay in their old per-strategy wallets as an archive.

---

## 2. The strategies

All three trade XAU/USD on **15-minute** candles, using live biquote.io prices.

| Strategy | Entry window | Buy when | Sell when | Stop | Target |
|---|---|---|---|---|---|
| **RSI Extremes** | Any time | RSI(14) crosses back above 30 | RSI(14) crosses back below 70 | 1.5 × ATR(14) | 1.5 × risk |
| **Donchian Breakout** | 05:30–13:15 IST (signal candle 23:45–07:30 UTC) | Close crosses above the highest high of the previous 20 candles | Close crosses below the lowest low of the previous 20 candles | 1.5 × ATR(14) | 1.5 × risk |
| **EMA Cross RSI** | Any time except London, 12:30–17:30 IST (signal candle 11:45–06:30 UTC) | EMA 9 crosses above EMA 21 and RSI(14) < 70 | EMA 9 crosses below EMA 21 and RSI(14) > 30 | 1.5 × ATR(14) | 2 × risk |

Entry windows are checked on the signal candle; the fill lands one candle (15 minutes) later. A window whose start is later than its end wraps past midnight UTC.

**Why these three:** a review of 194 paper trades (20 Sep – 5 Oct 2026) found they held all of the measurable edge. Donchian worked in the 00:00–08:00 UTC entry window and EMA Cross RSI lost in London. The review covered only 13 trading days in a falling market. A replay of August 2026 with these rules **lost 17.3R**, so treat the edge as unproven.

**Stopped strategies:** Range Fade, Trend Pullback, RSI DMA Combo, NY Opening Range, Fib Retracement, Asian Sweep Reversal, Liquidity Imbalance Continuation, Liquidity Sweep Reversal, Sweep MSS FVG 4H, Second FVG Retest, Order Block Sweep and Golden/Death Cross. If you start any of them from the Auto Trade panel, it also trades from the shared wallet under the same limits.

---

## 3. What happens on every 15-minute candle

Each running strategy repeats these steps whenever a candle closes.

1. **Manage open trades** (only this strategy's own):
   - The stop was touched → close at the stop.
   - The target was touched → close at the target.
   - Both were touched in the same candle → treated as a **stop** (order inside a candle is unknown, so the cautious outcome is assumed).
   - Profit or loss goes to the shared wallet.
2. **Wallet checks.** If any fails, this candle is skipped (section 4).
3. **Entry window.** Outside its window, the strategy does nothing.
4. **Signal.** The buy or sell rule from section 2.
5. **Signal checks.** The signal is dropped if:
   - the portfolio holds a trade in the opposite direction (no long and short at once), or
   - a same-direction trade is already pending, or was entered in the last 30 minutes (the same idea from another strategy).
6. **Size and place** the trade (section 5).

Every signal, skip (with its reason), fill and exit is written to the **Live Activity** feed.

---

## 4. Limits

### Editable (Auto Trade → Running strategies → Save limits)

| Limit | Default | Counts |
|---|---:|---|
| Max open trades | 5 | Open positions + pending entry orders in the portfolio |
| Max trades per day | 20 | Trades entered on the current UTC day |
| Max trades per session | 15 | Trades entered in the current session |

Allowed values are whole numbers from 1 to 1000. Changes apply from the next candle, with no restart. The account-wide "max open positions" in Risk Settings applies only to the manual wallet.

Sessions (UTC, by entry time):

| Session | UTC | IST |
|---|---|---|
| Asian | 21:00–07:00 | 02:30–12:30 |
| London | 07:00–12:00 | 12:30–17:30 |
| Overlap | 12:00–16:00 | 17:30–21:30 |
| New York | 16:00–21:00 | 21:30–02:30 |

### Fixed (in code: `webapp/portfolio.py`)

| Rule | Value |
|---|---|
| Daily stop | No new trades once today's closed trades total −3R or worse |
| Losing streak stop | No new trades after 3 losses in a row today |
| No hedging | Never long and short at the same time |
| Duplicate filter | One same-direction entry per 30 minutes |

### From Risk Settings (if set)

- Max daily loss %, max drawdown %.
- Trading halts if the wallet balance reaches $0.

All stops block **new** entries only. Open trades still run to their own stop or target.

---

## 5. Position size and costs

- **Risk per trade:** 1% of the current portfolio balance ($10 at $1,000).
- **Lots** = risk ÷ (stop distance × 100 oz), rounded to 0.01.
- With a $1,000 balance this almost always rounds to **0.01 lot**. One 0.01 lot with a $10–20 stop really risks **1–2%** of the wallet.
- If the size rounds to 0 (a very wide stop), the trade is skipped and logged.
- Orders are market orders, filled at the **open of the next candle**.
- Spread and costs (about $0.50 per 0.01 lot) are deducted, and R-multiples include them.
- There is no trailing stop and no partial close: each trade ends at its stop or its target.

---

## 6. Starting, stopping and starting over

**Start the strategies** (if they're stopped):

1. Auto Trade → tick **RSI Extremes (cross-back)**, **Donchian Breakout (N=20)**, **EMA9/21 Cross + RSI filter**.
2. Set **Risk / trade (%)** to **1** (blank uses the account default).
3. Press **Start Selected**.

They come back on their own after a server restart or redeploy, until you press **Stop**.

**Start the portfolio over** (fresh balance):

1. Dashboard → wallet table → **portfolio** row → **Reset**.
2. Enter the starting balance (defaults to the current one, $1,000).

On Reset:

- Past trades are **archived, not deleted**. They stay in History, re-labelled `portfolio#archived-<date-time>`, and no longer count toward the balance, stats or today's limits.
- Pending orders are cancelled.
- Open positions are **dropped** (not closed, and not recorded). Reset when nothing is open if you want every trade logged.
- Running strategies keep running and trade from the new balance.
- Your limits (section 4) are kept.

The global "reset everything" works the same way for every wallet.

---

## 7. One-time setup (applied October 2026)

On the first startup after the change, a one-time migration (`shared_portfolio_wallet_v1`):

- stopped every saved strategy except the three in section 2,
- set their entry windows and 1% risk,
- cancelled pending orders left in the old per-strategy wallets,
- opened the `portfolio` wallet with $1,000,
- turned the **daily optimizer off** so it doesn't re-tune risk or targets on its own (it can be turned back on in the UI).

Railway's deploy log shows what it did on lines starting with `[shared_portfolio_wallet_v1]`. The migration never runs again on the same database.

---

## 8. Known risks

- **Unproven edge.** The rules come from 13 trading days in one falling market, and an August 2026 replay of the same setup lost 17.3R ($1,000 → $830).
- **Real risk above 1%.** On $1,000, 0.01 lot is the minimum size, so most trades risk 1–2%.
- **Same-candle ambiguity.** Stop and target touched in one candle always count as a stop, which is pessimistic by design.
- **Paper fills.** Real broker spreads and slippage can be worse than the paper engine's.

Review the strategies after about 100 more trades before changing any rule or raising risk.
