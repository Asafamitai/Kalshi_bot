"""District results from 538's House file: per (cycle, state, district) the major-party shares."""
import csv, collections
DEM={'DEM','DFL','DNL'}
def load(path='data/538/election_results_house.csv'):
    rows=[x for x in csv.DictReader(open(path)) if x['stage']=='general' and x['special']=='false'
          and x['candidate_name'].strip() and x['ranked_choice_round'] in ('','1')]
    cands=collections.defaultdict(lambda: {'votes':0.0,'parties':set(),'winner':False,'unopposed':False})
    for x in rows:
        k=(x['cycle'],x['state_abbrev'],x['office_seat_name'],x['candidate_id'])
        c=cands[k]; c['name']=x['candidate_name']; c['votes']+=float(x['votes'] or 0)
        c['parties'].add(x['ballot_party']); c['winner']|=x['winner']=='true'; c['unopposed']|=x['unopposed']=='true'
    races=collections.defaultdict(list)
    for (cy,st,seat,_),c in cands.items():
        c['party']='D' if c['parties']&DEM else ('R' if 'REP' in c['parties'] else 'O')
        races[(cy,st,seat)].append(c)
    out={}
    for k,cs in races.items():
        tot=sum(c['votes'] for c in cs)
        d=[c for c in cs if c['party']=='D']; r=[c for c in cs if c['party']=='R']
        if len(d)!=1 or len(r)!=1 or tot<=0 or d[0]['unopposed'] or r[0]['unopposed']:
            out[k]={'contested':False,'cands':cs}; continue
        D,R=d[0]['votes'],r[0]['votes']
        w=[c for c in cs if c['winner']]
        out[k]={'contested':True,'total':tot,'d2p':100*D/(D+R),'third':100*(1-(D+R)/tot),'dname':d[0]['name'],'rname':r[0]['name'],
                'winner':w[0]['name'] if w else None,'wparty':w[0]['party'] if w else None,'cands':cs}
    return out
def surname(n):
    parts=[p for p in n.replace(',',' ').replace('.',' ').split() if p.lower() not in('jr','sr','ii','iii','iv')]
    return parts[-1].lower() if parts else ''
