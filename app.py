import sqlite3, time, os, csv, io
from functools import wraps
from flask import Flask, request, session, jsonify, send_file, g, Response

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET', 'change-me')
DB = os.environ.get('DB_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'books.db'))
PH = ['suggest', 'veto', 'rank', 'result']
F = ['name', 'author', 'year', 'pages', 'genre', 'description']
DEF = dict(phase='suggest', max_s='3', max_v='1', method='borda',
           dur_suggest='', dur_veto='', dur_rank='',
           head_suggest='', head_veto='', head_rank='', head_result='', failed='0')


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
    create table if not exists users(id integer primary key, username text unique, password text, is_admin int default 0, participates int default 1);
    create table if not exists settings(k text primary key, v text);
    create table if not exists books(id integer primary key, user_id int, name, author, year, pages, genre, description);
    create table if not exists vetoes(user_id int, book_id int);
    create table if not exists done(user_id int, phase text, primary key(user_id, phase));
    create table if not exists ranks(user_id int, book_id int, pos int);
    create table if not exists reads(user_id int, book_id int);
    create view if not exists removed as select book_id from vetoes union select book_id from reads;''')
    for k, v in DEF.items(): c.execute('insert or ignore into settings values(?,?)', (k, v))
    c.execute("insert or ignore into settings values('start',?)", (str(time.time()),))
    c.execute("insert or ignore into users(username,password,is_admin,participates) values('admin','admin',1,0)")
    c.commit(); c.close()


def S(): return {r['k']: r['v'] for r in q('select * from settings')}
def setv(k, v): q('insert or replace into settings values(?,?)', (k, str(v)))


def advance():
    s = S(); i = PH.index(s['phase'])
    if i < 3:
        nxt = PH[i + 1]
        left = q('select count(*) c from books where id not in (select book_id from removed)', one=True)['c']
        if nxt in ('veto', 'rank') and left == 0: nxt = 'result'; setv('failed', '1')  # nothing left: the vote fails
        setv('phase', nxt); setv('start', time.time())


def tick():
    while True:
        s = S(); ph = s['phase']
        if ph == 'result': return
        d = s.get('dur_' + ph)
        timeup = d and time.time() >= float(s['start']) + float(d) * 3600
        tot = q('select count(*) c from users where participates=1', one=True)['c']
        n = q('select count(*) c from done where phase=? and user_id in (select id from users where participates=1)', (ph,), one=True)['c']
        if timeup or (tot and n >= tot): advance()
        else: return


def me():
    return q('select * from users where id=?', (session.get('uid'),), one=True) if 'uid' in session else None


def admin(f):
    @wraps(f)
    def w(*a, **k):
        u = me()
        if not u or not u['is_admin']: return jsonify(error='Admin only'), 403
        return f(*a, **k)
    return w


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
def login():
    j = request.json; un = (j.get('username') or '').strip(); pw = j.get('password') or ''
    if not un: return jsonify(error='Enter a username')
    u = q('select * from users where username=?', (un,), one=True)
    if not u:
        q('insert into users(username,password) values(?,?)', (un, pw))
        u = q('select * from users where username=?', (un,), one=True)
    elif u['password'] != pw: return jsonify(error='Wrong password')
    session['uid'] = u['id']; return jsonify(ok=1)


@app.post('/api/logout')
def logout(): session.clear(); return jsonify(ok=1)


@app.get('/api/state')
def state():
    tick(); u = me(); s = S()
    if not u: return jsonify(user=None)
    ph = s['phase']; dur = s.get('dur_' + ph)
    out = dict(user=dict(name=u['username'], admin=bool(u['is_admin'])), phase=ph, header=s['head_' + ph],
               max_s=int(s['max_s']), max_v=int(s['max_v']), method=s['method'],
               end=float(s['start']) + float(dur) * 3600 if dur else None, can=bool(u['participates']))
    if not u['participates'] and ph != 'result': return jsonify(out)  # non-participants (e.g. admin) can still see the result
    uid = u['id']
    out['done'] = bool(q('select 1 from done where user_id=? and phase=?', (uid, ph), one=True))
    if ph == 'suggest': out['mine'] = [book_dict(r) for r in q('select * from books where user_id=? order by id', (uid,))]
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
    if not u: return jsonify(error='Not allowed right now')
    bs = request.json.get('books', [])[:int(S()['max_s'])]; rows = []
    for b in bs:
        v = [str(b.get(f, '')).strip() for f in F]
        if not all(v): return jsonify(error='Fill in every field of a suggestion (or leave it fully empty)')
        if not (v[2].lstrip('-').isdigit() and v[3].isdigit()): return jsonify(error='Year and pages must be numbers')
        rows.append(v)
    q('delete from books where user_id=?', (u['id'],))
    for v in rows: q('insert into books(user_id,name,author,year,pages,genre,description) values(?,?,?,?,?,?,?)', (u['id'], *v))
    mark(u['id'], 'suggest'); tick(); return jsonify(ok=1)


@app.post('/api/veto')
def veto():
    u = guard('veto')
    if not u: return jsonify(error='Not allowed right now')
    ids = request.json.get('ids', []); rd = request.json.get('reads', [])
    if len(ids) > int(S()['max_v']): return jsonify(error='Too many vetoes')
    q('delete from vetoes where user_id=?', (u['id'],)); q('delete from reads where user_id=?', (u['id'],))
    for i in ids: q('insert into vetoes values(?,?)', (u['id'], int(i)))
    for i in rd: q('insert into reads values(?,?)', (u['id'], int(i)))
    mark(u['id'], 'veto'); tick(); return jsonify(ok=1)


@app.post('/api/rank')
def rank():
    u = guard('rank')
    if not u: return jsonify(error='Not allowed right now')
    sc = request.json.get('scores', {})
    ids = [r['id'] for r in q('select id from books where id not in (select book_id from removed)')]
    try: sc = {i: int(sc[str(i)]) for i in ids}
    except (KeyError, ValueError, TypeError): return jsonify(error='Give every book a rank from 1 to 10')
    if any(not 1 <= v <= 10 for v in sc.values()): return jsonify(error='Ranks must be between 1 and 10')
    q('delete from ranks where user_id=?', (u['id'],))
    for i, v in sc.items(): q('insert into ranks values(?,?,?)', (u['id'], i, v))
    mark(u['id'], 'rank'); tick(); return jsonify(ok=1)


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
    for k, v in request.json.items():
        if k in DEF and k not in ('phase', 'failed'): setv(k, v)
    return jsonify(ok=1)


@app.post('/api/admin/user')
@admin
def admin_user():
    j = request.json
    if 'p' in j: q('update users set participates=? where id=?', (int(j['p']), j['id']))
    if j.get('password'): q('update users set password=? where id=?', (j['password'], j['id']))
    return jsonify(ok=1)


@app.post('/api/admin/book')
@admin
def admin_book():
    j = request.json
    if j.get('delete'):
        q('delete from books where id=?', (j['id'],)); q('delete from vetoes where book_id=?', (j['id'],)); q('delete from ranks where book_id=?', (j['id'],)); q('delete from reads where book_id=?', (j['id'],))
    else: q('update books set ' + ','.join(f + '=?' for f in F) + ' where id=?', [j[f] for f in F] + [j['id']])
    return jsonify(ok=1)


@app.post('/api/admin/phase')
@admin
def admin_phase():
    a = request.json['action']
    if a == 'advance': advance()
    elif a == 'reset':
        for t in ['books', 'vetoes', 'reads', 'done', 'ranks']: q(f'delete from {t}')
        setv('phase', 'suggest'); setv('failed', '0'); setv('start', time.time())
    return jsonify(ok=1)


@app.get('/api/results.csv')
@admin
def results_csv():
    if S()['phase'] != 'result' or S()['failed'] == '1': return jsonify(error='Results are not in yet'), 400
    books = q('select * from books where id not in (select book_id from removed) order by id')
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(['user'] + [f"{b['name']} - {b['author']} (#{b['id']})" for b in books])
    for u in q('select * from users where id in (select user_id from ranks) order by username'):
        sc = {r['book_id']: r['pos'] for r in q('select book_id,pos from ranks where user_id=?', (u['id'],))}
        w.writerow([u['username']] + [sc.get(b['id'], '') for b in books])
    return Response('\ufeff' + out.getvalue(), mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename=ranks.csv'})


init()
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
