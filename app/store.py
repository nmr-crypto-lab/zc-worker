"""App data for the user-facing service: users, invites, job ownership, file ownership, projects, events.

Local SQLite on the volume for the closed test. The schema is plain SQL that maps 1:1 onto Postgres tables
(Supabase later): text ids (UUID), REAL unix timestamps, no SQLite-only features. Code outside this module
never touches SQL, so moving to Supabase means re-implementing these functions, not the callers.

File keys are paths relative to <volume>/zc/output (e.g. "image/abc_00001_.png"); the same keys become the
object keys in R2 later.
"""
import os, secrets, sqlite3, threading, time, uuid

import zc_paths as P

DB = os.environ.get("ZC_APP_DB") or f"{P.STATE}/app.db"
_lock = threading.Lock()
_conn = None

SCHEMA = """
create table if not exists users (
  id text primary key, name text not null, role text not null default 'user',
  daily_limit integer not null default 60, disabled integer not null default 0, created real not null);
create table if not exists invites (
  token text primary key, user_id text not null references users(id), created real not null, used_at real);
create table if not exists jobs (
  id text primary key, user_id text references users(id), family text, status text,
  created real not null, finished real);
create index if not exists jobs_user_created on jobs(user_id, created);
create table if not exists files (
  key text primary key, user_id text not null references users(id), job_id text, kind text,
  created real not null, deleted real, adult integer not null default 0);
create index if not exists files_user_created on files(user_id, created);
create table if not exists projects (
  slug text primary key, user_id text not null references users(id), created real not null);
create table if not exists events (
  id integer primary key autoincrement, ts real not null, user_id text, kind text not null, data text);
create table if not exists meta (k text primary key, v text);
create table if not exists user_flags (user_id text not null, k text not null, v text, primary key (user_id, k));
create table if not exists heroines (
  id text primary key, owner_id text references users(id), name text not null, age integer not null,
  style text not null, vibe text, look text not null, story text, tags text, anchor text,
  created real not null, deleted real);
create table if not exists heroine_items (
  key text primary key, heroine_id text not null references heroines(id), user_id text not null references users(id),
  ask text, job_id text, created real not null);
create index if not exists heroine_items_hu on heroine_items(heroine_id, user_id, created);
create table if not exists comics (
  id text primary key, user_id text not null references users(id), style text, data text not null,
  created real not null, updated real not null, deleted real);
create table if not exists comic_files (
  key text primary key, comic_id text not null references comics(id), role text not null, created real not null);
create table if not exists heroine_candidates (
  key text primary key, heroine_id text not null references heroines(id), job_id text, created real not null);
"""

OWNER_NAME = "Owner"


def _db():
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB), exist_ok=True)
        _conn = sqlite3.connect(DB, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("pragma journal_mode=wal")
        _conn.executescript(SCHEMA)
        cols = {r[1] for r in _conn.execute("pragma table_info(files)")}
        if "adult" not in cols:          # databases made before the 18+ section
            _conn.execute("alter table files add column adult integer not null default 0")
    return _conn


def _q(sql, args=(), one=False):
    with _lock:
        cur = _db().execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()]
    return (rows[0] if rows else None) if one else rows


def _x(sql, args=()):
    with _lock:
        _db().execute(sql, args)


# ------------------------------------------------------------------ users and invites
def user(uid):
    return _q("select * from users where id=?", (uid,), one=True) if uid else None


def owner():
    """the first admin; created on first use. Owns everything made before accounts existed."""
    u = _q("select * from users where role='admin' order by created limit 1", one=True)
    if not u:
        uid = create_user(OWNER_NAME, role="admin", daily_limit=100000)
        u = user(uid)
    return u


def create_user(name, role="user", daily_limit=60):
    uid = str(uuid.uuid4())
    _x("insert into users(id,name,role,daily_limit,disabled,created) values(?,?,?,?,0,?)",
       (uid, (name or "").strip()[:60] or "Tester", role if role in ("user", "admin") else "user",
        int(daily_limit), time.time()))
    return uid


def update_user(uid, **kw):
    allowed = {"name": str, "role": str, "daily_limit": int, "disabled": int}
    for k, v in kw.items():
        if k in allowed and v is not None:
            if k == "role" and v not in ("user", "admin"):
                continue
            _x(f"update users set {k}=? where id=?", (allowed[k](v), uid))


def new_invite(uid):
    """one valid invite per user: making a new link invalidates the old unused one"""
    _x("delete from invites where user_id=? and used_at is null", (uid,))
    tok = secrets.token_urlsafe(18)
    _x("insert into invites(token,user_id,created) values(?,?,?)", (tok, uid, time.time()))
    return tok


def use_invite(tok):
    """invite links stay usable (same person, new phone) until replaced; returns the user or None"""
    inv = _q("select * from invites where token=?", (tok,), one=True)
    if not inv:
        return None
    u = user(inv["user_id"])
    if not u or u["disabled"]:
        return None
    _x("update invites set used_at=coalesce(used_at, ?) where token=?", (time.time(), tok))
    return u


def users_overview():
    day = time.time() - 86400
    rows = _q("""select u.*, (select count(*) from jobs j where j.user_id=u.id) as jobs_total,
                 (select count(*) from jobs j where j.user_id=u.id and j.created>?) as jobs_24h,
                 (select count(*) from files f where f.user_id=u.id and f.deleted is null) as files,
                 (select token from invites i where i.user_id=u.id order by created desc limit 1) as invite
               from users u order by u.created""", (day,))
    return rows


# ------------------------------------------------------------------ jobs
def add_job(jid, uid, family, status, created):
    _x("insert or replace into jobs(id,user_id,family,status,created) values(?,?,?,?,?)",
       (jid, uid, family, status, created))


def finish_job(jid, status, finished):
    _x("update jobs set status=?, finished=? where id=?", (status, finished, jid))


def jobs_last_24h(uid):
    r = _q("select count(*) as n from jobs where user_id=? and created>? and status!='cancelled'",
           (uid, time.time() - 86400), one=True)
    return r["n"] if r else 0


# ------------------------------------------------------------------ files
def add_file(key, uid, job_id=None, kind=None, adult=False):
    """adult=True: made in the 18+ section; kept apart from the everyday gallery, the studio and their inputs"""
    _x("insert or ignore into files(key,user_id,job_id,kind,created,adult) values(?,?,?,?,?,?)",
       (key, uid, job_id, kind, time.time(), 1 if adult else 0))


def file_is_adult(key):
    r = _q("select adult from files where key=?", (key,), one=True)
    return bool(r and r["adult"]) or key.startswith("adult/")


def file_owner(key):
    r = _q("select user_id from files where key=? and deleted is null", (key,), one=True)
    return r["user_id"] if r else None


def files_of(uid, kind="", limit=120, adult=False):
    rows = _q("select key, kind, created from files where user_id=? and deleted is null and adult=? order by created desc limit ?",
              (uid, 1 if adult else 0, int(limit) * 2 if kind else int(limit)))
    if kind:
        rows = [r for r in rows if (r["kind"] or "").startswith(kind)][:int(limit)]
    return rows


def delete_file(key):
    _x("update files set deleted=? where key=?", (time.time(), key))


# ------------------------------------------------------------------ projects
def add_project(slug, uid):
    _x("insert or ignore into projects(slug,user_id,created) values(?,?,?)", (slug, uid, time.time()))


def project_owner(slug):
    r = _q("select user_id from projects where slug=?", (slug,), one=True)
    return r["user_id"] if r else None


def projects_of(uid):
    return {r["slug"] for r in _q("select slug from projects where user_id=?", (uid,))}


def drop_project(slug):
    _x("delete from projects where slug=?", (slug,))


# ------------------------------------------------------------------ events + one-off migration
def event(kind, uid=None, data=None):
    import json
    _x("insert into events(ts,user_id,kind,data) values(?,?,?,?)",
       (time.time(), uid, kind, json.dumps(data, ensure_ascii=False)[:2000] if data is not None else None))


def flag(k, v=None):
    if v is None:
        r = _q("select v from meta where k=?", (k,), one=True)
        return r["v"] if r else None
    _x("insert or replace into meta(k,v) values(?,?)", (k, str(v)))


def adopt_legacy(out_dir, media_ext, project_slugs):
    """first start with accounts: everything already on the volume belongs to the owner"""
    if flag("legacy_adopted"):
        return
    oid = owner()["id"]
    for root, dirs, fs in os.walk(out_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if root == out_dir and "projects" in dirs:
            dirs.remove("projects")
        for f in fs:
            if f.lower().endswith(media_ext):
                key = os.path.relpath(os.path.join(root, f), out_dir)
                kind = "video" if f.lower().endswith((".mp4", ".webm", ".mov")) else "image"
                _x("insert or ignore into files(key,user_id,kind,created) values(?,?,?,?)",
                   (key, oid, kind, os.path.getmtime(os.path.join(root, f))))
    for s in project_slugs:
        add_project(s, oid)
    flag("legacy_adopted", time.time())


# ------------------------------------------------------------------ per-user flags (e.g. confirmed 18+)
def user_flag(uid, k, v=None):
    if v is None:
        r = _q("select v from user_flags where user_id=? and k=?", (uid, k), one=True)
        return r["v"] if r else None
    _x("insert or replace into user_flags(user_id,k,v) values(?,?,?)", (uid, k, str(v)))


# ------------------------------------------------------------------ 18+ heroines
# owner_id NULL = the public catalog (made by the admin); otherwise a user's own heroine, visible only to them.
# story/tags are JSON {"en":..,"ru":..,"id":..}; look is the English appearance text used in every prompt.
import json as _json

_HCOLS = ("name", "age", "style", "vibe", "look", "story", "tags", "anchor")


def _hrow(r):
    if not r:
        return None
    for k in ("story", "tags"):
        try:
            r[k] = _json.loads(r[k]) if r[k] else {}
        except ValueError:
            r[k] = {}
    return r


def add_heroine(owner_id, name, age, style, look, vibe="", story=None, tags=None, hid=None):
    hid = hid or str(uuid.uuid4())
    _x("insert into heroines(id,owner_id,name,age,style,vibe,look,story,tags,created) values(?,?,?,?,?,?,?,?,?,?)",
       (hid, owner_id, name, int(age), style, vibe, look, _json.dumps(story or {}, ensure_ascii=False),
        _json.dumps(tags or {}, ensure_ascii=False), time.time()))
    return hid


def heroine(hid):
    return _hrow(_q("select * from heroines where id=? and deleted is null", (hid,), one=True))


def heroines_for(uid):
    """the catalog plus the user's own heroines"""
    return [_hrow(r) for r in _q("""select h.*, (select count(*) from heroine_items i where i.heroine_id=h.id and i.user_id=?) as items,
                                    (select max(created) from heroine_items i where i.heroine_id=h.id and i.user_id=?) as last_used
                                  from heroines h where h.deleted is null and (h.owner_id is null or h.owner_id=?)
                                  order by h.owner_id is not null desc, h.created""", (uid, uid, uid))]


def catalog_count():
    r = _q("select count(*) as n from heroines where owner_id is null", one=True)
    return r["n"] if r else 0


def update_heroine(hid, **kw):
    for k, v in kw.items():
        if k in _HCOLS and v is not None:
            if k in ("story", "tags"):
                v = _json.dumps(v, ensure_ascii=False)
            _x(f"update heroines set {k}=? where id=?", (v, hid))


def delete_heroine(hid):
    _x("update heroines set deleted=? where id=?", (time.time(), hid))


def is_public_anchor(key):
    """catalog portraits are visible to everyone (they are the catalog covers)"""
    return bool(_q("select 1 from heroines where anchor=? and owner_id is null and deleted is null", (key,), one=True))


def add_hero_item(key, hid, uid, ask=None, job_id=None):
    _x("insert or ignore into heroine_items(key,heroine_id,user_id,ask,job_id,created) values(?,?,?,?,?,?)",
       (key, hid, uid, ask, job_id, time.time()))


def hero_items(hid, uid, limit=60):
    return _q("""select i.key, i.ask, i.created from heroine_items i join files f on f.key=i.key and f.deleted is null
                 where i.heroine_id=? and i.user_id=? order by i.created desc limit ?""", (hid, uid, int(limit)))


def add_candidate(key, hid, job_id=None):
    _x("insert or ignore into heroine_candidates(key,heroine_id,job_id,created) values(?,?,?,?)", (key, hid, job_id, time.time()))


def candidates(hid):
    return [r["key"] for r in _q("select key from heroine_candidates where heroine_id=? order by created desc limit 12", (hid,))]


# ------------------------------------------------------------------ comics ("Tell a story")
def add_comic(uid, style, data):
    cid = uuid.uuid4().hex[:12]
    now = time.time()
    _x("insert into comics(id,user_id,style,data,created,updated) values(?,?,?,?,?,?)",
       (cid, uid, style, _json.dumps(data, ensure_ascii=False), now, now))
    return cid


def comic(cid):
    r = _q("select * from comics where id=? and deleted is null", (cid,), one=True)
    if r:
        r["data"] = _json.loads(r["data"])
    return r


def save_comic(cid, data):
    _x("update comics set data=?, updated=? where id=?", (_json.dumps(data, ensure_ascii=False), time.time(), cid))


def comics_of(uid, limit=30):
    rows = _q("select id, style, data, updated from comics where user_id=? and deleted is null order by updated desc limit ?",
              (uid, int(limit)))
    for r in rows:
        r["title"] = (_json.loads(r.pop("data")) or {}).get("title", "")
    return rows


def delete_comic(cid):
    _x("update comics set deleted=? where id=?", (time.time(), cid))


def add_comic_file(key, cid, role):
    _x("insert or ignore into comic_files(key,comic_id,role,created) values(?,?,?,?)", (key, cid, role, time.time()))


def drop_comic_role(cid, role):
    _x("delete from comic_files where comic_id=? and role=?", (cid, role))


def comic_files(cid):
    """role -> [keys, newest first]"""
    out = {}
    for r in _q("select key, role from comic_files where comic_id=? order by created desc", (cid,)):
        out.setdefault(r["role"], []).append(r["key"])
    return out
