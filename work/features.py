"""Local multi-user workflow. This does not place, verify or settle bets."""
import json, math, time, re
from urllib.parse import urlsplit
from datetime import datetime

def init(c):
    c.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, snapshot TEXT NOT NULL, owner TEXT NOT NULL, status TEXT NOT NULL, legs TEXT NOT NULL, note TEXT NOT NULL DEFAULT "", realized REAL, updated REAL NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS bonuses (id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, bookmaker TEXT NOT NULL, domain TEXT NOT NULL, amount REAL NOT NULL, currency TEXT NOT NULL, requirement TEXT NOT NULL, expiration TEXT NOT NULL, status TEXT NOT NULL, updated REAL NOT NULL)')

def readable(v,n=300): return isinstance(v,str) and len(v.strip())>0 and len(v)<=n

def finite(v, positive=False):
    return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and (not positive or v>0)

def get(c,path,user):
    init(c)
    if path=='/api/bookmakers':
        row=c.execute('SELECT value FROM settings WHERE key=?',('bookmakers',)).fetchone()
        return 200,{'bookmakers':json.loads(row[0]) if row else []}
    if path=='/api/tasks':
        rows=(c.execute('SELECT id,snapshot,owner,status,legs,note,realized,updated FROM tasks ORDER BY updated DESC LIMIT 250').fetchall() if user['role']=='admin' else c.execute('SELECT id,snapshot,owner,status,legs,note,realized,updated FROM tasks WHERE owner = ? COLLATE NOCASE ORDER BY updated DESC LIMIT 250',(user['username'],)).fetchall())
        return 200,{'tasks':[{'id':r[0],'snapshot':json.loads(r[1]),'owner':r[2],'status':r[3],'legs':json.loads(r[4]),'note':r[5],'realized':r[6],'updated':r[7]} for r in rows]}
    if path=='/api/bonuses':
        if user['role']!='admin':return 403,{'error':'Samo administrator.'}
        rows=c.execute('SELECT id,owner,bookmaker,domain,amount,currency,requirement,expiration,status,updated FROM bonuses ORDER BY updated DESC LIMIT 250').fetchall()
        return 200,{'bonuses':[dict(zip(('id','owner','bookmaker','domain','amount','currency','requirement','expiration','status','updated'),r)) for r in rows]}
    return 404,{'error':'Nepoznat zahtev.'}

def post(c,path,user,d):
    init(c); now=time.time()
    if path=='/api/bookmakers':
        if user['role']!='admin':return 403,{'error':'Samo administrator.'}
        entries=d.get('bookmakers')
        if not isinstance(entries,list) or len(entries)>100:return 400,{'error':'Neispravna lista.'}
        cleaned=[];seen=set()
        for entry in entries:
            if not isinstance(entry,dict) or not isinstance(entry.get('id'),int) or entry['id']<1 or entry['id'] in seen:return 400,{'error':'ID kladionice mora biti jedinstven broj.'}
            u=entry.get('url','').strip()
            if u and (urlsplit(u).scheme!='https' or not urlsplit(u).hostname or urlsplit(u).username or urlsplit(u).password or len(u)>300):return 400,{'error':'Domen mora biti HTTPS adresa.'}
            seen.add(entry['id']);cleaned.append({'id':entry['id'],'url':u})
        c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('bookmakers',json.dumps(cleaned)));c.commit();return 200,{'ok':True}
    if path=='/api/tasks':
        action=d.get('action');ident=d.get('id')
        if not readable(ident,200):return 400,{'error':'Nedostaje ID prilike.'}
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT owner,status FROM tasks WHERE id=?',(ident,)).fetchone()
        if action=='claim':
            snap=d.get('snapshot')
            if not isinstance(snap,dict) or str(snap.get('id'))!=ident or not isinstance(snap.get('legs'),list) or len(snap['legs']) not in (2,3):return 400,{'error':'Nepotpuna prilika.'}
            if row:return 409,{'error':'Prilika je već preuzeta ili evidentirana.'}
            if any('pauziran' in str(x).lower() or 'nedostaje' in str(x).lower() for x in snap.get('reasons',[])):return 400,{'error':'Pauziran ili nepotpun kandidat ne može se preuzeti.'}
            for leg in snap['legs']:
                try:
                    stamp=datetime.fromisoformat(str(leg['updated_at']).replace('Z','+00:00')).timestamp()
                    if not 0<=now-stamp<=120 or not finite(leg.get('odds'),True) or leg['odds']<=1: raise ValueError()
                except (ValueError,TypeError,KeyError,OverflowError):return 400,{'error':'Kvota je starija od 120 sekundi ili nedostaju podaci.'} 
            c.execute('INSERT INTO tasks (id,snapshot,owner,status,legs,note,updated) VALUES (?,?,?,?,?,?,?)',(ident,json.dumps(snap,ensure_ascii=False),user['username'],'preuzeto','[]','',now))
        elif row is None:return 404,{'error':'Prilika ne postoji.'}
        elif row[0].lower()!=user['username'].lower() and user['role']!='admin':return 403,{'error':'Priliku obrađuje drugi korisnik.'}
        elif action=='record':
            if row[1] in ('zavrseno','odbaceno','obracunato'):return 409,{'error':'Prilika je već zaključena.'}
            legs=d.get('legs'); note=d.get('note','')
            if not isinstance(legs,list) or len(legs)>3 or not isinstance(note,str) or len(note)>1000:return 400,{'error':'Neispravan unos.'}
            for leg in legs:
                if not isinstance(leg,dict) or not finite(leg.get('stake'),True) or not finite(leg.get('odds'),True) or leg['odds']<=1:return 400,{'error':'Unesi prihvaćen ulog i kvotu veću od 1.'}
            oldlegs=json.loads(c.execute('SELECT legs FROM tasks WHERE id=?',(ident,)).fetchone()[0])
            if len(legs)!=len(oldlegs)+1 or legs[:len(oldlegs)]!=oldlegs:return 409,{'error':'Evidencija je promenjena. Osveži podatke i pokušaj ponovo.'}
            snap=json.loads(c.execute('SELECT snapshot FROM tasks WHERE id=?',(ident,)).fetchone()[0]); n=len(snap['legs'])
            if len(legs)>n:return 400,{'error':'Previše uplata.'}
            status='zavrseno' if len(legs)==n else ('delimicno' if legs else 'preuzeto')
            c.execute('UPDATE tasks SET legs=?,note=?,status=?,updated=? WHERE id=?',(json.dumps(legs),note,status,now,ident))
        elif action=='reject':
            legs=json.loads(c.execute('SELECT legs FROM tasks WHERE id=?',(ident,)).fetchone()[0])
            if legs:return 409,{'error':'Postoji evidentirana uplata: prijavi otvorenu izloženost.'}
            c.execute('UPDATE tasks SET status=?,updated=? WHERE id=?',('odbaceno',now,ident))
        elif action=='settle':
            if user['role']!='admin':return 403,{'error':'Samo administrator unosi konačan rezultat.'}
            if row[1] not in ('zavrseno','delimicno'):return 409,{'error':'Rezultat možeš uneti samo za evidentirane uplate.'}
            val=d.get('realized');note=d.get('note','')
            if not finite(val) or not isinstance(note,str) or len(note)>1000:return 400,{'error':'Unesi stvarni neto rezultat.'}
            c.execute('UPDATE tasks SET status=?,realized=?,note=?,updated=? WHERE id=?',('obracunato',val,note,now,ident))
        else:return 400,{'error':'Nepoznata akcija.'}
        c.commit();return 200,{'ok':True}
    if path=='/api/bonuses':
        if user['role']!='admin':return 403,{'error':'Samo administrator.'}
        if d.get('action')=='status':
            status=d.get('status');ident=d.get('id')
            if status not in ('aktivan','ispunjen','istekao','odbacen') or not isinstance(ident,int):return 400,{'error':'Neispravan status.'}
            if not c.execute('SELECT 1 FROM bonuses WHERE id=?',(ident,)).fetchone():return 404,{'error':'Bonus ne postoji.'}
            c.execute('UPDATE bonuses SET status=?,updated=? WHERE id=?',(status,now,ident))
        else:
            fields=[d.get('owner'),d.get('bookmaker'),d.get('currency'),d.get('requirement'),d.get('expiration')]
            amount=d.get('amount');url=d.get('domain','')
            if not all(readable(x) for x in fields) or not finite(amount,True) or not isinstance(url,str) or len(url)>300:return 400,{'error':'Popuni sva polja i validan iznos.'}
            if url and (urlsplit(url).scheme!='https' or not urlsplit(url).hostname):return 400,{'error':'Domen mora biti HTTPS adresa.'}
            c.execute('INSERT INTO bonuses (owner,bookmaker,domain,amount,currency,requirement,expiration,status,updated) VALUES (?,?,?,?,?,?,?,?,?)',(d['owner'],d['bookmaker'],url,amount,d['currency'],d['requirement'],d['expiration'],'aktivan',now))
        c.commit();return 200,{'ok':True}
    return 404,{'error':'Nepoznat zahtev.'}
