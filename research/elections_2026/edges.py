import json, collections
rows=json.load(open('data/model_rows.json'))
fee=lambda p:0.07*p*(1-p)
E=[]
for r in rows:
    b,a,m=r['bid'],r['ask'],r['model']
    if 0<a<1 and 0.08<=a<=0.92: E.append((m-a-fee(a),'YES',a,r))
    if 0<b<1 and 0.08<=1-b<=0.92: E.append(((1-m)-(1-b)-fee(1-b),'NO',1-b,r))
best={}
for e,side,price,r in E:
    k=(r['race'],r['cand'])
    if e>0.05 and (k not in best or e>best[k][0]): best[k]=(e,side,price,r)
L=sorted(best.values(),key=lambda x:-x[0])
print('candidates with >=5c edge after fee:',len(L),' races:',len({x[3]['race'] for x in L}))
print(f"{'race':7s} {'candidate':22s} p inc  model_mu±sd  rung   side  price model  edge  OI")
for e,side,price,r in L[:30]:
    pm=r['model'] if side=='YES' else 1-r['model']
    print(f"{r['race']:7s} {r['cand'][:22]:22s} {r['party']} {'I' if r['inc'] else ' '}  {r['mu']:5.1f}±{r['sd']:.1f}  >={r['K']:<4.0f} {side:4s} {price:5.2f} {pm:5.2f} {e:5.2f} {r['oi']:6.0f}")
