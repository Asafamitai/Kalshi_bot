import json, collections, statistics, math
rows=json.load(open('data/model_rows.json'))
fee=lambda p:0.07*p*(1-p)
two=[r for r in rows if 0<r['bid']<r['ask']<1]
for r in two: r['mid']=(r['bid']+r['ask'])/2
d=[r['model']-r['mid'] for r in two]
print('two-sided rungs',len(two),'mean(model-mid)',round(statistics.mean(d),3),'mean |diff|',round(statistics.mean(map(abs,d)),3))
# market-implied median per candidate: strike where mid crosses 0.5 (linear interp)
by=collections.defaultdict(list)
for r in two: by[(r['race'],r['cand'])].append(r)
cmp=[]
for k,rs in by.items():
    rs.sort(key=lambda r:r['K'])
    med=None
    for a,b in zip(rs,rs[1:]):
        if a['mid']>=0.5>=b['mid'] and a['mid']!=b['mid']:
            med=a['K']+(a['mid']-0.5)/(a['mid']-b['mid'])*(b['K']-a['K']); break
    if med is not None: cmp.append((round(rs[0]['mu']-med,1),k,rs[0]['party'],rs[0]['mu'],round(med,1),rs[0]['inc'],rs[0]['base24']))
diffs=[c[0] for c in cmp]
print('candidates with market median',len(cmp),'mean(model-market) pts',round(statistics.mean(diffs),2),'SD',round(statistics.pstdev(diffs),2))
for p in 'DR':
    x=[c[0] for c in cmp if c[2]==p]; print(' party',p,'n',len(x),'mean diff',round(statistics.mean(x),2))
cmp.sort()
print('\nlargest disagreements (model mu - market median), pts:')
for c in cmp[:12]+cmp[-12:]: print(' ',c)
json.dump(cmp,open('data/medians.json','w'))
