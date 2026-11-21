"""ZC generation worker. The SAME handler runs as a RunPod Serverless worker and on a normal pod
(`python handler.py --rp_serve_api --rp_api_host 0.0.0.0 --rp_api_port 8000` exposes the same /run, /runsync,
/status API), so the site switches between pod and serverless by changing only the base URL.

Job input:
  {"action": "generate",                   # default
   "family": "zimage" | "illustrious" | "qwen_aio" | "wan22_i2v" | "h3_fl2va" | "h3_ref2va",
   "model": "<registry key>",              # optional, family default otherwise
   "prompt": "...", "negative": "...", "seed": 123,
   "loras": {"<registry key>": 0.8},
   "width": 832, "height": 1216, "seconds": 5, "megapixels": 0.9, "aspect": "9:16", "turbo": true,
   "images": [{"path": "image/<job>/x.png"}]}  # inputs: ONLY our own earlier results (see INPUT RULES)
  {"action": "models"}                     # registry for the UI: families, models, LoRAs, defaults

Output: {"outputs": [{"path", "type", "b64"?, "key"?, "url"?}], "removed": n, "gate": [...], "secs": t}
  path = location under <volume>/zc/output (pods and serverless); b64 = inline copy for small files when no
  object store is configured (the site cannot read the volume); key/url only if R2 is configured (optional).

INPUT RULES (no real people): input images must be results this service generated: {"path": ...} under
<volume>/zc/output, or {"r2_key": ...} if R2 is configured. Arbitrary uploads / URLs are refused.
Every input is age-gated again before use.
Every output is age-gated before upload; flagged/uncheckable outputs are deleted, only metadata is logged.
"""
import base64, os, re, sys, time, uuid, json, traceback, mimetypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zc_paths as P
import zc_client as zc
import registry as R
import templates as T

R2_BUCKET = os.environ.get("ZC_R2_BUCKET", "")
R2_INPUT_PREFIX = os.environ.get("ZC_R2_INPUT_PREFIX", "outputs/")
R2_OUTPUT_PREFIX = os.environ.get("ZC_R2_OUTPUT_PREFIX", "outputs/")
URL_TTL = int(os.environ.get("ZC_URL_TTL", "86400"))
KEEP_LOCAL = not P.USE_R2 or os.environ.get("ZC_KEEP_LOCAL", "1" if P.MODE == "pod" else "0") == "1"
INLINE_MAX = int(os.environ.get("ZC_INLINE_MAX", str(15 * 1024 * 1024)))

# prompts asking for minors are refused before anything is generated (output gate still runs on everything)
_BLOCK = re.compile(
    r"\b(child|children|kid|kids|minor|minors|underage|under[- ]?age|teen|teens|teenager|preteen|pre-teen|"
    r"loli|lolita|shota|toddler|infant|baby|schoolgirl|schoolboy|school girl|school boy|young girl|young boy|"
    r"little girl|little boy|elementary school|middle school|high school|junior high)\b|"
    r"ребен|ребён|дет[еия]|девочк|мальчик|школьн|подрост|малолет|несовершеннолет|лоли|"
    # Indonesian / Malay
    r"\b(anak|anak-anak|bocah|balita|batita|bayi|remaja|abg|siswi|siswa|murid|pelajar|"
    r"gadis kecil|cewek kecil|cowok kecil|di ?bawah umur|sd|smp|sma)\b",
    re.I)
# "<N> years old" and the same in Russian / Indonesian; N < 18 is refused
_AGE = re.compile(r"\b(\d{1,2})\s*(?:yo|y/o|years?[- ]old|year[- ]old|yrs?|лет|год[аы]?|[- ]?летн\w*|tahun|thn)(?!\w)"
                  r"|\b(?:usia|umur|age[d]?|возраст\w*)\s*:?\s*(\d{1,2})\b", re.I)


def prompt_violation(text):
    if not text:
        return None
    for m in _AGE.finditer(text):
        n = int(m.group(1) or m.group(2))
        if n < 18:
            return f"age {n}"
    m = _BLOCK.search(text)
    return m.group(0) if m else None


# ------------------------------------------------------------------ R2 (S3 API)
_s3 = None


def s3():
    global _s3
    if _s3 is None and R2_BUCKET:
        import boto3
        _s3 = boto3.client("s3", endpoint_url=os.environ["ZC_R2_ENDPOINT"],
                           aws_access_key_id=os.environ["ZC_R2_KEY_ID"],
                           aws_secret_access_key=os.environ["ZC_R2_SECRET"], region_name="auto")
    return _s3


def fetch_input(item, job_id, i):
    """returns a file name inside ComfyUI's input folder"""
    if "path" in item:
        base = os.path.realpath(f"{P.ROOT}/output")
        local = os.path.realpath(os.path.join(base, item["path"]))
        if not local.startswith(base + os.sep) or not os.path.isfile(local):
            raise ValueError("input must be one of our own generated results (path under zc/output)")
    elif "r2_key" in item:
        key = item["r2_key"]
        if not s3():
            raise ValueError("R2 not configured")
        if not key.startswith(R2_INPUT_PREFIX) or ".." in key:
            raise ValueError("input must be one of our own generated images")
        local = f"/tmp/zc_in_{job_id}_{i}{os.path.splitext(key)[1] or '.png'}"
        s3().download_file(R2_BUCKET, key, local)
    else:
        raise ValueError("unsupported input (only our own generated images are accepted)")
    g = zc.gate().check(local)
    if not g.get("ok"):
        zc.safety_log({"source": "handler_input", "job": job_id, "gate": g, "action": "refused"})
        raise ValueError("input image refused by the age filter")
    return zc.upload_image(local)


def publish(path, job_id):
    name = os.path.basename(path)
    out = {"type": "video" if path.lower().endswith((".mp4", ".webm", ".webp", ".gif", ".mov")) else "image",
           "name": name}
    out["path"] = os.path.relpath(path, f"{P.ROOT}/output") if path.startswith(f"{P.ROOT}/output") else path
    if s3():
        key = f"{R2_OUTPUT_PREFIX}{time.strftime('%Y/%m/%d')}/{job_id}/{name}"
        ct = mimetypes.guess_type(name)[0] or "application/octet-stream"
        s3().upload_file(path, R2_BUCKET, key, ExtraArgs={"ContentType": ct})
        out["key"] = key
        out["url"] = s3().generate_presigned_url("get_object", Params={"Bucket": R2_BUCKET, "Key": key},
                                                 ExpiresIn=URL_TTL)
    elif P.MODE == "serverless" and os.path.getsize(path) <= INLINE_MAX:
        out["b64"] = base64.b64encode(open(path, "rb").read()).decode()
    if not KEEP_LOCAL:
        try:
            os.remove(path)
        except OSError:
            pass
    return out


# ------------------------------------------------------------------ actions
def models_info():
    reg = R.reg(reload=True)
    fams = {}
    for name, f in reg["families"].items():
        if f.get("template") is None:
            continue
        fams[name] = {"label": f["label"], "kind": f["kind"], "status": f["status"],
                      "models": [{"key": m["key"], "label": m.get("label", m["key"]), "default": bool(m.get("default")),
                                  "defaults": m.get("defaults", {})} for m in R.mains(name)],
                      "loras": [{"key": m["key"], "label": m.get("label", m["key"]), "triggers": m.get("triggers", "")}
                                for m in R.loras(name)]}
    return {"families": fams, "mode": P.MODE}


def generate(inp, job_id, progress=None):
    fam = inp["family"]
    f = R.family(fam)
    if f.get("template") is None:
        raise ValueError(f"unknown family {fam}")
    v = prompt_violation(inp.get("prompt"))
    if v:
        zc.safety_log({"source": "handler_prompt", "job": job_id, "family": fam, "action": "refused", "match": v})
        raise ValueError("prompt refused: content involving minors is not allowed")
    names = [fetch_input(it, job_id, i) for i, it in enumerate(inp.get("images") or [])]
    kw = {k: inp[k] for k in ("model", "prompt", "negative", "seed", "loras", "width", "height", "seconds",
                              "megapixels", "aspect", "turbo", "steps", "length") if k in inp}
    if fam in ("qwen_aio", "wan22_i2v", "h3_fl2va"):
        if not names:
            raise ValueError(f"{fam} needs an input image")
        kw["image"] = names[0]
        if fam == "h3_fl2va" and len(names) > 1:
            kw["last_image"] = names[1]
    if fam == "h3_ref2va":
        if not names:
            raise ValueError("h3_ref2va needs 1-9 reference images")
        kw["refs"] = names
    if fam == "wan22_i2v" and "seconds" in inp and "length" not in kw:
        kw["length"] = int(float(inp["seconds"]) * 16) + 1   # Wan: 16 fps, length must be 4n+1
        kw["length"] -= (kw["length"] - 1) % 4
    if "loras" not in kw:
        kw["loras"] = {}
    kw["prefix"] = f"{f['kind']}/{job_id}"
    op = inp.get("out_prefix")          # set only by the project runner (Create Movie), never from user input
    if op:
        if not re.fullmatch(r"projects/[a-z0-9_-]+/clips/\d{3}_\d+", op):
            raise ValueError("bad out_prefix")
        kw["prefix"] = op
    wf = T.build(fam, **kw)
    kept, info = zc.run(wf, {"job": job_id, "family": fam, "model": inp.get("model"), "seed": wf_seed(wf)},
                        progress=progress)
    return {"outputs": [publish(p, job_id) for p in kept], "removed": info["removed"], "gate": info["gate"],
            "secs": info["secs"], "seed": wf_seed(wf)}


def wf_seed(wf):
    for n in wf.values():
        for k in ("seed", "noise_seed"):
            if k in n["inputs"] and isinstance(n["inputs"][k], int):
                return n["inputs"][k]


def handler(job):
    inp = job.get("input") or {}
    job_id = (job.get("id") or uuid.uuid4().hex)[:24]
    try:
        if inp.get("action") == "models":
            return models_info()
        if not zc.wait_ready(600):
            return {"error": "ComfyUI did not start"}

        last = {"t": 0, "k": None}

        def progress(st):
            # throttle: report a stage change at once, otherwise at most every 3 s
            k = (st.get("stage"), st.get("detail"))
            if k[0] == last["k"] and time.time() - last["t"] < 3:
                return
            last.update(t=time.time(), k=k[0])
            try:
                import runpod
                runpod.serverless.progress_update(job, json.dumps(st))
            except Exception:
                pass
        return generate(inp, job_id, progress)
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        traceback.print_exc()
        return {"error": f"internal: {e.__class__.__name__}: {str(e)[:300]}"}


if __name__ == "__main__":
    import runpod
    runpod.serverless.start({"handler": handler})
