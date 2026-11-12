#!/usr/bin/env python3
import http.server, json, os, pathlib, re, urllib.parse, urllib.request, urllib.error
from betburger_adapter import normalize_response
import features
import sqlite3, hashlib, hmac, secrets, time, threading
from datetime import datetime, timezone
from http.cookies import SimpleCookie
ROOT = pathlib.Path(__file__).resolve().parent.parent
PUBLIC = ROOT / 'outputs'
KEYFILE = ROOT / 'work' / '.odds-api.env'
DBFILE = ROOT / 'work' / 'users.sqlite3'

def db():
    fd=os.open(str(DBFILE), os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    os.chmod(DBFILE, 0o600)
    c=sqlite3.connect(str(DBFILE),timeout=10)
    c.execute('CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY COLLATE NOCASE, salt TEXT NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ("admin","worker")))')
    c.execute('CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, expires REAL NOT NULL)')
    return c

def hash_password(password,salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 310000).hex()

def valid_user(name,password):
    return isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9_.-]{3,32}',name) and isinstance(password,str) and 10<=len(password)<=256


def load_key():
    try:
        for line in KEYFILE.read_text().splitlines():
            if line.startswith('ODDS_API_KEY='):
                return line.split('=', 1)[1].strip()
    except OSError:
        pass
    return os.environ.get('ODDS_API_KEY', '')

# One local feed request shared by all logged-in browsers. Failures are never
# returned as successful cached quotes. This cache does not imply bookmaker freshness.
_feed_lock = threading.Lock()
_feed_state = {'checked_monotonic': 0.0, 'result': None, 'status': 503}
FEED_INTERVAL = 10.0

def cached_betburger(fetch, clock=time.monotonic):
    with _feed_lock:
        now=clock()
        if _feed_state['result'] is not None and now-_feed_state['checked_monotonic'] < FEED_INTERVAL:
            return _feed_state['status'], dict(_feed_state['result'], cached=True)
        start=clock()
        status,result=fetch()
        elapsed=max(0,round((clock()-start)*1000))
        result=dict(result, request_ms=elapsed,
                    received_at=datetime.now(timezone.utc).isoformat(), cached=False)
        _feed_state.update(checked_monotonic=clock(), status=status, result=result)
        return status,result

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC), **kwargs)
    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path not in ('/api/odds-proxy', '/api/betburger-live', '/api/session', '/api/users', '/api/bookmakers', '/api/tasks', '/api/bonuses'):
            return super().do_GET()
        # Reject remote browser origins and DNS-rebinding Host headers.
        host = self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        if host not in ('localhost:8765', '127.0.0.1:8765') or (origin and origin not in ('http://localhost:8765', 'http://127.0.0.1:8765')):
            self.send_error(403); return
        if parsed.path == '/api/session':
            with db() as conn:
                initialized = bool(conn.execute('SELECT 1 FROM users LIMIT 1').fetchone())
            user = self.current_user()
            return self.json_reply(200, {'initialized':initialized, 'user':user})
        user = self.current_user()
        if not user:
            return self.json_reply(401, {'error':'Prijavite se.'})
        if parsed.path in ('/api/bookmakers','/api/tasks','/api/bonuses'):
            with db() as conn:
                status, result = features.get(conn,parsed.path,user)
            return self.json_reply(status,result)
        if parsed.path == '/api/users':
            if user['role'] != 'admin': return self.json_reply(403, {'error':'Samo administrator.'})
            with db() as conn:
                users=[{'username':row[0],'role':row[1]} for row in conn.execute('SELECT username,role FROM users ORDER BY username')]
            return self.json_reply(200, {'users':users})
        if parsed.path == '/api/odds-proxy' and user['role'] != 'admin':
            return self.json_reply(403, {'error':'Samo administrator.'})
        q = urllib.parse.parse_qs(parsed.query)
        if parsed.path == '/api/betburger-live':
            status, result = cached_betburger(self.fetch_betburger)
            return self.json_reply(status, result)
        sport = q.get('sport', [''])[0]
        regions = q.get('regions', ['eu'])[0].split(',')
        markets = q.get('markets', ['h2h'])[0].split(',')
        if sport != 'sports' and not re.fullmatch(r'[a-z0-9_]+', sport):
            self.send_error(400); return
        if not all(v in ('eu','uk','us','au') for v in regions) or not all(v in ('h2h','spreads','totals') for v in markets):
            self.send_error(400); return
        key = load_key()
        if not key:
            self.send_error(503, 'API key missing'); return
        url = 'https://api.the-odds-api.com/v4/sports/'
        params = {'apiKey': key}
        if sport != 'sports':
            url += sport + '/odds/'
            params.update(regions=','.join(regions), markets=','.join(markets), oddsFormat='decimal', includeLinks='true')
        try:
            with urllib.request.urlopen(url + '?' + urllib.parse.urlencode(params), timeout=20) as response:
                data = response.read(); status = response.status
                headers = {h: response.headers.get(h) for h in ('x-requests-remaining','x-requests-used','x-requests-last')}
        except urllib.error.HTTPError as exc:
            status = exc.code; data = exc.read(); headers = {}
        except (OSError, urllib.error.URLError):
            status = 502; data = json.dumps({'message':'Nije moguće povezati se sa dobavljačem kvota.'}).encode(); headers = {}
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        for h, value in headers.items():
            if value is not None: self.send_header(h, value)
        self.end_headers(); self.wfile.write(data)

    def current_user(self):
        try:
            cookies=SimpleCookie(); cookies.load(self.headers.get('Cookie',''))
            token=cookies['sb_session'].value
            if not re.fullmatch(r'[A-Za-z0-9_-]{40,100}',token): return None
            token_hash=hashlib.sha256(token.encode()).hexdigest()
            with db() as conn:
                row=conn.execute('SELECT u.username,u.role FROM sessions s JOIN users u ON u.username=s.username WHERE s.token_hash=? AND s.expires>?', (token_hash,time.time())).fetchone()
            return {'username':row[0],'role':row[1]} if row else None
        except (KeyError,ValueError): return None

    def do_POST(self):
        host=self.headers.get('Host',''); origin=self.headers.get('Origin','')
        if host not in ('localhost:8765','127.0.0.1:8765') or origin != 'http://'+host:
            return self.json_reply(403, {'error':'Zahtev nije iz ove aplikacije.'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length<=0 or length>20000: raise ValueError()
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict): raise ValueError()
        except (ValueError,TypeError): return self.json_reply(400, {'error':'Neispravan zahtev.'})
        path=urllib.parse.urlsplit(self.path).path
        if path in ('/api/setup','/api/login'):
            username=data.get('username');password=data.get('password')
            if not valid_user(username,password): return self.json_reply(400, {'error':'Korisničko ime: 3–32 slova, cifre ili ._-; lozinka najmanje 10 znakova.'})
            with db() as conn:
                if path == '/api/setup':
                    conn.execute('BEGIN IMMEDIATE')
                    if conn.execute('SELECT 1 FROM users LIMIT 1').fetchone(): return self.json_reply(409, {'error':'Admin je već kreiran.'})
                    salt=secrets.token_hex(16)
                    conn.execute('INSERT INTO users VALUES (?,?,?,?)',(username,salt,hash_password(password,salt),'admin'))
                    role='admin'
                else:
                    row=conn.execute('SELECT username,salt,password_hash,role FROM users WHERE username=?',(username,)).fetchone()
                    if not row or not hmac.compare_digest(hash_password(password,row[1]),row[2]):
                        return self.json_reply(401, {'error':'Pogrešno korisničko ime ili lozinka.'})
                    username,role=row[0],row[3]
                token=secrets.token_urlsafe(32)
                conn.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
                conn.execute('INSERT INTO sessions VALUES (?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),username,time.time()+28800))
                conn.commit()
            return self.json_reply(200, {'user':{'username':username,'role':role}},cookie='sb_session='+token+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800')
        user=self.current_user()
        if not user: return self.json_reply(401, {'error':'Prijavite se.'})
        if path == '/api/logout':
            cookies=SimpleCookie(); cookies.load(self.headers.get('Cookie',''))
            token=cookies['sb_session'].value
            with db() as conn: conn.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),)); conn.commit()
            return self.json_reply(200, {'ok':True},cookie='sb_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
        if path in ('/api/bookmakers','/api/tasks','/api/bonuses'):
            with db() as conn:
                status, result = features.post(conn,path,user,data)
            return self.json_reply(status,result)
        if path == '/api/users':
            if user['role']!='admin': return self.json_reply(403, {'error':'Samo administrator može da upravlja nalozima.'})
            username=data.get('username');role=data.get('role'); password=data.get('password')
            if role not in ('admin','worker') or not isinstance(username,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{3,32}',username): return self.json_reply(400, {'error':'Neispravno korisničko ime ili uloga.'})
            with db() as conn:
                conn.execute('BEGIN IMMEDIATE')
                existing=conn.execute('SELECT username,role FROM users WHERE username=?',(username,)).fetchone()
                if existing:
                    if existing[1]=='admin' and role!='admin' and conn.execute("SELECT count(*) FROM users WHERE role='admin'").fetchone()[0]<=1:
                        return self.json_reply(400, {'error':'Poslednji administrator ne može izgubiti ulogu.'})
                    conn.execute('UPDATE users SET role=? WHERE username=?',(role,username))
                else:
                    if not valid_user(username,password): return self.json_reply(400, {'error':'Za novi nalog potrebna je lozinka od najmanje 10 znakova.'})
                    salt=secrets.token_hex(16);conn.execute('INSERT INTO users VALUES (?,?,?,?)',(username,salt,hash_password(password,salt),role))
                conn.commit()
            return self.json_reply(200, {'ok':True})
        return self.json_reply(404, {'error':'Nepostojeći zahtev.'})

    def fetch_betburger(self):
        secrets = {}
        try:
            for line in KEYFILE.read_text().splitlines():
                if '=' in line:
                    k, v = line.split('=', 1); secrets[k.strip()] = v.strip()
        except OSError:
            pass
        token = secrets.get('BETBURGER_API_TOKEN') or os.environ.get('BETBURGER_API_TOKEN', '')
        filter_id = secrets.get('BETBURGER_LIVE_FILTER') or os.environ.get('BETBURGER_LIVE_FILTER', '')
        if not token or not re.fullmatch(r'[0-9]+', filter_id):
            return 503, {'error':'BetBurger token ili live filter nisu podešeni.'}
        body = urllib.parse.urlencode({'access_token':token, 'search_filter[]':filter_id, 'per_page':20}).encode()
        req = urllib.request.Request('https://rest-api-lv.betburger.com/api/v1/arbs/bot_pro_search', data=body, headers={'Content-Type':'application/x-www-form-urlencoded', 'Accept':'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=12) as response:
                raw=json.loads(response.read())
            return 200, {'source':'BetBurger','signals':normalize_response(raw)}
        except urllib.error.HTTPError as exc:
            return exc.code, {'error':'BetBurger je odbio zahtev (HTTP %s). Moguć istekao token, ograničenje ili pretplata.' % exc.code}
        except (OSError, ValueError, TypeError):
            return 502, {'error':'BetBurger nije dostupan ili je vratio nepoznat format.'}

    def json_reply(self, status, value, cookie=None):
        data=json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        if cookie: self.send_header('Set-Cookie',cookie)
        self.send_header('Content-Length',str(len(data)))
        self.end_headers(); self.wfile.write(data)

if __name__ == '__main__':
    print('Surebet Lab: http://localhost:8765/surebet-lab.html', flush=True)
    http.server.ThreadingHTTPServer(('127.0.0.1', 8765), Handler).serve_forever()
