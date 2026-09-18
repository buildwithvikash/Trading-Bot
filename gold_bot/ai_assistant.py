"""Gold Bot AI Assistant — CLI & Python Module.

Usage:
  python gold_bot/ai_assistant.py --test-stream
  python gold_bot/ai_assistant.py --run-auto-quant

Or import programmatically:
  from gold_bot.ai_assistant import run_test_stream, run_auto_quant
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

if sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

from anthropic import Anthropic



def get_client() -> Anthropic:
    base_url = os.getenv("ANTHROPIC_BASE_URL", "https://api.openapis.online/anthropic")
    api_key = os.getenv("ANTHROPIC_API_KEY", "admin")
    return Anthropic(base_url=base_url, api_key=api_key)


def run_test_stream(prompt: str = "Hello, Claude! Analyze gold market principles for high win-rate trading."):
    """Demonstrate Anthropic text stream as specified in user request."""
    client = get_client()
    model = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-7")

    print(f"Connecting to Anthropic API endpoint: {client.base_url}")
    print(f"Streaming model: {model}\n--- Output Stream ---")

    try:
        with client.messages.stream(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for text in stream.text_stream:
                print(text, end="", flush=True)
        print("\n--- Stream Complete ---")
    except Exception as exc:
        print(f"\n[Anthropic API Notice]: {exc}")



def run_auto_quant():
    """Run full Autonomous AI Quant Pipeline from terminal."""
    from webapp.ai_quant_engine import run_auto_quant_pipeline

    print("Launching Autonomous AI Quant Engine Pipeline...")
    res = run_auto_quant_pipeline(timeframe="15min", auto_start_demo=True)
    print("\n" + res["report_markdown"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gold Bot AI Assistant")
    parser.add_argument("--test-stream", action="store_true", help="Run test Anthropic stream")
    parser.add_argument("--run-auto-quant", action="store_true", help="Run Autonomous Quant Pipeline")
    args = parser.parse_args()

    if args.run_auto_quant:
        run_auto_quant()
    else:
        run_test_stream()
