"""House 2026 vote-share model vs Kalshi's KXVOTEGENERAL ladders.

d2p_2026 = 2016->2018 fit applied to 2024 results, shifted by the extra national swing.
"""
import json, math, re, collections, csv, sys
import numpy as np
from hist import load, surname
GB_MARGIN = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0      # generic ballot, D minus R
NAT_SD = 2.0      # 2p uncertainty of the national environment (poll error + 18 days)
RESID_SD = 3.54   # 2016->2018 district residual
THIRD_SD = 1.5
REDRAWN = {'CA','TX','MO','NC','OH','UT','FL','AL','LA','TN','AK'}
H = load()
# coefficients from 2016->2018
X=[];Y=[]
for (cy,st,seat),r0 in H.items():
    if cy!='2016' or st=='PA' or not r0['contested']: continue
    r1=H.get(('2018',st,seat))
    if not r1 or not r1['contested']: continue
    ws=1 if r0['wparty']=='D' else -1
    run=surname(r0['winner']) in {surname(c['name']) for c in r1['cands']}
    X.append((1,r0['d2p'],ws*run,ws)); Y.append(r1['d2p'])
B,*_=np.linalg.lstsq(np.array(X,float),np.array(Y),rcond=None)
SWING18 = 54.53-49.71
SWING26 = (50+GB_MARGIN/2) - 48.73
SHIFT = SWING26-SWING18
thirds=[r['third'] for k,r in H.items() if k[0]=='2024' and r['contested']]
MED_THIRD=float(np.median(thirds))
N=lambda x:0.5*(1+math.erf(x/math.sqrt(2)))
fee=lambda p:0.07*p*(1-p)
D=json.load(open('data/elections.json'))
EV={e['event_ticker']:e for e in D['KXVOTEGENERAL']['events']}
MK=collections.defaultdict(list)
for m in D['KXVOTEGENERAL']['markets']: MK[m['event_ticker']].append(m)
# party hints from margin-of-victory events: "<Surname>, N+ pts" in KXMIDTERMMOV-<ST><NN><P>
movparty={}
for m in D['KXMIDTERMMOV']['markets']:
    mm=re.match(r'KXMIDTERMMOV-([A-Z]{2})(\d{2}|AL)([DR])',m['event_ticker'])
    if mm and m.get('yes_sub_title'):
        movparty[(mm.group(1),mm.group(2),m['yes_sub_title'].split(',')[0].strip().lower())]=mm.group(3)
races=collections.defaultdict(list)
for et,e in EV.items():
    mm=re.match(r'([A-Z]{2})-(\d{2}|AL) House election: (.+) vote percent$',e['title'])
    if mm: races[(mm.group(1),mm.group(2))].append((mm.group(3),et))
tot24=sorted(r['total'] for k,r in H.items() if k[0]=='2024' and r['contested'])
MIN_TOTAL=0.5*tot24[len(tot24)//2]
rows=[]; skipped=collections.Counter()
for (st,dn),cands in races.items():
    if st in REDRAWN: skipped['redrawn']+=1; continue
    seat='District '+str(1 if dn=='AL' else int(dn))
    r24=H.get(('2024',st,seat))
    if not r24 or not r24['contested']: skipped['2024 not contested']+=1; continue
    if r24['total']<MIN_TOTAL: skipped['2024 totals incomplete']+=1; continue
    party={}
    for name,et in cands:
        s=surname(name)
        if s==surname(r24['dname']): party[et]='D'
        elif s==surname(r24['rname']): party[et]='R'
        elif (st,dn,s) in movparty: party[et]=movparty[(st,dn,s)]
    for name,et in cands:
        if et not in party:
            known={p for e2,p in party.items()}
            if len(cands)==2 and len(known)==1: party[et]='R' if 'D' in known else 'D'
    if len(cands)!=2 or sorted(party.values())!=['D','R']: skipped['candidates/party unclear']+=1; continue
    ws=1 if r24['wparty']=='D' else -1
    run=surname(r24['winner']) in {surname(n) for n,_ in cands}
    d2p=float(B@np.array([1,r24['d2p'],ws*run,ws]))+SHIFT
    third=0.5*r24['third']+0.5*MED_THIRD
    for name,et in cands:
        p=party[et]; share=(d2p if p=='D' else 100-d2p)*(1-third/100)
        sd=math.sqrt(((RESID_SD**2+NAT_SD**2)*(1-third/100)**2)+(share/100*THIRD_SD)**2)
        for m in MK[et]:
            if m.get('strike_type')!='greater_or_equal' or m.get('floor_strike') is None: continue
            K=float(m['floor_strike']); pm=1-N((K-share)/sd)
            b,a=float(m['yes_bid_dollars']),float(m['yes_ask_dollars'])
            rows.append(dict(race=f'{st}-{dn}',cand=name,party=p,inc=run and surname(name)==surname(r24['winner']),
                             mu=round(share,1),sd=round(sd,1),K=K,model=pm,bid=b,ask=a,ticker=m['ticker'],
                             oi=float(m['open_interest_fp']),vol=float(m['volume_24h_fp']),
                             base24=round(r24['d2p'],1)))
print('coef',[round(float(v),2) for v in B],'shift',round(SHIFT,2),'median third',round(MED_THIRD,2))
print('races modelled',len({r['race'] for r in rows}),'rungs',len(rows),'skipped',dict(skipped))
json.dump(rows,open('data/model_rows.json','w'))
