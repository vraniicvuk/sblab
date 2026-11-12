"""Conservative candidate calculations from raw The Odds API event snapshots."""
from datetime import datetime,timezone
import math
from urllib.parse import urlsplit


def safe_link(value):
    if not isinstance(value,str) or len(value)>2000:return None
    u=urlsplit(value)
    return value if u.scheme=='https' and u.hostname and not u.username and not u.password else None


def scan(events,stake=100,now=None,max_age=120):
    now=now or datetime.now(timezone.utc)
    if not isinstance(events,list):raise ValueError('Neispravan odgovor izvora')
    found=[]
    for event in events:
        if not isinstance(event,dict):continue
        home,away=event.get('home_team'),event.get('away_team')
        if not isinstance(home,str) or not isinstance(away,str) or home==away:continue
        # Every contributing bookmaker must expose the same full outcome set.
        # This alone cannot establish settlement compatibility; all output is conditional.
        books=[]
        for b in event.get('bookmakers',[]):
            for m in b.get('markets',[]):
                if m.get('key')!='h2h':continue
                out=m.get('outcomes',[])
                if not isinstance(out,list):continue
                names=[x.get('name') for x in out if isinstance(x,dict)]
                if len(names)!=len(set(names)) or set(names) not in ({home,away},{home,away,'Draw'}):continue
                when=m.get('last_update') or b.get('last_update')
                try:
                    stamp=datetime.fromisoformat(str(when).replace('Z','+00:00'))
                    age=(now-stamp).total_seconds()
                except (ValueError,TypeError):continue
                if not 0<=age<=max_age:continue
                prices={}
                for o in out:
                    price=o.get('price')
                    if isinstance(price,(int,float)) and not isinstance(price,bool) and math.isfinite(price) and price>1:
                        prices[o['name']]=(price,safe_link(o.get('link')) or safe_link(m.get('link')) or safe_link(b.get('link')))
                if len(prices)==len(names):books.append((frozenset(names),b.get('title','Nepoznato'),prices,when))
        # Reject ambiguous two-way / three-way mixtures rather than excluding draws.
        groups={x[0] for x in books}
        if len(groups)!=1 or len(books)<2:continue
        names=next(iter(groups));ordered=[home]+(['Draw'] if 'Draw' in names else [])+[away]
        legs=[]
        for n in ordered:
            group=max(books,key=lambda b:b[2][n][0]);price,link=group[2][n]
            legs.append({'outcome':n,'bookmaker':group[1],'odds':price,'link':link,'updated_at':group[3]})
        if len({x['bookmaker'] for x in legs})<2:continue
        inv=sum(1/x['odds'] for x in legs)
        if inv>=1:continue
        for l in legs:l['suggested_stake']=round(stake/(inv*l['odds']),2)
        found.append({'id':event.get('id'),'home':home,'away':away,'sport':event.get('sport_title'),
          'commence_time':event.get('commence_time'),'legs':legs,'theoretical_roi':round((1/inv-1)*100,3),
          'warning':'Kandidat: proveriti sva pravila, žreb, produžetke, provizije i važeće kvote.'})
    return sorted(found,key=lambda x:x['theoretical_roi'],reverse=True)
