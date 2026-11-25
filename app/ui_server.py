"""ZC prototype UI: one page + small JSON API, served from the pod (port 8000).

Generation goes through handler.generate (registry -> family template -> ComfyUI -> age gate), so the UI and the
serverless worker share exactly the same code path. Jobs run one at a time on the GPU (in-process queue).

Auth: HTTP Basic, user "zc", password = $ZC_UI_PASSWORD or the one generated into <volume>/zc/state/ui_password.txt.
"""
import asyncio, base64, glob, json, os, queue, re, secrets, sys, threading, time, traceback, uuid, mimetypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zc_paths as P

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

import handler as H
import registry as R
import projects as PJ

APP_DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.realpath(f"{P.ROOT}/output")
MEDIA = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm", ".mov")
JOBS_LOG = f"{P.LOGS}/ui_jobs.jsonl"


def _password():
    if os.environ.get("ZC_UI_PASSWORD"):
        return os.environ["ZC_UI_PASSWORD"]
    f = f"{P.STATE}/ui_password.txt"
    try:
        return open(f).read().strip()
    except OSError:
        pw = secrets.token_urlsafe(9)
        os.makedirs(P.STATE, exist_ok=True)
        open(f, "w").write(pw)
        return pw


PASSWORD = _password()
app = FastAPI(title="ZC")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    h = request.headers.get("authorization", "")
    ok = False
    if h.startswith("Basic "):
        try:
            user, _, pw = base64.b64decode(h[6:]).decode().partition(":")
            ok = secrets.compare_digest(pw, PASSWORD)
        except Exception:
            ok = False
    # link-friendly alternative for phones: open /?key=<password> once, a cookie keeps you logged in
    key = request.query_params.get("key") or request.cookies.get("zc_key") or ""
    if not ok and key and secrets.compare_digest(key, PASSWORD):
        ok = True
    if not ok:
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="ZC"'})
    resp = await call_next(request)
    if request.query_params.get("key"):
        resp.set_cookie("zc_key", key, max_age=30 * 86400, httponly=True, secure=True, samesite="lax")
    return resp


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
        job = {k: r.get(k) for k in ("id", "status", "input", "created", "started", "finished", "error", "note")}
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
        Q.task_done()


_restore()
threading.Thread(target=worker, daemon=True).start()


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
def job_one(jid: str):
    j = JOBS.get(jid)
    if not j:
        raise HTTPException(404)
    d = {k: j.get(k) for k in ("id", "status", "error", "progress")}
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
    if not j or j["status"] != "done" or j["input"].get("family") != "gif":
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
    return {"outputs": [{"path": os.path.relpath(o, f"{P.ROOT}/output"), "type": "video"} for o in outs]}


@app.get("/api/models")
def models():
    return H.models_info()


def enqueue(inp):
    jid = uuid.uuid4().hex[:12]
    with LOCK:
        JOBS[jid] = {"id": jid, "status": "queued", "input": inp, "created": time.time()}
        ORDER.append(jid)
    Q.put(jid)
    return jid


@app.post("/api/generate")
async def generate(request: Request):
    inp = await request.json()
    for k in ("out_prefix", "_project"):          # internal keys, set only by the project runner
        inp.pop(k, None)
    if inp.get("family") not in H.models_info()["families"]:
        raise HTTPException(400, "unknown family")
    v = H.prompt_violation(inp.get("prompt"))
    if v:
        raise HTTPException(400, "Prompt refused: content involving minors is not allowed")
    return {"id": enqueue(inp)}


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
def cancel_job(jid: str):
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404, "no such job")
    with LOCK:
        if not _cancel(job):
            raise HTTPException(409, f"job is already {job['status']}")
    return {"ok": True}


@app.post("/api/jobs/cancel_queued")
def cancel_queued():
    with LOCK:
        n = sum(_cancel(j) for j in list(JOBS.values()) if j["status"] == "queued")
    return {"cancelled": n}


@app.get("/api/jobs")
def jobs():
    now = time.time()
    out = []
    active = [k for k in ORDER if JOBS[k]["status"] in ("queued", "running")]
    recent = [k for k in ORDER if JOBS[k]["status"] not in ("queued", "running")][-30:]
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
def gallery(limit: int = 120, kind: str = ""):
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
def files(path: str, dl: int = 0):
    p = _media(path)
    h = {"Cache-Control": "private, max-age=86400"}
    if dl:
        h["Content-Disposition"] = f'attachment; filename="{os.path.basename(p)}"'
    return FileResponse(p, media_type=mimetypes.guess_type(p)[0] or "application/octet-stream", headers=h)


THUMBS = os.environ.get("ZC_THUMBS", "/root/zcthumbs")
THUMB_PX = 360


@app.get("/thumb/{path:path}")
def thumb(path: str):
    """Small JPEG preview for the gallery (a frame for videos). Cached on local disk, rebuilt if the file changes.
    Phones would otherwise download and decode every full PNG / MP4 in the grid."""
    import subprocess
    from PIL import Image
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
def delete_file(path: str):
    """Permanently delete a result (the UI asks for a second tap first)."""
    if path.startswith("projects/"):
        raise HTTPException(403, "project files are managed on the project page")
    p = os.path.realpath(os.path.join(OUT, path))
    if not p.startswith(OUT + os.sep) or not os.path.isfile(p) or not p.lower().endswith(MEDIA):
        raise HTTPException(404)
    os.remove(p)
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
def projects_list():
    return PJ.list_projects()


@app.post("/api/projects")
async def projects_create(request: Request):
    return {"slug": _pj(PJ.create, (await request.json()).get("title"))}


@app.get("/api/projects/{slug}")
def project_get(slug: str):
    return _pj(PJ.state, slug, sorted(JOBS.values(), key=lambda j: j.get("created") or 0))


@app.delete("/api/projects/{slug}")
def project_delete(slug: str):
    if any((j["input"].get("_project") or {}).get("slug") == slug and j["status"] in ("queued", "running")
           for j in JOBS.values()):
        raise HTTPException(400, "the project still has clips in the queue")
    _pj(PJ.delete_project, slug)
    return {"ok": True}


@app.post("/api/projects/{slug}/refs")
async def project_add_ref(slug: str, request: Request):
    b = await request.json()
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
def project_generate(slug: str):
    """queue every clip variant that is not on disk and not already queued: 000_1..000_N, 001_1, ..."""
    p, m = _pj(PJ.plan, slug), _pj(PJ.meta, slug)
    if not p:
        raise HTTPException(400, "upload the JSON plan first")
    files, busy = PJ.clip_files(slug), _active(slug)
    todo = [(i, v) for i in range(len(p["clips"])) for v in range(1, p["variants"] + 1)
            if (i, v) not in files and (i, v) not in busy]
    inputs = [_pj(PJ.job_input, slug, i, v, p, m) for i, v in todo]      # validate all before queueing any
    for inp in inputs:
        enqueue(inp)
    return {"queued": len(inputs)}


@app.post("/api/projects/{slug}/clips/{i}/{v}/regenerate")
def project_regen(slug: str, i: int, v: int):
    if (i, v) in _active(slug):
        raise HTTPException(400, "this clip is already in the queue")
    inp = _pj(PJ.job_input, slug, i, v)
    inp.pop("seed", None)                         # a new take: always a fresh random seed
    return {"id": enqueue(inp)}


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
    print(f"[ui] password for user 'zc' is in {P.STATE}/ui_password.txt (or $ZC_UI_PASSWORD)", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("ZC_UI_PORT", "8000")), log_level="warning")
