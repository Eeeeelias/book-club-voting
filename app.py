import sqlite3, time, os, csv, io, re, sys, math
from datetime import timedelta
from functools import wraps
from flask import Flask, request, session, jsonify, send_file, g, Response
from werkzeug.exceptions import HTTPException
from werkzeug.security import generate_password_hash, check_password_hash
from flask_limiter import Limiter

# ---- required configuration: refuse to start with missing/weak secrets ----
SECRET = os.environ.get('SECRET', '')
ADMIN_PW = os.environ.get('ADMIN_PASSWORD', '')
if len(SECRET) < 32 or SECRET.startswith(('replace-with', 'change-me')):
    sys.exit('SECRET must be a random string of at least 32 characters (e.g. `openssl rand -hex 32`)')
if len(ADMIN_PW) < 10 or ADMIN_PW.startswith('replace-with'):
    sys.exit('ADMIN_PASSWORD must be set to at least 10 characters')
COOKIE_SECURE = os.environ.get('COOKIE_SECURE', '1') != '0'  # set COOKIE_SECURE=0 only for plain-http access
MAX_USERS = int(os.environ.get('MAX_USERS', '20'))  # includes the admin account

app = Flask(__name__)
app.config.update(SECRET_KEY=SECRET, MAX_CONTENT_LENGTH=64 * 1024,
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=COOKIE_SECURE,
                  PERMANENT_SESSION_LIFETIME=timedelta(days=14))
DB = os.environ.get('DB_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'books.db'))
PH = ['suggest', 'veto', 'rank', 'result']
F = ['name', 'author', 'year', 'pages', 'genre', 'description']
LIM = dict(name=200, author=200, genre=100, description=2000)
USER_RE = re.compile(r'[\w .\-]{2,32}')
DEF = dict(phase='suggest', max_s='3', max_v='1', method='borda',
           dur_suggest='', dur_veto='', dur_rank='',
           head_suggest='', head_veto='', head_rank='', head_result='', failed='0', bonus='0')


def client_ip():
    # Only trustworthy because the container is reachable solely through the Cloudflare tunnel
    return request.headers.get('CF-Connecting-IP') or request.remote_addr


limiter = Limiter(client_ip, app=app, default_limits=['60 per minute', '600 per hour'], storage_uri='memory://')


def err(msg, code=200): return jsonify(error=msg), code


@app.errorhandler(HTTPException)
def http_err(e):
    msg = {413: 'Request too large', 429: 'Too many requests, slow down and try again in a bit'}.get(e.code, e.description)
    return jsonify(error=msg), e.code


@app.errorhandler(Exception)
def any_err(e):
    app.logger.exception(e)
    return jsonify(error='Something went wrong'), 500


@app.after_request
def sec_headers(r):
    r.headers['Content-Security-Policy'] = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; "
                                            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    r.headers['X-Frame-Options'] = 'DENY'
    r.headers['X-Content-Type-Options'] = 'nosniff'
    r.headers['Referrer-Policy'] = 'no-referrer'
    if COOKIE_SECURE: r.headers['Strict-Transport-Security'] = 'max-age=31536000'
    if request.path.startswith('/api/'): r.headers['Cache-Control'] = 'no-store'
    return r


def db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close(e):
    d = g.pop('db', None)
    if d: d.close()


def q(sql, a=(), one=False):
    r = db().execute(sql, a).fetchall()
    db().commit()
    return (r[0] if r else None) if one else r


def init():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    c.executescript('''
    create table if not exists users(id integer primary key, username text unique, password text, is_admin int default 0, participates int default 1, sv int default 0);
    create table if not exists settings(k text primary key, v text);
    create table if not exists books(id integer primary key, user_id int, name, author, year, pages, genre, description);
    create table if not exists vetoes(user_id int, book_id int);
    create table if not exists done(user_id int, phase text, primary key(user_id, phase));
    create table if not exists ranks(user_id int, book_id int, pos int);
    create table if not exists reads(user_id int, book_id int);
    create view if not exists removed as select book_id from vetoes union select book_id from reads;''')
    if 'sv' not in [r[1] for r in c.execute('pragma table_info(users)')]:  # session version, bumped on password change
        c.execute('alter table users add column sv int default 0')
    for k, v in DEF.items(): c.execute('insert or ignore into settings values(?,?)', (k, v))
    c.execute("insert or ignore into settings values('start',?)", (str(time.time()),))
    # the admin password always comes from the environment; changing it there also logs out old admin sessions
    row = c.execute("select password from users where username='admin'").fetchone()
    if not row:
        c.execute("insert into users(username,password,is_admin,participates,sv) values('admin',?,1,0,0)", (generate_password_hash(ADMIN_PW),))
    elif not check_password_hash(row[0] or '', ADMIN_PW):
        c.execute("update users set password=?, is_admin=1, sv=sv+1 where username='admin'", (generate_password_hash(ADMIN_PW),))
    c.commit(); c.close()


def S(): return {r['k']: r['v'] for r in q('select * from settings')}
def setv(k, v): q('insert or replace into settings values(?,?)', (k, str(v)))


def deadline(s, ph):
    """When the given step ends: start + its own timer + any time carried over from the previous step (None = no timer)."""
    d = s.get('dur_' + ph)
    return float(s['start']) + float(d) * 3600 + float(s.get('bonus') or 0) if d else None


def advance():
    s = S(); ph = s['phase']; i = PH.index(ph)
    if i < 3:
        nxt = PH[i + 1]
        left = q('select count(*) c from books where id not in (select book_id from removed)', one=True)['c']
        if nxt in ('veto', 'rank') and left == 0: nxt = 'result'; setv('failed', '1')  # nothing left: the vote fails
        dl = deadline(s, ph)  # time this step had left (incl. what it inherited) is handed to the next step
        carry = max(0.0, dl - time.time()) if dl and s.get('dur_' + nxt) else 0.0
        setv('phase', nxt); setv('start', time.time()); setv('bonus', carry)


def tick():
    while True:
        s = S(); ph = s['phase']
        if ph == 'result': return
        dl = deadline(s, ph)
        timeup = dl is not None and time.time() >= dl
        tot = q('select count(*) c from users where participates=1', one=True)['c']
        n = q('select count(*) c from done where phase=? and user_id in (select id from users where participates=1)', (ph,), one=True)['c']
        if timeup or (tot and n >= tot): advance()
        else: return


def me():
    uid = session.get('uid')
    if uid is None: return None
    u = q('select * from users where id=?', (uid,), one=True)
    if u and u['sv'] == session.get('sv'): return u
    session.clear(); return None


def admin(f):
    @wraps(f)
    def w(*a, **k):
        u = me()
        if not u or not u['is_admin']: return err('Admin only', 403)
        return f(*a, **k)
    return w


def body():
    j = request.get_json(silent=True)  # requires a JSON content type, which also blocks cross-site form posts
    return j if isinstance(j, dict) else {}


def clean_book(b):
    """Validate one book; returns (list of values in F order, None) or (None, error message)."""
    if not isinstance(b, dict): return None, 'Invalid suggestion'
    v = []
    for f in F:
        x = b.get(f)
        x = '' if x is None or isinstance(x, (dict, list)) else str(x).strip()
        if not x: return None, 'Fill in every field of a suggestion (or leave it fully empty)'
        if f in LIM and len(x) > LIM[f]: return None, f'{f.capitalize()} is too long (max {LIM[f]} characters)'
        if f == 'year' and not re.fullmatch(r'-?\d{1,4}', x): return None, 'Year must be a number (up to 4 digits)'
        if f == 'pages' and not re.fullmatch(r'\d{1,5}', x): return None, 'Pages must be a number (up to 5 digits)'
        v.append(str(int(x)) if f in ('year', 'pages') else x)
    return v, None


def clean_setting(k, v):
    """Returns the cleaned value as a string, or None if invalid."""
    v = '' if v is None or isinstance(v, (dict, list)) else str(v).strip()
    if k in ('max_s', 'max_v'): return str(int(v)) if re.fullmatch(r'\d{1,2}', v) else None
    if k == 'method': return v if v in ('irv', 'schulze', 'borda') else None
    if k.startswith('dur_'):
        if v == '': return ''
        try: f = float(v)
        except ValueError: return None
        return str(f) if 0 <= f <= 24 * 365 and math.isfinite(f) else None
    if k.startswith('head_'): return v if len(v) <= 300 else None
    return None


def book_dict(r, names=False):
    d = {k: r[k] for k in ['id'] + F}
    if names: d['by'] = q('select username from users where id=?', (r['user_id'],), one=True)['username']
    return d


def book_extra(r):
    d = book_dict(r)  # read_by is public (names); who vetoed is never exposed
    d['read_by'] = [x['username'] for x in q('select username from users where id in (select user_id from reads where book_id=?) order by username', (r['id'],))]
    return d


def compute(method):
    ids = [r['id'] for r in q('select id from books where id not in (select book_id from removed) order by id')]
    bal = {}
    for r in q('select user_id,book_id,pos from ranks'):
        if r['book_id'] in ids: bal.setdefault(r['user_id'], {})[r['book_id']] = r['pos']
    B = list(bal.values())  # each ballot: {book_id: rank 1-10}, lower = better, ties allowed
    n = len(ids)
    borda = {b: 0.0 for b in ids}  # per voter: 1 point per book ranked strictly worse, 0.5 per tie
    for bl in B:
        for a in ids:
            for b in ids:
                if a != b and a in bl and b in bl:
                    borda[a] += 1 if bl[a] < bl[b] else 0.5 if bl[a] == bl[b] else 0
    if method == 'borda':
        order = sorted(ids, key=lambda b: -borda[b]); note = lambda b: f'{borda[b]:g} points'
    elif method == 'schulze':
        d = {a: {b: 0 for b in ids} for a in ids}
        for bl in B:
            for a in ids:
                for b in ids:
                    if a != b and a in bl and b in bl and bl[a] < bl[b]: d[a][b] += 1
        p = {a: {b: (d[a][b] if d[a][b] > d[b][a] else 0) for b in ids} for a in ids}
        for k in ids:
            for i in ids:
                if i != k:
                    for j in ids:
                        if j != i and j != k: p[i][j] = max(p[i][j], min(p[i][k], p[k][j]))
        wins = {a: sum(p[a][b] > p[b][a] for b in ids if b != a) for a in ids}
        order = sorted(ids, key=lambda b: (-wins[b], -borda[b])); note = lambda b: f'beats {wins[b]} of {n - 1}'
    else:  # irv: a voter's vote goes (split equally) to their best-ranked remaining book(s)
        alive = set(ids); elim = []; rnd = {}
        while len(alive) > 1:
            cnt = {b: 0.0 for b in alive}
            for bl in B:
                av = [b for b in alive if b in bl]
                if av:
                    m = min(bl[b] for b in av); top = [b for b in av if bl[b] == m]
                    for b in top: cnt[b] += 1 / len(top)
            lo = min(alive, key=lambda b: (cnt[b], borda[b]))
            alive.discard(lo); elim.append(lo); rnd[lo] = len(elim)
        elim += list(alive)
        order = elim[::-1]; note = lambda b: 'last remaining' if b == order[0] else f'eliminated in round {rnd[b]}'
    books = {r['id']: r for r in q('select * from books')}
    return [dict(book_dict(books[b]), note=note(b), rank=i + 1) for i, b in enumerate(order)], len(B)


@app.get('/')
def index(): return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html'))


@app.post('/api/login')
@limiter.limit('10 per minute;50 per hour')
def login():
    j = body(); un = str(j.get('username') or '').strip(); pw = j.get('password')
    if not USER_RE.fullmatch(un): return err('Username must be 2-32 characters: letters, digits, spaces, . _ -')
    if not isinstance(pw, str) or not pw or len(pw) > 128: return err('Password must be 8-128 characters')
    u = q('select * from users where username=? collate nocase', (un,), one=True)
    if not u:
        if len(pw) < 8: return err('Password must be at least 8 characters')
        if q('select count(*) c from users', one=True)['c'] >= MAX_USERS: return err('Registration is closed')
        try: q('insert into users(username,password,is_admin,participates) values(?,?,0,1)', (un, generate_password_hash(pw)))  # new accounts participate by default
        except sqlite3.IntegrityError: return err('Username taken')
        u = q('select * from users where username=? collate nocase', (un,), one=True)
    elif not check_password_hash(u['password'] or '', pw): return err('Wrong password')
    session.clear(); session.permanent = True
    session['uid'] = u['id']; session['sv'] = u['sv']
    return jsonify(ok=1)


@app.post('/api/logout')
def logout(): session.clear(); return jsonify(ok=1)


@app.get('/api/state')
def state():
    tick(); u = me(); s = S()
    if not u: return jsonify(user=None)
    ph = s['phase']; dur = s.get('dur_' + ph)
    out = dict(user=dict(name=u['username'], admin=bool(u['is_admin'])), phase=ph, header=s['head_' + ph],
               max_s=int(s['max_s']), max_v=int(s['max_v']), method=s['method'],
               end=deadline(s, ph), carry=float(s['bonus']) if dur else 0, can=bool(u['participates']))
    if not u['participates'] and ph != 'result': return jsonify(out)  # non-participants (e.g. admin) can still see the result
    uid = u['id']
    out['done'] = bool(q('select 1 from done where user_id=? and phase=?', (uid, ph), one=True))
    if ph == 'suggest':
        out['mine'] = [book_dict(r) for r in q('select * from books where user_id=? order by id', (uid,))]
        # others' suggestions, without author; sorted by title so consecutive ids can't hint which ones belong together
        out['others'] = [book_dict(r) for r in q('select * from books where user_id!=? order by name collate nocase, id', (uid,))]
    elif ph == 'veto':
        out['books'] = [book_extra(r) for r in q('select * from books order by id')]
        out['vetoes'] = [r['book_id'] for r in q('select book_id from vetoes where user_id=?', (uid,))]
        out['reads'] = [r['book_id'] for r in q('select book_id from reads where user_id=?', (uid,))]
    elif ph == 'rank':
        out['books'] = [dict(book_extra(r), vetoed=bool(q('select 1 from vetoes where book_id=?', (r['id'],), one=True))) for r in q('select * from books order by id')]
        out['scores'] = {r['book_id']: r['pos'] for r in q('select book_id,pos from ranks where user_id=?', (uid,))}
    elif s['failed'] == '1': out['failed'] = True; out['result'] = []; out['ballots'] = 0
    else: out['result'], out['ballots'] = compute(s['method'])
    return jsonify(out)


def mark(uid, ph): q('insert or ignore into done values(?,?)', (uid, ph))


def guard(ph):
    tick(); u = me()
    if not u or not u['participates'] or S()['phase'] != ph: return None
    return u


@app.post('/api/suggest')
def suggest():
    u = guard('suggest')
    if not u: return err('Not allowed right now')
    bs = body().get('books', [])
    if not isinstance(bs, list): return err('Invalid request')
    rows = []
    for b in bs[:int(S()['max_s'])]:
        v, e = clean_book(b)
        if e: return err(e)
        rows.append(v)
    q('delete from books where user_id=?', (u['id'],))
    for v in rows: q('insert into books(user_id,name,author,year,pages,genre,description) values(?,?,?,?,?,?,?)', (u['id'], *v))
    mark(u['id'], 'suggest'); tick(); return jsonify(ok=1)


@app.post('/api/veto')
def veto():
    u = guard('veto')
    if not u: return err('Not allowed right now')
    j = body(); ids = j.get('ids', []); rd = j.get('reads', [])
    if not (isinstance(ids, list) and isinstance(rd, list) and all(type(i) is int for i in ids + rd)): return err('Invalid request')
    valid = {r['id'] for r in q('select id from books')}
    ids = set(ids) & valid; rd = set(rd) & valid
    if len(ids) > int(S()['max_v']): return err('Too many vetoes')
    q('delete from vetoes where user_id=?', (u['id'],)); q('delete from reads where user_id=?', (u['id'],))
    for i in ids: q('insert into vetoes values(?,?)', (u['id'], i))
    for i in rd: q('insert into reads values(?,?)', (u['id'], i))
    mark(u['id'], 'veto'); tick(); return jsonify(ok=1)


@app.post('/api/rank')
def rank():
    u = guard('rank')
    if not u: return err('Not allowed right now')
    sc = body().get('scores', {})
    ids = [r['id'] for r in q('select id from books where id not in (select book_id from removed)')]
    try: sc = {i: int(sc[str(i)]) for i in ids}
    except (KeyError, ValueError, TypeError): return err('Give every book a rank from 1 to 10')
    if any(not 1 <= v <= 10 for v in sc.values()): return err('Ranks must be between 1 and 10')
    q('delete from ranks where user_id=?', (u['id'],))
    for i, v in sc.items(): q('insert into ranks values(?,?,?)', (u['id'], i, v))
    mark(u['id'], 'rank'); tick(); return jsonify(ok=1)


@app.post('/api/skip')
def skip():
    ph = body().get('phase')  # the step the person was looking at; refused if it has already moved on
    u = guard(ph) if isinstance(ph, str) else None
    if not u: return err('Not allowed right now')
    if ph == 'suggest': q('delete from books where user_id=?', (u['id'],))
    elif ph == 'veto': q('delete from vetoes where user_id=?', (u['id'],)); q('delete from reads where user_id=?', (u['id'],))
    elif ph == 'rank': q('delete from ranks where user_id=?', (u['id'],))  # abstain: no ballot, not counted in the vote
    else: return err('Nothing to skip')
    mark(u['id'], ph); tick(); return jsonify(ok=1)


@app.get('/api/admin')
@admin
def admin_get():
    tick()
    return jsonify(settings=S(), users=[dict(id=r['id'], name=r['username'], admin=bool(r['is_admin']), p=bool(r['participates']),
                   done=[x['phase'] for x in q('select phase from done where user_id=?', (r['id'],))]) for r in q('select * from users order by id')],
                   books=[book_dict(r, True) for r in q('select * from books order by id')],
                   vetoes={r['book_id']: r['n'] for r in q('select book_id, count(*) n from vetoes group by book_id')},
                   reads={r['book_id']: r['n'] for r in q('select book_id, count(*) n from reads group by book_id')})


@app.post('/api/admin/settings')
@admin
def admin_settings():
    new = {}
    for k, v in body().items():
        if k in DEF and k not in ('phase', 'failed', 'bonus'):
            c = clean_setting(k, v)
            if c is None: return err(f'Invalid value for {k}')
            new[k] = c
    for k, v in new.items(): setv(k, v)
    return jsonify(ok=1)


@app.post('/api/admin/user')
@admin
def admin_user():
    j = body(); uid = j.get('id')
    if type(uid) is not int or not q('select 1 from users where id=?', (uid,), one=True): return err('Unknown user')
    pw = j.get('password')
    if pw is not None and pw != '' and (not isinstance(pw, str) or not 8 <= len(pw) <= 128): return err('Password must be 8-128 characters')
    if 'p' in j: q('update users set participates=? where id=?', (1 if j['p'] else 0, uid))
    if pw: q('update users set password=?, sv=sv+1 where id=?', (generate_password_hash(pw), uid))  # also logs that user out everywhere
    return jsonify(ok=1)


@app.post('/api/admin/book')
@admin
def admin_book():
    j = body(); bid = j.get('id')
    if type(bid) is not int: return err('Unknown book')
    if j.get('delete'):
        q('delete from books where id=?', (bid,)); q('delete from vetoes where book_id=?', (bid,)); q('delete from ranks where book_id=?', (bid,)); q('delete from reads where book_id=?', (bid,))
    else:
        v, e = clean_book(j)
        if e: return err(e)
        q('update books set ' + ','.join(f + '=?' for f in F) + ' where id=?', v + [bid])
    return jsonify(ok=1)


@app.post('/api/admin/phase')
@admin
def admin_phase():
    a = body().get('action')
    if a == 'advance': advance()
    elif a == 'reset':
        for t in ['books', 'vetoes', 'reads', 'done', 'ranks']: q(f'delete from {t}')
        setv('phase', 'suggest'); setv('failed', '0'); setv('bonus', '0'); setv('start', time.time())
    else: return err('Unknown action')
    return jsonify(ok=1)


def csv_safe(v):
    # neutralise spreadsheet formulas (=, +, -, @, tab, CR) so opening the CSV in Excel can't run user-supplied formulas
    s = str(v)
    return "'" + s if s.lstrip()[:1] in ('=', '+', '-', '@') or s[:1] in ('\t', '\r') else s


@app.get('/api/results.csv')
@admin
def results_csv():
    if S()['phase'] != 'result' or S()['failed'] == '1': return err('Results are not in yet', 400)
    books = q('select * from books where id not in (select book_id from removed) order by id')
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(['user'] + [csv_safe(f"{b['name']} - {b['author']} (#{b['id']})") for b in books])
    for u in q('select * from users where id in (select user_id from ranks) order by username'):
        sc = {r['book_id']: r['pos'] for r in q('select book_id,pos from ranks where user_id=?', (u['id'],))}
        w.writerow([csv_safe(u['username'])] + [sc.get(b['id'], '') for b in books])
    return Response('\ufeff' + out.getvalue(), mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename=ranks.csv'})


init()
if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000)  # dev only; the container uses gunicorn
