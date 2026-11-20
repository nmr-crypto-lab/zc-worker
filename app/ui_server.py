"""ZC prototype UI: one page + small JSON API, served from the pod (port 8000).

Generation goes through handler.generate (registry -> family template -> ComfyUI -> age gate), so the UI and the
serverless worker share exactly the same code path. Jobs run one at a time on the GPU (in-process queue).

Auth: HTTP Basic, user "zc", password = $ZC_UI_PASSWORD or the one generated into <volume>/zc/state/ui_password.txt.
"""
import base64, json, os, queue, secrets, sys, threading, time, traceback, uuid, mimetypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zc_paths as P

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

import handler as H
import registry as R

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
        job.update(status="running", started=time.time())
        try:
            res = H.generate(dict(job["input"]), jid, progress=lambda st, job=job: job.__setitem__("progress", st))
            for o in res.get("outputs", []):
                o.pop("b64", None)
            job.update(status="done", result=res)
            if not res.get("outputs"):
                job["note"] = "result removed by the age filter" if res.get("removed") else "no result"
        except ValueError as e:
            job.update(status="error", error=str(e))
        except Exception as e:
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
    return open(f"{APP_DIR}/ui/index.html", encoding="utf-8").read()


@app.get("/api/models")
def models():
    return H.models_info()


@app.post("/api/generate")
async def generate(request: Request):
    inp = await request.json()
    if inp.get("family") not in H.models_info()["families"]:
        raise HTTPException(400, "unknown family")
    v = H.prompt_violation(inp.get("prompt"))
    if v:
        raise HTTPException(400, "Prompt refused: content involving minors is not allowed")
    jid = uuid.uuid4().hex[:12]
    with LOCK:
        JOBS[jid] = {"id": jid, "status": "queued", "input": inp, "created": time.time()}
        ORDER.append(jid)
    Q.put(jid)
    return {"id": jid}


@app.get("/api/jobs")
def jobs():
    now = time.time()
    out = []
    for jid in reversed(ORDER[-50:]):
        j = JOBS[jid]
        d = {k: j.get(k) for k in ("id", "status", "error", "note", "progress")}
        d["family"] = j["input"].get("family")
        d["prompt"] = (j["input"].get("prompt") or "")[:120]
        d["queue_pos"] = None
        if j["status"] == "queued":
            d["queue_pos"] = sum(1 for k in ORDER if JOBS[k]["status"] == "queued" and JOBS[k]["created"] <= j["created"])
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


@app.get("/files/{path:path}")
def files(path: str):
    p = os.path.realpath(os.path.join(OUT, path))
    if not p.startswith(OUT + os.sep) or not os.path.isfile(p):
        raise HTTPException(404)
    return FileResponse(p, media_type=mimetypes.guess_type(p)[0] or "application/octet-stream")


@app.delete("/api/files/{path:path}")
def delete_file(path: str):
    """Permanently delete a result (the UI asks for a second tap first)."""
    p = os.path.realpath(os.path.join(OUT, path))
    if not p.startswith(OUT + os.sep) or not os.path.isfile(p) or not p.lower().endswith(MEDIA):
        raise HTTPException(404)
    os.remove(p)
    with open(JOBS_LOG, "a") as f:
        f.write(json.dumps({"deleted": path, "ts": time.time()}) + "\n")
    return {"ok": True}


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
    return {"gpu": gpu, "queued": sum(1 for j in JOBS.values() if j["status"] == "queued"),
            "running": sum(1 for j in JOBS.values() if j["status"] == "running"), "mode": P.MODE}


if __name__ == "__main__":
    import uvicorn
    print(f"[ui] password for user 'zc' is in {P.STATE}/ui_password.txt (or $ZC_UI_PASSWORD)", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("ZC_UI_PORT", "8000")), log_level="warning")
