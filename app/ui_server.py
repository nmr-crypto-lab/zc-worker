"""ZC prototype UI: one page + small JSON API, served from the pod (port 8000).

Generation goes through handler.generate (registry -> family template -> ComfyUI -> age gate), so the UI and the
serverless worker share exactly the same code path. Jobs run one at a time on the GPU (in-process queue).

Accounts (closed test): people sign in with a personal invitation link /i/<token> made on the admin page; a signed
cookie keeps them signed in. Everyone sees only their own jobs, results and movie projects (store.py, SQLite).
The admin page (/admin) needs the admin password ($ZC_ADMIN_PASSWORD or <volume>/zc/state/admin_password.txt);
signing in with it makes you the owner account, which sees everything.
"""
import asyncio, base64, glob, hashlib, hmac, json, os, queue, re, secrets, sys, threading, time, traceback, uuid, mimetypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zc_paths as P

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

import handler as H
import registry as R
import projects as PJ
import store as ST
import adult as AD

APP_DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.realpath(f"{P.ROOT}/output")
MEDIA = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm", ".mov")
JOBS_LOG = f"{P.LOGS}/ui_jobs.jsonl"


def _password(env="ZC_UI_PASSWORD", name="ui_password.txt"):
    if os.environ.get(env):
        return os.environ[env]
    f = f"{P.STATE}/{name}"
    try:
        return open(f).read().strip()
    except OSError:
        pw = secrets.token_urlsafe(9)
        os.makedirs(P.STATE, exist_ok=True)
        open(f, "w").write(pw)
        return pw


ADMIN_PASSWORD = _password("ZC_ADMIN_PASSWORD", "admin_password.txt")
ADMIN_ONLY = ("/admin", "/api/admin/")     # owner only: testers get the user app and the studio
PUBLIC = ("/i/", "/ui/ex/", "/favicon")
SESSION_DAYS = 120


def _secret():
    f = f"{P.STATE}/session_secret.txt"
    try:
        return open(f).read().strip()
    except OSError:
        os.makedirs(P.STATE, exist_ok=True)
        s = secrets.token_urlsafe(32)
        open(f, "w").write(s)
        return s


SECRET = os.environ.get("ZC_SESSION_SECRET") or _secret()


def _sig(uid, exp):
    return hmac.new(SECRET.encode(), f"{uid}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def make_session(uid):
    exp = int(time.time()) + SESSION_DAYS * 86400
    return f"{uid}.{exp}.{_sig(uid, exp)}"


def read_session(tok):
    try:
        uid, exp, sig = (tok or "").rsplit(".", 2)
        if int(exp) < time.time() or not hmac.compare_digest(sig, _sig(uid, exp)):
            return None
    except ValueError:
        return None
    u = ST.user(uid)
    return u if u and not u["disabled"] else None


def _set_session(resp, uid):
    resp.set_cookie("zc_s", make_session(uid), max_age=SESSION_DAYS * 86400, httponly=True, secure=True, samesite="lax")


_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wishly</title><style>body{margin:0;font-family:system-ui,sans-serif;background:#f3f6fb;color:#172033;display:flex;min-height:100vh;
align-items:center;justify-content:center;padding:24px;box-sizing:border-box}div{max-width:420px;text-align:center}h1{font-size:28px;margin:0 0 12px}
p{font-size:17px;line-height:1.45;color:#4a5568;margin:8px 0}</style></head><body><div><h1>Wishly</h1>%s</div></body></html>"""
LOGIN_HTML = _PAGE % ("<p>Closed testing. Open your personal invitation link to sign in.</p>"
                      "<p>Закрытое тестирование. Откройте свою персональную ссылку-приглашение.</p>"
                      "<p>Uji tertutup. Buka tautan undangan pribadi Anda untuk masuk.</p>")
BAD_INVITE_HTML = _PAGE % ("<p>This invitation link is not valid any more. Ask for a new one.</p>"
                           "<p>Эта ссылка больше не действует. Попросите новую.</p>"
                           "<p>Tautan ini sudah tidak berlaku. Minta tautan baru.</p>")
app = FastAPI(title="ZC")


@app.middleware("http")
async def auth(request: Request, call_next):
    path = request.url.path
    if path.startswith(PUBLIC):
        return await call_next(request)
    pw = ""
    h = request.headers.get("authorization", "")
    if h.startswith("Basic "):
        try:
            pw = base64.b64decode(h[6:]).decode().partition(":")[2]
        except Exception:
            pw = ""
    key = request.query_params.get("key") or ""
    u = read_session(request.cookies.get("zc_s"))
    became_owner = False
    if (pw and secrets.compare_digest(pw, ADMIN_PASSWORD)) or (key and secrets.compare_digest(key, ADMIN_PASSWORD)):
        u, became_owner = ST.owner(), True
    if path.startswith(ADMIN_ONLY) and not (u and u["role"] == "admin"):
        # a separate realm makes the browser ask for the admin password
        return Response("admin only", status_code=401, headers={"WWW-Authenticate": 'Basic realm="ZC admin"'})
    if not u:
        if path.startswith(("/api/", "/files/", "/thumb/")):
            return JSONResponse({"detail": "sign in with your invitation link"}, status_code=401)
        return HTMLResponse(LOGIN_HTML, status_code=401)
    m = re.match(r"^/(?:api/projects|files/projects|thumb/projects)/([^/?]+)", path)
    if m and u["role"] != "admin" and ST.project_owner(m.group(1)) != u["id"]:
        return JSONResponse({"detail": "not found"}, status_code=404)
    request.state.user = u
    resp = await call_next(request)
    if became_owner:
        _set_session(resp, u["id"])
    return resp


def me(request):
    return request.state.user


def is_admin(request):
    return me(request)["role"] == "admin"


def can_file(request, key):
    """results belong to whoever made them; movie project files to the project owner; the admin sees everything"""
    if is_admin(request):
        return True
    if key.startswith("projects/"):
        return ST.project_owner(key.split("/")[1]) == me(request)["id"]
    return ST.file_owner(key) == me(request)["id"] or ST.is_public_anchor(key)


def can_job(request, job):
    return is_admin(request) or job.get("user") == me(request)["id"]


# ------------------------------------------------------------------ job queue (single GPU, one job at a time)
JOBS = {}          # id -> dict
ORDER = []         # newest last
Q = queue.Queue()
LOCK = threading.Lock()


def _save(job):
    """one line per finished job; the actual seed (also when it was random), timing and the age-gate verdict included"""
    res = job.get("result") or {}
    rec = {k: v for k, v in job.items() if k not in ("result", "progress")}
    rec.update(outputs=[o.get("path") for o in res.get("outputs", [])], seed=res.get("seed"), gen_secs=res.get("secs"),
               removed=res.get("removed"), gate=[{k: g.get(k) for k in ("ok", "faces", "min_age", "error") if k in g}
                                                 for g in (res.get("gate") or [])])
    try:
        with open(JOBS_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def _restore():
    """Reload the recent job history so the queue panel survives a server restart or a new pod.
    The local log is copied to <volume>/zc/logs every 30 s; a fresh pod continues from that copy."""
    vol = f"{P.ROOT}/logs/ui_jobs.jsonl"
    try:
        if not os.path.exists(JOBS_LOG) and os.path.exists(vol):
            os.makedirs(os.path.dirname(JOBS_LOG), exist_ok=True)
            __import__("shutil").copyfile(vol, JOBS_LOG)
        lines = open(JOBS_LOG).read().splitlines()[-500:]
    except OSError:
        return
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if "id" not in r or "input" not in r:
            continue
        outs = [{"path": p, "type": "video" if p.lower().endswith((".mp4", ".webm", ".mov")) else "image"}
                for p in (r.get("outputs") or []) if os.path.exists(os.path.join(OUT, p))]
        job = {k: r.get(k) for k in ("id", "status", "input", "created", "started", "finished", "error", "note", "user")}
        job["result"] = {"outputs": outs, "seed": r.get("seed")}
        if job["status"] in ("queued", "running"):
            job.update(status="error", error="interrupted by a server restart")
        if r["id"] not in JOBS:
            ORDER.append(r["id"])
        JOBS[r["id"]] = job
    ORDER.sort(key=lambda k: JOBS[k].get("created") or 0)
    del ORDER[:-50]


def worker():
    while True:
        jid = Q.get()
        job = JOBS[jid]
        if job["status"] != "queued":          # cancelled while waiting
            Q.task_done()
            continue
        job.update(status="running", started=time.time())
        try:
            res = H.generate(dict(job["input"]), jid, progress=lambda st, job=job: job.__setitem__("progress", st),
                             cancel=lambda job=job: job.get("cancel", False))
            if job["input"].get("_project"):
                PJ.on_done(job, res)           # final clip name 000_1.mp4 + what it was made with
            for o in res.get("outputs", []):
                o.pop("b64", None)
            job.update(status="done", result=res)
            if not res.get("outputs"):
                job["note"] = "result removed by the age filter" if res.get("removed") else "no result"
        except ValueError as e:
            job.update(status="error", error=str(e))
        except Exception as e:
            if job.get("cancel"):
                job.update(status="cancelled")
            else:
                traceback.print_exc()
                job.update(status="error", error=f"{e.__class__.__name__}: {str(e)[:300]}")
        job.pop("progress", None)
        job["finished"] = time.time()
        _save(job)
        try:
            ST.finish_job(jid, job["status"], job["finished"])
            for o in (job.get("result") or {}).get("outputs", []):
                if not o["path"].startswith("projects/"):
                    ST.add_file(o["path"], job.get("user") or ST.owner()["id"], jid, o.get("type"))
            _hero_after(job)
        except Exception:
            traceback.print_exc()
        Q.task_done()


def _hero_after(job):
    """18+ heroine jobs: file the results in her album (or as portrait candidates); run the follow-up edit step"""
    m = job["input"].get("_hero")
    if not m or job["status"] != "done":
        return
    outs = [o for o in (job.get("result") or {}).get("outputs", []) if o.get("type") == "image"]
    uid = job.get("user") or ST.owner()["id"]
    for o in outs:
        if m.get("cand"):
            ST.add_candidate(o["path"], m["id"], job["id"])
        else:
            ST.add_hero_item(o["path"], m["id"], uid, m.get("ask"), job["id"])
    then = job["input"].get("_then")
    if then and outs:
        nxt = dict(then, images=[{"path": outs[0]["path"]}], _hero=m)
        job["next"] = enqueue(nxt, uid)


_restore()
ST.owner()
try:
    ST.adopt_legacy(OUT, MEDIA, [p["slug"] for p in PJ.list_projects()])
except Exception:
    traceback.print_exc()
try:
    AD.seed_catalog(ST)
except Exception:
    traceback.print_exc()
threading.Thread(target=worker, daemon=True).start()


@app.get("/i/{tok}")
def invite_login(tok: str):
    u = ST.use_invite(tok)
    if not u:
        return HTMLResponse(BAD_INVITE_HTML, status_code=404)
    ST.event("login", u["id"])
    r = RedirectResponse("/", status_code=302)
    _set_session(r, u["id"])
    return r


@app.get("/api/me")
def whoami(request: Request):
    u = me(request)
    return {"id": u["id"], "name": u["name"], "role": u["role"], "daily_limit": u["daily_limit"],
            "used_24h": ST.jobs_last_24h(u["id"])}


# ------------------------------------------------------------------ admin: testers and invitation links
@app.get("/api/admin/users")
def admin_users():
    return ST.users_overview()


@app.post("/api/admin/users")
async def admin_user_create(request: Request):
    b = await request.json()
    uid = ST.create_user(b.get("name"), b.get("role") or "user", int(b.get("daily_limit") or 60))
    return {"id": uid, "invite": ST.new_invite(uid)}


@app.post("/api/admin/users/{uid}/invite")
def admin_user_invite(uid: str):
    if not ST.user(uid):
        raise HTTPException(404)
    return {"invite": ST.new_invite(uid)}


@app.patch("/api/admin/users/{uid}")
async def admin_user_update(uid: str, request: Request):
    if not ST.user(uid):
        raise HTTPException(404)
    b = await request.json()
    ST.update_user(uid, **{k: b.get(k) for k in ("name", "role", "daily_limit", "disabled")})
    return ST.user(uid)


# ------------------------------------------------------------------ API
@app.get("/", response_class=HTMLResponse)
def index():
    """the casual user app (by task); the full studio/admin UI lives at /admin"""
    return open(f"{APP_DIR}/ui/app.html", encoding="utf-8").read()


@app.get("/admin", response_class=HTMLResponse)
def admin():
    return open(f"{APP_DIR}/ui/index.html", encoding="utf-8").read()


@app.get("/ui/ex/{name}")
def example_file(name: str):
    """bundled examples for the user app (made with this service)"""
    if not re.fullmatch(r"[a-z0-9_-]+\.(mp4|jpg|png|webp)", name):
        raise HTTPException(404)
    # baked into the image (app/ui/ex) or kept on the volume (zc/ui_ex) so examples can change without a rebuild
    p = next((q for q in (f"{P.ROOT}/ui_ex/{name}", f"{APP_DIR}/ui/ex/{name}") if os.path.isfile(q)), None)
    if not p:
        raise HTTPException(404)
    return FileResponse(p, media_type=mimetypes.guess_type(p)[0], headers={"Cache-Control": "public, max-age=604800"})


@app.get("/api/job/{jid}")
def job_one(jid: str, request: Request):
    j = JOBS.get(jid)
    if not j or not can_job(request, j):
        raise HTTPException(404)
    d = {k: j.get(k) for k in ("id", "status", "error", "progress", "note", "next")}
    d["queue_pos"] = next((n + 1 for n, k in enumerate(k for k in ORDER if JOBS[k]["status"] == "queued") if k == jid), None)
    if j.get("result"):
        d["outputs"] = [{"path": o["path"], "type": o["type"]} for o in j["result"].get("outputs", [])]
        d["seed"] = j["result"].get("seed")
    return d


@app.post("/api/gif/{jid}/retext")
async def gif_retext(jid: str, request: Request):
    """re-render the text on a finished GIF job without the GPU (new words / position)"""
    import gif as G
    j = JOBS.get(jid)
    if not j or j["status"] != "done" or j["input"].get("family") != "gif" or not can_job(request, j):
        raise HTTPException(404, "no finished GIF with this id")
    body = await request.json()
    text = (body.get("text") or "").strip()[:80]
    v = H.prompt_violation(text)
    if v:
        raise HTTPException(400, "text refused")
    pos = body.get("text_pos") if body.get("text_pos") in ("top", "middle", "bottom") else "bottom"
    raw = sorted(glob.glob(f"{P.ROOT}/output/gif/.work/{jid}_loop*.mp4"))
    if not raw:
        raise HTTPException(404, "the raw loop is gone")
    n = int(time.time())
    outs = await asyncio.to_thread(G.finish, raw[-1], text, pos, f"{P.ROOT}/output/gif/{jid}_t{n}")
    rel = [os.path.relpath(o, f"{P.ROOT}/output") for o in outs]
    for k in rel:
        ST.add_file(k, j.get("user") or me(request)["id"], jid, "video")
    return {"outputs": [{"path": k, "type": "video"} for k in rel]}


# ------------------------------------------------------------------ 18+: heroines ("Meet someone", phase 1)
def _adult(request):
    u = me(request)
    if u["role"] != "admin" and not ST.user_flag(u["id"], "adult_ok"):
        raise HTTPException(403, "confirm that you are 18+ first")
    return u


def _hero_or_404(request, hid):
    h = ST.heroine(hid)
    u = me(request)
    if not h or (h["owner_id"] and h["owner_id"] != u["id"] and u["role"] != "admin"):
        raise HTTPException(404)
    return h


def _can_edit_hero(request, h):
    u = me(request)
    return h["owner_id"] == u["id"] or (u["role"] == "admin" and not h["owner_id"])


def _limit_ok(u, n=1):
    if u["role"] != "admin" and ST.jobs_last_24h(u["id"]) + n > u["daily_limit"]:
        ST.event("limit_hit", u["id"])
        raise HTTPException(429, f"Daily limit reached ({u['daily_limit']} per 24 h). Please try again later.")


def _hero_out(h, uid):
    return {k: h.get(k) for k in ("id", "name", "age", "style", "vibe", "story", "tags", "anchor", "items", "last_used")} | \
        {"mine": h.get("owner_id") == uid, "catalog": not h.get("owner_id")}


@app.post("/api/adult/ok")
def adult_ok(request: Request):
    u = me(request)
    if not ST.user_flag(u["id"], "adult_ok"):
        ST.user_flag(u["id"], "adult_ok", time.time())
        ST.event("adult_ok", u["id"])
    return {"ok": True}


@app.get("/api/heroines")
def heroines_list(request: Request):
    u = _adult(request)
    return [_hero_out(h, u["id"]) for h in ST.heroines_for(u["id"])]


@app.get("/api/heroines/{hid}")
def heroine_one(hid: str, request: Request):
    u = _adult(request)
    h = _hero_or_404(request, hid)
    d = _hero_out(h, u["id"])
    d["can_edit"] = _can_edit_hero(request, h)
    d["items"] = [{"path": i["key"], "ask": i["ask"], "type": "video" if i["key"].lower().endswith((".mp4", ".webm")) else "image"}
                  for i in ST.hero_items(hid, u["id"])]
    d["candidates"] = ST.candidates(hid) if d["can_edit"] else []
    d["jobs"] = [k for k in ORDER if JOBS[k]["status"] in ("queued", "running") and JOBS[k].get("user") == u["id"]
                 and (JOBS[k]["input"].get("_hero") or {}).get("id") == hid]
    return d


@app.post("/api/heroines")
async def heroine_create(request: Request):
    u = _adult(request)
    b = await request.json()
    look = AD.look_from_form(b)
    name = (b.get("name") or "").strip()[:30]
    try:
        age = int(b.get("age") or 0)
    except ValueError:
        age = 0
    if not name or not look:
        raise HTTPException(400, "name and appearance are needed")
    if age < AD.MIN_AGE:
        raise HTTPException(400, f"heroines are {AD.MIN_AGE} or older")
    if b.get("style") not in AD.STYLES:
        raise HTTPException(400, "unknown style")
    if H.prompt_violation(look) or H.prompt_violation(name):
        ST.event("prompt_refused", u["id"], {"where": "heroine"})
        raise HTTPException(400, "Refused: content involving minors is not allowed")
    _limit_ok(u, 4)
    catalog = bool(b.get("catalog")) and u["role"] == "admin"
    hid = ST.add_heroine(None if catalog else u["id"], name, age, b["style"], look, vibe=(b.get("vibe") or "")[:20])
    ST.event("heroine_new", u["id"], {"id": hid, "style": b["style"]})
    h = ST.heroine(hid)
    jobs = [enqueue(dict(inp, _hero={"id": hid, "cand": True}), u["id"]) for inp in AD.portrait_inputs(h)]
    return {"id": hid, "jobs": jobs}


@app.post("/api/heroines/{hid}/portraits")
def heroine_portraits(hid: str, request: Request):
    u = _adult(request)
    h = _hero_or_404(request, hid)
    if not _can_edit_hero(request, h):
        raise HTTPException(403)
    _limit_ok(u, 4)
    return {"jobs": [enqueue(dict(inp, _hero={"id": hid, "cand": True}), u["id"]) for inp in AD.portrait_inputs(h)]}


@app.post("/api/heroines/{hid}/anchor")
async def heroine_anchor(hid: str, request: Request):
    _adult(request)
    h = _hero_or_404(request, hid)
    if not _can_edit_hero(request, h):
        raise HTTPException(403)
    key = (await request.json()).get("path")
    if key not in ST.candidates(hid):
        raise HTTPException(400, "pick one of her portraits")
    ST.update_heroine(hid, anchor=key)
    return {"ok": True}


@app.delete("/api/heroines/{hid}")
def heroine_delete(hid: str, request: Request):
    _adult(request)
    h = _hero_or_404(request, hid)
    if not _can_edit_hero(request, h):
        raise HTTPException(403)
    ST.delete_heroine(hid)
    return {"ok": True}


@app.post("/api/heroines/{hid}/ask")
async def heroine_ask(hid: str, request: Request):
    u = _adult(request)
    h = _hero_or_404(request, hid)
    b = await request.json()
    opt = b.get("opt") or {}
    if H.prompt_violation(opt.get("place_text")):
        ST.event("prompt_refused", u["id"], {"where": "ask"})
        raise HTTPException(400, "Refused: content involving minors is not allowed")
    try:
        first, then = AD.ask_inputs(h, b.get("ask"), opt)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _limit_ok(u, 2 if then else 1)
    meta = {"id": hid, "ask": b.get("ask")}
    first = dict(first, _hero=meta)
    if then:
        first["_then"] = then
    ST.event("heroine_ask", u["id"], {"id": hid, "ask": b.get("ask")})
    return {"id": enqueue(first, u["id"])}


@app.get("/api/models")
def models():
    return H.models_info()


def enqueue(inp, uid):
    jid = uuid.uuid4().hex[:12]
    with LOCK:
        JOBS[jid] = {"id": jid, "status": "queued", "input": inp, "created": time.time(), "user": uid}
        ORDER.append(jid)
    ST.add_job(jid, uid, inp.get("family"), "queued", JOBS[jid]["created"])
    Q.put(jid)
    return jid


@app.post("/api/generate")
async def generate(request: Request):
    inp = await request.json()
    for k in ("out_prefix", "_project", "_hero", "_then"):   # internal keys, set only by the server itself
        inp.pop(k, None)
    if inp.get("family") not in H.models_info()["families"]:
        raise HTTPException(400, "unknown family")
    v = H.prompt_violation(inp.get("prompt"))
    if v:
        ST.event("prompt_refused", me(request)["id"], {"family": inp.get("family")})
        raise HTTPException(400, "Prompt refused: content involving minors is not allowed")
    for it in inp.get("images") or []:
        if "path" not in it or not can_file(request, it["path"]):
            raise HTTPException(403, "you can only use your own pictures")
    u = me(request)
    if u["role"] != "admin" and ST.jobs_last_24h(u["id"]) >= u["daily_limit"]:
        ST.event("limit_hit", u["id"])
        raise HTTPException(429, f"Daily limit reached ({u['daily_limit']} per 24 h). Please try again later.")
    return {"id": enqueue(inp, u["id"])}


def _cancel(job):
    if job["status"] == "queued":
        job.update(status="cancelled", finished=time.time())
        _save(job)
        return True
    if job["status"] == "running":
        job["cancel"] = True           # the worker interrupts ComfyUI and marks it cancelled
        return True
    return False


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str, request: Request):
    job = JOBS.get(jid)
    if not job or not can_job(request, job):
        raise HTTPException(404, "no such job")
    with LOCK:
        if not _cancel(job):
            raise HTTPException(409, f"job is already {job['status']}")
    return {"ok": True}


@app.post("/api/jobs/cancel_queued")
def cancel_queued(request: Request):
    with LOCK:
        n = sum(_cancel(j) for j in list(JOBS.values()) if j["status"] == "queued" and can_job(request, j))
    return {"cancelled": n}


@app.get("/api/jobs")
def jobs(request: Request, all: int = 0):
    """your own jobs; the admin page asks for everybody's (all=1)"""
    now = time.time()
    out = []
    mine = (lambda j: True) if (all and is_admin(request)) else (lambda j: j.get("user") == me(request)["id"])
    active = [k for k in ORDER if JOBS[k]["status"] in ("queued", "running") and mine(JOBS[k])]
    recent = [k for k in ORDER if JOBS[k]["status"] not in ("queued", "running") and mine(JOBS[k])][-30:]
    qpos = {k: n + 1 for n, k in enumerate(k for k in ORDER if JOBS[k]["status"] == "queued")}
    for jid in reversed(sorted(set(active + recent), key=ORDER.index)):
        j = JOBS[jid]
        d = {k: j.get(k) for k in ("id", "status", "error", "note", "progress", "cancel")}
        d["family"] = j["input"].get("family")
        pj = j["input"].get("_project")
        if pj:
            d["label"] = f"{pj['slug']} · {PJ.cname(pj['i'], pj['v'])}"
        d["prompt"] = (j["input"].get("prompt") or "")[:120]
        d["queue_pos"] = qpos.get(jid)
        t0 = j.get("started") or j["created"]
        d["secs"] = round((j.get("finished") or now) - t0)
        if j.get("result"):
            d["outputs"] = [{"path": o["path"], "type": o["type"]} for o in j["result"].get("outputs", [])]
            d["seed"] = j["result"].get("seed")
        out.append(d)
    return out


@app.get("/api/gallery")
def gallery(request: Request, limit: int = 120, kind: str = "", all: int = 0):
    if not (all and is_admin(request)):          # your own results, newest first
        out = []
        for r in ST.files_of(me(request)["id"], kind, limit):
            p = os.path.join(OUT, r["key"])
            try:
                out.append({"path": r["key"], "type": r["kind"] or "image", "mtime": int(os.path.getmtime(p))})
            except OSError:
                continue
        return out
    items = []
    for root, dirs, files in os.walk(OUT):
        dirs[:] = [x for x in dirs if not x.startswith(".")]      # skip .ipynb_checkpoints etc.
        if root == OUT and "projects" in dirs:                    # projects have their own page
            dirs.remove("projects")
        for f in files:
            if f.lower().endswith(MEDIA):
                p = os.path.join(root, f)
                try:
                    m = os.path.getmtime(p)
                except OSError:
                    continue
                rel = os.path.relpath(p, OUT)
                t = "video" if f.lower().endswith((".mp4", ".webm", ".mov")) or "/video/" in "/" + rel else "image"
                if f.lower().endswith((".webp", ".gif")) and rel.startswith("video"):
                    t = "video_anim"
                items.append((m, rel, t))
    items.sort(reverse=True)
    if kind:
        items = [i for i in items if i[2].startswith(kind)]
    return [{"path": r, "type": t, "mtime": int(m)} for m, r, t in items[:limit]]


def _media(path):
    p = os.path.realpath(os.path.join(OUT, path))
    if not p.startswith(OUT + os.sep) or not os.path.isfile(p):
        raise HTTPException(404)
    return p


@app.get("/files/{path:path}")
def files(path: str, request: Request, dl: int = 0):
    if not can_file(request, path):
        raise HTTPException(404)
    p = _media(path)
    h = {"Cache-Control": "private, max-age=86400"}
    if dl:
        h["Content-Disposition"] = f'attachment; filename="{os.path.basename(p)}"'
    return FileResponse(p, media_type=mimetypes.guess_type(p)[0] or "application/octet-stream", headers=h)


THUMBS = os.environ.get("ZC_THUMBS", "/root/zcthumbs")
THUMB_PX = 360


@app.get("/thumb/{path:path}")
def thumb(path: str, request: Request):
    """Small JPEG preview for the gallery (a frame for videos). Cached on local disk, rebuilt if the file changes.
    Phones would otherwise download and decode every full PNG / MP4 in the grid."""
    import subprocess
    from PIL import Image
    if not can_file(request, path):
        raise HTTPException(404)
    p = _media(path)
    t = os.path.join(THUMBS, os.path.relpath(p, OUT)) + ".jpg"
    if not (os.path.exists(t) and os.path.getmtime(t) >= os.path.getmtime(p)):
        os.makedirs(os.path.dirname(t), exist_ok=True)
        tmp = t + ".tmp.jpg"
        try:
            if p.lower().endswith((".mp4", ".webm", ".mov")):
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "0.5", "-i", p, "-frames:v", "1",
                                "-vf", f"scale={THUMB_PX}:-2", "-q:v", "5", tmp], check=True, timeout=60)
            else:
                im = Image.open(p)
                im.seek(0)
                im = im.convert("RGB")
                im.thumbnail((THUMB_PX, THUMB_PX * 2))
                im.save(tmp, "JPEG", quality=80)
            os.replace(tmp, t)
        except Exception:
            raise HTTPException(404)
    return FileResponse(t, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


@app.delete("/api/files/{path:path}")
def delete_file(path: str, request: Request):
    """Permanently delete a result (the UI asks for a second tap first)."""
    if path.startswith("projects/"):
        raise HTTPException(403, "project files are managed on the project page")
    if not can_file(request, path):
        raise HTTPException(404)
    p = os.path.realpath(os.path.join(OUT, path))
    if not p.startswith(OUT + os.sep) or not os.path.isfile(p) or not p.lower().endswith(MEDIA):
        raise HTTPException(404)
    os.remove(p)
    ST.delete_file(path)
    with open(JOBS_LOG, "a") as f:
        f.write(json.dumps({"deleted": path, "ts": time.time()}) + "\n")
    return {"ok": True}


# ------------------------------------------------------------------ Create Movie projects
def _pj(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except PJ.ProjectError as e:
        raise HTTPException(400, str(e))


@app.get("/ui/movie.js")
def movie_js():
    return Response(open(f"{APP_DIR}/ui/movie.js", encoding="utf-8").read(), media_type="application/javascript")


@app.get("/api/projects")
def projects_list(request: Request):
    ps = PJ.list_projects()
    if is_admin(request):
        return ps
    own = ST.projects_of(me(request)["id"])
    return [p for p in ps if p["slug"] in own]


@app.post("/api/projects")
async def projects_create(request: Request):
    slug = _pj(PJ.create, (await request.json()).get("title"))
    ST.add_project(slug, me(request)["id"])
    return {"slug": slug}


@app.get("/api/projects/{slug}")
def project_get(slug: str):
    return _pj(PJ.state, slug, sorted(JOBS.values(), key=lambda j: j.get("created") or 0))


@app.delete("/api/projects/{slug}")
def project_delete(slug: str):
    if any((j["input"].get("_project") or {}).get("slug") == slug and j["status"] in ("queued", "running")
           for j in JOBS.values()):
        raise HTTPException(400, "the project still has clips in the queue")
    _pj(PJ.delete_project, slug)
    ST.drop_project(slug)
    return {"ok": True}


@app.post("/api/projects/{slug}/refs")
async def project_add_ref(slug: str, request: Request):
    b = await request.json()
    if not can_file(request, b.get("path") or ""):
        raise HTTPException(403, "you can only use your own pictures")
    return {"name": _pj(PJ.add_ref, slug, b.get("name"), b.get("path"), b.get("desc", ""))}


@app.patch("/api/projects/{slug}/refs/{name}")
async def project_ref_desc(slug: str, name: str, request: Request):
    _pj(PJ.set_ref_desc, slug, name, (await request.json()).get("desc", ""))
    return {"ok": True}


@app.delete("/api/projects/{slug}/refs/{name}")
def project_del_ref(slug: str, name: str):
    _pj(PJ.delete_ref, slug, name)
    return {"ok": True}


@app.put("/api/projects/{slug}/plan")
async def project_plan(slug: str, request: Request):
    b = await request.json()
    return {"warnings": _pj(PJ.save_plan, slug, b.get("plan"), H.prompt_violation)}


def _active(slug):
    return {(j["input"]["_project"]["i"], j["input"]["_project"]["v"]) for j in JOBS.values()
            if (j["input"].get("_project") or {}).get("slug") == slug and j["status"] in ("queued", "running")}


@app.post("/api/projects/{slug}/generate")
def project_generate(slug: str, request: Request):
    """queue every clip variant that is not on disk and not already queued: 000_1..000_N, 001_1, ..."""
    p, m = _pj(PJ.plan, slug), _pj(PJ.meta, slug)
    if not p:
        raise HTTPException(400, "upload the JSON plan first")
    files, busy = PJ.clip_files(slug), _active(slug)
    todo = [(i, v) for i in range(len(p["clips"])) for v in range(1, p["variants"] + 1)
            if (i, v) not in files and (i, v) not in busy]
    inputs = [_pj(PJ.job_input, slug, i, v, p, m) for i, v in todo]      # validate all before queueing any
    for inp in inputs:
        enqueue(inp, me(request)["id"])
    return {"queued": len(inputs)}


@app.post("/api/projects/{slug}/clips/{i}/{v}/regenerate")
def project_regen(slug: str, i: int, v: int, request: Request):
    if (i, v) in _active(slug):
        raise HTTPException(400, "this clip is already in the queue")
    inp = _pj(PJ.job_input, slug, i, v)
    inp.pop("seed", None)                         # a new take: always a fresh random seed
    return {"id": enqueue(inp, me(request)["id"])}


@app.delete("/api/projects/{slug}/clips/{i}/{v}")
def project_del_clip(slug: str, i: int, v: int):
    _pj(PJ.delete_clip, slug, i, v)
    return {"ok": True}


@app.post("/api/projects/{slug}/select/{i}/{v}")
def project_select(slug: str, i: int, v: int):
    return {"selection": _pj(PJ.select, slug, i, v)}


@app.post("/api/projects/{slug}/montage")
async def project_montage(slug: str, request: Request):
    b = await request.json()
    if b.get("custom"):
        return _pj(PJ.montage, slug, custom=True)
    return _pj(PJ.montage, slug, variant=int(b.get("variant") or 1), fallback=bool(b.get("fallback")))


@app.delete("/api/projects/{slug}/movies/{name}")
def project_del_movie(slug: str, name: str):
    _pj(PJ.delete_movie, slug, name)
    return {"ok": True}


@app.get("/api/projects/{slug}/export")
def project_export(slug: str):
    data = _pj(PJ.export_zip, slug)
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{slug}-for-llm.zip"'})


@app.get("/api/status")
def status():
    import zc_client as zc
    try:
        st = zc._req("/system_stats")
        dev = (st.get("devices") or [{}])[0]
        gpu = {"name": dev.get("name"), "vram_free_gb": round((dev.get("vram_free") or 0) / 2**30, 1),
               "vram_total_gb": round((dev.get("vram_total") or 0) / 2**30, 1)}
    except Exception as e:
        gpu = {"error": str(e)[:100]}
    return {"thumbs": True, "gpu": gpu, "queued": sum(1 for j in JOBS.values() if j["status"] == "queued"),
            "running": sum(1 for j in JOBS.values() if j["status"] == "running"), "mode": P.MODE}


if __name__ == "__main__":
    import uvicorn
    print(f"[ui] testers sign in with invitation links made on /admin; the admin password is in "
          f"{P.STATE}/admin_password.txt (or $ZC_ADMIN_PASSWORD)", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("ZC_UI_PORT", "8000")), log_level="warning")
