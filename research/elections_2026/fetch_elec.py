import json, sys, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
from kalshi_bot.kalshi import KalshiClient
c=KalshiClient(); out={}
for series in ("KXVOTEGENERAL","KXMIDTERMMOV"):
    ms=[]; cur=None
    while True:
        p={"series_ticker":series,"status":"open","limit":1000}
        if cur: p["cursor"]=cur
        d=c._request("GET","/markets",params=p); ms+=d.get("markets") or []; cur=d.get("cursor")
        if not cur: break
        time.sleep(0.2)
    evs=[]; cur=None
    while True:
        p={"series_ticker":series,"status":"open","limit":200}
        if cur: p["cursor"]=cur
        d=c._request("GET","/events",params=p); evs+=d.get("events") or []; cur=d.get("cursor")
        if not cur: break
        time.sleep(0.2)
    out[series]={"markets":ms,"events":evs}; print(series,len(ms),len(evs))
out["fetched_at"]=time.time()
json.dump(out,open("data/elections.json","w"))
