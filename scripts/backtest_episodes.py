"""Backtest the recorded contradictions under several versions of the trading rules.

Each contradiction in the episode tables is bought on its safer rung at the ask seen
when it opened (ref_safer_ask), with the taker fee, and held to settlement. A contract
not settled yet is marked at today's bid less the exit fee, which is pessimistic for thin
markets. One entry per contract: the first episode a variant would have taken.

The episodes keep only part of what the live filters look at, so the variants are
approximations: no spread, no underlying check, and depth and volume only through the
`alertable` flag (the alert filters, which also need 24h left).

    python scripts/fetch_episode_markets.py   # writes episode_markets.json
    python scripts/backtest_episodes.py       # DB_PATH picks another database
"""
import json, os, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kalshi_bot.fees import fee_per_contract
from kalshi_bot.paper import settlement
from kalshi_bot.ladder import to_decimal

ROOT = Path(__file__).resolve().parents[1]
M = json.loads((ROOT / "episode_markets.json").read_text())
DB = Path(os.environ.get("DB_PATH") or ROOT / "kalshi_bot.sqlite3")
db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
db.row_factory = sqlite3.Row
rows = [dict(r, is_open=0) for r in db.execute("select * from episodes_closed")]
rows += [dict(r, is_open=1) for r in db.execute("select * from episodes_open")]
NOW = max(r["last_seen"] for r in rows)


def epoch(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None


eps = []
for r in rows:
    m = M.get(r["safer"])
    if m is None or r["ref_safer_ask"] is None:
        continue
    p = float(r["ref_safer_ask"])
    if not 0 < p < 1:
        continue
    ends = [t for t in (epoch(m.get("close_time")), epoch(m.get("expected_expiration_time"))) if t]
    hours = (min(ends) - r["opened_at"]) / 3600 if ends else None
    cost = p + float(fee_per_contract(to_decimal(p)))
    pay = settlement(m, r["side"])
    if pay is not None:
        value, settled = float(pay), True
    else:
        bid = float(to_decimal(m.get(f"{r['side']}_bid_dollars")) or 0)
        value, settled = (bid - float(fee_per_contract(to_decimal(bid))) if bid > 0 else 0.0), False
    eps.append(dict(side=r["side"], safer=r["safer"], event=m["event_ticker"], p=p, hours=hours,
                    dur=r["last_seen"] - r["opened_at"], alertable=r["alertable"],
                    net=float(r["max_net_saving"]), opened=r["opened_at"], cost=cost, value=value,
                    settled=settled, exch=int(m.get("exchange_index") or 0), series=m["event_ticker"].split("-")[0]))
eps.sort(key=lambda e: e["opened"])


def first_per_contract(es):
    seen, out = set(), []
    for e in es:
        k = (e["safer"], e["side"])
        if k not in seen:
            seen.add(k); out.append(e)
    return out


def stats(name, es, show=True):
    es = first_per_contract(es)
    if not es:
        print(f"{name:<52} n=0"); return
    s = [e for e in es if e["settled"]]
    def ret(x):
        c = sum(e["cost"] for e in x); return (sum(e["value"] for e in x) - c) / c * 100 if c else 0.0
    wins = sum(e["value"] > e["cost"] for e in s)
    events = len({e["event"] for e in es})
    print(f"{name:<52} n={len(es):>4} events={events:>4} | settled {len(s):>4}: win {wins / len(s) * 100 if s else 0:5.1f}% "
          f"ret {ret(s):+6.1f}% | all incl. marked: ret {ret(es):+6.1f}%")


base = [e for e in eps if 0.30 <= e["p"] <= 0.90 and e["net"] >= 0.01]
hrs = lambda e, lo, hi=1e9: e["hours"] is not None and lo <= e["hours"] < hi
yes60 = lambda e: e["side"] != "yes" or e["p"] >= 0.60
days = (NOW - min(e["opened"] for e in eps)) / 86400
print(f"episodes with market data: {len(eps)} over {days:.1f} days; settled {sum(e['settled'] for e in eps)}\n")

print("== rule variants (price .30-.90, net saving >= 1c; one entry per contract) ==")
stats("everything in price range", base)
stats("alert filter (depth, volume, >=24h)", [e for e in base if e["alertable"]])
stats("alert filter + YES>=.60", [e for e in base if e["alertable"] and yes60(e)])
stats("~strict: alert + >=168h + YES>=.60", [e for e in base if e["alertable"] and hrs(e, 168) and yes60(e)])
stats("  same, exchange 0 only", [e for e in base if e["alertable"] and hrs(e, 168) and yes60(e) and e["exch"] == 0])
stats("~patient: >=6h + lasted>=240s + YES>=.60", [e for e in base if hrs(e, 6) and e["dur"] >= 240 and yes60(e)])
stats("  same + alert filter", [e for e in base if hrs(e, 6) and e["dur"] >= 240 and yes60(e) and e["alertable"]])
stats("lasted>=240s, >=6h, any YES price", [e for e in base if hrs(e, 6) and e["dur"] >= 240])
stats("lasted>=240s, 6-72h, YES>=.60", [e for e in base if hrs(e, 6, 72) and e["dur"] >= 240 and yes60(e)])

print("\n== by side and price (price range, net >= 1c) ==")
for side in ("no", "yes"):
    for lo, hi in ((0.30, 0.45), (0.45, 0.60), (0.60, 0.75), (0.75, 0.905)):
        stats(f"{side.upper()} {lo:.2f}-{min(hi, .90):.2f}", [e for e in base if e["side"] == side and lo <= e["p"] < hi])

print("\n== by hours to resolution at first sight ==")
for lo, hi, lab in ((0, 6, "<6h"), (6, 24, "6-24h"), (24, 72, "24-72h"), (72, 168, "3-7d"), (168, 1e9, ">7d")):
    stats(lab, [e for e in base if hrs(e, lo, hi)])

print("\n== by how long the contradiction lasted ==")
for lo, hi, lab in ((0, 1, "one scan only"), (1, 600, "<10 min"), (600, 3600, "10-60 min"), (3600, 1e9, ">1 hour")):
    stats(lab, [e for e in base if lo <= e["dur"] < hi])

print("\n== top series in the ~patient set ==")
pat = first_per_contract([e for e in base if hrs(e, 6) and e["dur"] >= 240 and yes60(e)])
by = defaultdict(list)
for e in pat: by[e["series"]].append(e)
for s, es in sorted(by.items(), key=lambda kv: -len(kv[1]))[:12]:
    c = sum(e["cost"] for e in es); v = sum(e["value"] for e in es)
    print(f"  {s:<28} n={len(es):>3} settled={sum(e['settled'] for e in es):>3} ret {(v - c) / c * 100:+6.1f}%")
