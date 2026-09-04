"""One-shot backfill of historical ATM IV so iv_rank clears cold_start
immediately instead of waiting 20+ real trading days (audit item 2).

python scripts/backfill_iv_history.py [days]
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.iv_rank import backfill_history

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 45


async def main() -> None:
    result = await backfill_history("SPY", days=DAYS)
    print(f"seeded {result['seeded']} day(s) of {result['days_requested']} requested")
    if result["skipped"]:
        print(f"skipped {len(result['skipped'])}:")
        for s in result["skipped"]:
            print(f"  {s['date']}: {s['reason']}")


if __name__ == "__main__":
    asyncio.run(main())
