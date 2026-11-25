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
  created real not null, deleted real);
create index if not exists files_user_created on files(user_id, created);
create table if not exists projects (
  slug text primary key, user_id text not null references users(id), created real not null);
create table if not exists events (
  id integer primary key autoincrement, ts real not null, user_id text, kind text not null, data text);
create table if not exists meta (k text primary key, v text);
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
def add_file(key, uid, job_id=None, kind=None):
    _x("insert or ignore into files(key,user_id,job_id,kind,created) values(?,?,?,?,?)",
       (key, uid, job_id, kind, time.time()))


def file_owner(key):
    r = _q("select user_id from files where key=? and deleted is null", (key,), one=True)
    return r["user_id"] if r else None


def files_of(uid, kind="", limit=120):
    rows = _q("select key, kind, created from files where user_id=? and deleted is null order by created desc limit ?",
              (uid, int(limit) * 2 if kind else int(limit)))
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
