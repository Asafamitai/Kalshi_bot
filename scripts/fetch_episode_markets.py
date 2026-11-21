"""Fetch the current state of every contract the episode tables recorded as the safer rung.

Read-only GETs, 50 tickers a request, two seconds apart so a running bot sharing the
rate limit isn't starved. Results are cached in episode_markets.json (repo root), and a
second run fetches only tickers not cached yet; delete the file for fresh prices.

    python scripts/fetch_episode_markets.py      # then: python scripts/backtest_episodes.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kalshi_bot.config import Config, load_dotenv  # noqa: E402
from kalshi_bot.kalshi import KalshiError  # noqa: E402

DB = Path(os.environ.get("DB_PATH") or ROOT / "kalshi_bot.sqlite3")
OUT = ROOT / "episode_markets.json"


def main() -> None:
    load_dotenv(ROOT / ".env")
    client = Config.from_env().kalshi_client()
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    tickers = sorted({r[0] for t in ("episodes_closed", "episodes_open") for r in db.execute(f"select safer from {t}")})
    have = json.loads(OUT.read_text()) if OUT.exists() else {}
    todo = [t for t in tickers if t not in have]
    print(f"{len(tickers)} tickers, {len(todo)} to fetch", flush=True)
    for i in range(0, len(todo), 50):
        chunk = todo[i:i + 50]
        for attempt in range(5):
            try:
                data = client._request("GET", "/markets", params={"tickers": ",".join(chunk), "limit": 100})
                break
            except KalshiError as e:
                if e.status != 429:
                    raise
                time.sleep(10 * (attempt + 1))
        else:
            raise SystemExit("still rate limited after 5 tries; run again later")
        for m in data.get("markets") or []:
            have[m["ticker"]] = m
        OUT.write_text(json.dumps(have))
        time.sleep(2)
    print(f"have {len(have)}", flush=True)


if __name__ == "__main__":
    main()
