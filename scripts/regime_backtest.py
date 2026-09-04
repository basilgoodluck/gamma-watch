"""Historical sanity check for the regime engine.

Slides the classifier across the last ~N days of SPY bars (no lookahead -
each point only sees bars up to itself) and prints the regime sequence, so
we can eyeball it for implausible bar-to-bar flapping.

python scripts/regime_backtest.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.regime import classify_history

BACKTEST_LOOKBACK_DAYS = 60
EVAL_STEP = 8  # print/evaluate every Nth bar, not every single bar (too dense to eyeball)


async def main() -> None:
    sequence = await classify_history("SPY", lookback_days=BACKTEST_LOOKBACK_DAYS, eval_step=EVAL_STEP)
    if not sequence:
        print("not enough bars for a meaningful backtest")
        return

    print(f"\n{'timestamp':<26}{'regime':<16}confidence")
    prev_regime = None
    flips = 0
    for point in sequence:
        ts, regime, confidence = point["ts"], point["regime"], point["confidence"]
        marker = "  <- flip" if prev_regime and regime != prev_regime else ""
        print(f"{str(ts):<26}{regime:<16}{confidence:<10.4f}{marker}")
        if prev_regime and regime != prev_regime:
            flips += 1
        prev_regime = regime

    print(
        f"\n{len(sequence)} points evaluated (every {EVAL_STEP} bars), "
        f"{flips} regime changes ({flips / len(sequence):.1%} of steps)"
    )


if __name__ == "__main__":
    asyncio.run(main())
