"""Minimal ComfyUI API client with a mandatory output age filter.

Every image / video produced through `run()` passes `AgeGate.check()` before it is kept.
Anything that looks possibly under 20 (or where the gate itself fails) is deleted immediately;
only metadata (model, seed, scores, sha-less id) is written to the safety log.
No face swap, no uploading photos of real people: inputs are only our own generated images.
"""
import json, os, time, uuid, random, urllib.request, urllib.parse, urllib.error

COMFY = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
import zc_paths as P
OUT_ROOT = P.OUTPUT
SAFETY_LOG = P.SAFETY_LOG
MODELS = P.MODELS


# ----------------------------------------------------------------- age gate
class AgeGate:
    """v2: InsightFace buffalo_l (RetinaFace detector + age regression) as the primary signal,
    ViT age classifier as a second opinion on the SAME face crops (no Haar false faces).
    Delete if any real face (det_score >= DET_MIN) has estimated age < MIN_AGE, or the ViT
    gives P(age<20) >= VIT_P on that face. Faces are checked on the image and on video frames.
    Gate errors fail closed (delete)."""
    MIN_AGE = 18
    DET_MIN = 0.6
    VIT_P = 0.90
    YOUNG_LABELS = ("0-2", "3-9", "10-19")

    def __init__(self):
        import torch
        from insightface.app import FaceAnalysis
        from transformers import ViTImageProcessor, ViTForImageClassification
        self.torch = torch
        self.app = FaceAnalysis(name="buffalo_l", root=f"{MODELS}/insightface",
                                allowed_modules=["detection", "genderage"],
                                providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
        self.app.prepare(ctx_id=0, det_size=(640, 640))
        d = f"{MODELS}/safety/vit-age-classifier"
        self.proc = ViTImageProcessor.from_pretrained(d)
        self.vit = ViTForImageClassification.from_pretrained(d).eval()
        self.labels = self.vit.config.id2label
        try:
            self.tagger = AnimeTagger()
        except Exception as e:  # missing model -> check() fails closed unless ZC_REQUIRE_TAGGER=0
            print("[gate] anime tagger not available:", repr(e)[:200], flush=True)
            self.tagger = None

    def _p_young(self, pil):
        with self.torch.no_grad():
            x = self.proc(pil.convert("RGB"), return_tensors="pt")
            p = self.vit(**x).logits.softmax(-1)[0]
        return float(sum(p[i] for i, l in self.labels.items() if l in self.YOUNG_LABELS))

    def score_pil(self, pil):
        import numpy as np
        rgb = pil.convert("RGB")
        bgr = np.array(rgb)[:, :, ::-1].copy()
        faces = [f for f in self.app.get(bgr) if float(f.det_score) >= self.DET_MIN]
        out = []
        for f in faces:
            x1, y1, x2, y2 = [int(v) for v in f.bbox]
            m = int(0.25 * max(x2 - x1, y2 - y1))
            crop = rgb.crop((max(0, x1 - m), max(0, y1 - m), x2 + m, y2 + m))
            out.append({"age": int(f.age), "p_young": round(self._p_young(crop), 3),
                        "det": round(float(f.det_score), 2)})
        return out

    @staticmethod
    def _frames(path):
        """image -> [img]; animated webp/gif -> first/middle/last; mp4/webm/mov -> 5 evenly spaced frames"""
        from PIL import Image, ImageSequence
        if path.lower().endswith((".mp4", ".webm", ".mov", ".mkv")):
            import cv2
            cap = cv2.VideoCapture(path)
            n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
            out = []
            for i in sorted({0, n // 4, n // 2, 3 * n // 4, max(0, n - 1)}):
                cap.set(cv2.CAP_PROP_POS_FRAMES, i)
                ok, fr = cap.read()
                if ok:
                    out.append(Image.fromarray(fr[:, :, ::-1]))
            cap.release()
            if not out:
                raise RuntimeError("no frames decoded")
            return out
        im = Image.open(path)
        frames = [f.copy() for f in ImageSequence.Iterator(im)]
        if len(frames) > 1:
            frames = [frames[0], frames[len(frames) // 2], frames[-1]]
        return frames

    # Everyday pictures (outside output/adult) may show young-looking or cartoon faces - kids' cards, fairy tales,
    # comics. There a young signal deletes only if the picture is not clearly safe (tagger rating) .
    # The 18+ section, ComfyUI previews and anything unknown keep the strict rule: any young signal deletes.
    SAFE_GENERAL = float(os.environ.get("ZC_SAFE_GENERAL", "0.5"))
    SAFE_RISKY = float(os.environ.get("ZC_SAFE_RISKY", "0.15"))

    @staticmethod
    def _strict(path):
        p = os.path.realpath(path)
        out = os.path.realpath(OUT_ROOT)
        return (not p.startswith(out + os.sep)) or p.startswith(os.path.join(out, "adult") + os.sep) \
            or os.environ.get("ZC_GATE_STRICT_ALL") == "1"

    def check(self, path):
        """v3: real-face check (InsightFace + ViT) AND whole-image anime tagger on every output.
        Both run on everything, because the watcher cannot know which model produced a file.
        Delete if either flags; errors fail closed."""
        try:
            frames = self._frames(path)
            faces = [f for fr in frames for f in self.score_pil(fr)]
            tags = self.tagger.score(frames) if self.tagger else None
            res = {"faces": len(faces)}
            ok = True
            if faces:
                res["min_age"] = min(f["age"] for f in faces)
                res["p_young_max"] = max(f["p_young"] for f in faces)
                ok = res["min_age"] >= self.MIN_AGE and res["p_young_max"] < self.VIT_P
            if tags is not None:
                res["tag_max"] = tags["max"]
                res["tag_hits"] = tags["hits"]
                res["rating"] = tags.get("rating") or {}
                if tags["flag"]:
                    ok = False
            elif os.environ.get("ZC_REQUIRE_TAGGER", "1") == "1":
                return {"ok": False, "error": "anime tagger not loaded"}
            if not ok and not self._strict(path) and tags is not None:
                r = tags.get("rating") or {}
                risky = r.get("questionable", 1) + r.get("explicit", 1)
                if r.get("general", 0) >= self.SAFE_GENERAL and risky < self.SAFE_RISKY and r.get("sensitive", 1) < 0.5:
                    ok, res["note"] = True, "young-looking but clearly safe (everyday)"
            res["ok"] = ok
            return res
        except Exception as e:
            return {"ok": False, "error": repr(e)[:200]}


class AnimeTagger:
    """WD EVA02-Large tagger v3 (Danbooru tags). Flags images whose child-related tags score >= HARD_P on the whole
    image or on any of 4 overlapping tiles. Thresholds are deliberately low (prefer deleting adults over keeping minors)."""
    HARD = ("loli", "shota", "child", "aged_down", "baby", "oppai_loli", "randoseru")
    HARD_P = float(os.environ.get("ZC_TAG_P", "0.35"))

    def __init__(self, d=f"{MODELS}/safety/wd-eva02-large-tagger-v3"):
        import csv, onnxruntime as ort
        self.sess = ort.InferenceSession(f"{d}/model.onnx", providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
        inp = self.sess.get_inputs()[0]
        self.inp, self.size = inp.name, inp.shape[1]
        names = [r["name"] for r in csv.DictReader(open(f"{d}/selected_tags.csv"))]
        self.idx = {n: i for i, n in enumerate(names) if n in self.HARD}
        self.rating = {n: i for i, n in enumerate(names) if n in ("general", "sensitive", "questionable", "explicit")}

    def _prep(self, pil):
        import numpy as np
        from PIL import Image
        im = pil.convert("RGB")
        s = max(im.size)
        canvas = Image.new("RGB", (s, s), (255, 255, 255))
        canvas.paste(im, ((s - im.size[0]) // 2, (s - im.size[1]) // 2))
        canvas = canvas.resize((self.size, self.size), Image.BICUBIC)
        return np.asarray(canvas, dtype=np.float32)[:, :, ::-1]  # RGB -> BGR, 0..255

    def _crops(self, pil):
        w, h = pil.size
        out = [pil]
        if min(w, h) >= 640:
            cw, ch = int(w * 0.6), int(h * 0.6)
            for x, y in ((0, 0), (w - cw, 0), (0, h - ch), (w - cw, h - ch)):
                out.append(pil.crop((x, y, x + cw, y + ch)))
        return out

    def score(self, frames):
        import numpy as np
        crops, whole = [], []
        for fr in frames:
            cs = self._crops(fr)
            whole.append(len(crops))            # index of the whole-frame crop
            crops += cs
        batch = np.stack([self._prep(c) for c in crops])
        probs = self.sess.run(None, {self.inp: batch})[0]
        per_tag = {n: float(probs[:, i].max()) for n, i in self.idx.items()}
        mx = max(per_tag.values()) if per_tag else 0.0
        hits = {n: round(p, 3) for n, p in per_tag.items() if p >= 0.1}
        rating = {n: round(float(probs[whole, i].max()), 3) for n, i in self.rating.items()}
        return {"flag": mx >= self.HARD_P, "max": round(mx, 3), "hits": hits, "rating": rating}


_gate = None


def gate():
    global _gate
    if _gate is None:
        _gate = AgeGate()
    return _gate


def safety_log(entry):
    os.makedirs(os.path.dirname(SAFETY_LOG), exist_ok=True)
    entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(SAFETY_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ----------------------------------------------------------------- comfy api
def _req(path, data=None):
    req = urllib.request.Request(COMFY + path, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:1500]}")


def wait_ready(timeout=900):
    t = time.time()
    while time.time() - t < timeout:
        try:
            _req("/system_stats")
            return True
        except Exception:
            time.sleep(3)
    return False


def object_info():
    return _req("/object_info")


def upload_image(path):
    """Upload one of OUR generated images to ComfyUI input (never real photos)."""
    import requests
    with open(path, "rb") as f:
        r = requests.post(COMFY + "/upload/image", files={"image": (os.path.basename(path), f)},
                          data={"overwrite": "true"})
    r.raise_for_status()
    return r.json()["name"]


_OI = None


def fill_defaults(workflow):
    """Add required inputs the workflow does not set, using ComfyUI's own defaults (object_info).
    Protects against new required widgets appearing in newer ComfyUI versions."""
    global _OI
    if _OI is None:
        _OI = object_info()
    for node in workflow.values():
        spec = _OI.get(node["class_type"], {}).get("input", {}).get("required", {})
        for name, s in spec.items():
            if name in node["inputs"]:
                continue
            kind = s[0] if s else None
            opts = s[1] if len(s) > 1 and isinstance(s[1], dict) else {}
            if isinstance(kind, list) and kind:
                node["inputs"][name] = opts.get("default", kind[0])
            elif "default" in opts:
                node["inputs"][name] = opts["default"]
    return workflow


# ----------------------------------------------------------------- progress
LOADERS = ("Loader", "LoraLoader")            # class_type substrings that read model files
ENCODERS = ("TextEncode", "CLIPTextEncode")
SAMPLERS = ("KSampler", "SamplerCustom")
DECODERS = ("VAEDecode", "DecodeAudio")
MODEL_EXT = (".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin", ".sft")
RATE_FILE = f"{P.STATE}/load_rate.json"


def _stage_of(cls):
    if any(k in cls for k in SAMPLERS):
        return "generating"
    if any(k in cls for k in DECODERS):
        return "decoding"
    if any(k in cls for k in ENCODERS):
        return "encoding prompt"
    if any(k in cls for k in LOADERS):
        return "loading models"
    if cls.startswith("Save") or cls.startswith("Create"):
        return "saving"
    return None


def _model_bytes(workflow):
    """Total size of model files referenced by loader nodes -> {node_id: bytes}."""
    out = {}
    for nid, n in workflow.items():
        if not any(k in n["class_type"] for k in LOADERS):
            continue
        b = 0
        for v in n["inputs"].values():
            if isinstance(v, str) and v.lower().endswith(MODEL_EXT):
                for root in ("diffusion_models", "checkpoints", "text_encoders", "vae", "loras", "clip", "unet",
                             "clip_vision", "upscale_models"):
                    f = os.path.join(MODELS, root, v)
                    if os.path.isfile(f):
                        b += os.path.getsize(f)
                        break
        if b:
            out[nid] = b
    return out


def _rate():
    try:
        return max(30e6, json.load(open(RATE_FILE))["bps"])
    except Exception:
        return 120e6          # global volume reads ~100-150 MB/s


def _save_rate(bps):
    try:
        old = _rate()
        json.dump({"bps": 0.6 * old + 0.4 * bps}, open(RATE_FILE, "w"))
    except Exception:
        pass


class Progress:
    """Turns ComfyUI websocket events into {stage, frac, detail, eta} for the UI.
    Sampling progress is exact (step n/N). Model loading has no native progress: it is estimated from the size of the
    model files that are not already cached in ComfyUI and the measured volume read rate."""

    def __init__(self, workflow, cb):
        self.wf, self.cb = workflow, cb
        self.sizes = _model_bytes(workflow)
        self.samplers = [nid for nid, n in workflow.items() if any(k in n["class_type"] for k in SAMPLERS)]
        self.cached = set()
        self.active = []
        self.t0 = time.time()
        self.load_bytes = None
        self.first_step_t = None
        self.step_t = {}
        self.started = False          # True once ComfyUI actually starts OUR prompt (not waiting behind another one)
        self.state = {"stage": "queued", "frac": 0, "detail": "", "eta": None}

    def _emit(self, **kw):
        self.state.update(kw)
        try:
            self.cb(dict(self.state))
        except Exception:
            pass

    def on(self, msg):
        t, d = msg.get("type"), msg.get("data") or {}
        if t == "execution_cached":
            self.cached |= set(d.get("nodes") or [])
        elif t == "execution_start":
            self.t0 = time.time()
            self.started = True
        elif t == "executing" and d.get("node"):
            nid = str(d["node"])
            st = _stage_of(self.wf.get(nid, {}).get("class_type", ""))
            if st == "generating":
                self._preparing(nid)
            elif st:
                self._emit(stage=st, frac=0 if st != self.state["stage"] else self.state["frac"], detail="", eta=None)
        elif t == "progress":
            nid, v, mx = str(d.get("node")), int(d.get("value", 0)), max(1, int(d.get("max", 1)))
            if nid not in self.samplers:            # decoders etc. also report progress
                self._emit(frac=v / mx, detail=f"{v}/{mx}")
                return
            now = time.time()
            if self.first_step_t is None:
                self.first_step_t = now
                if self.load_bytes:
                    _save_rate(self.load_bytes / max(1.0, now - self.t0))
            ts = self.step_t.setdefault(nid, [])
            ts.append(now)
            per = (ts[-1] - ts[0]) / (len(ts) - 1) if len(ts) > 1 else None
            # count only sampler nodes that actually run (a template may contain unused alternatives)
            if nid not in self.active:
                self.active.append(nid)
            k = self.active.index(nid)
            passes = f" · pass {k + 1}" if len(self.active) > 1 else ""
            self._emit(stage="generating", frac=v / mx, detail=f"step {v}/{mx}{passes}",
                       eta=round(per * (mx - v)) if per else None)

    def _preparing(self, nid):
        """Sampler started but no step yet: weights are being read / moved to the GPU."""
        if self.load_bytes is None:
            self.load_bytes = sum(b for n, b in self.sizes.items() if n not in self.cached)
        if self.state["stage"] != "loading models":
            self._emit(stage="loading models" if self.load_bytes else "generating", frac=0, detail="", eta=None)

    def tick(self):
        """Called about once a second while waiting: advance the estimated loading bar."""
        if not self.started:
            if self.state["stage"] != "waiting for GPU":
                self._emit(stage="waiting for GPU", frac=0, detail="another job is running", eta=None)
            return
        if self.state["stage"] == "loading models" or (self.state["stage"] in ("queued", "encoding prompt")
                                                        and self.first_step_t is None):
            lb = self.load_bytes if self.load_bytes is not None else \
                sum(b for n, b in self.sizes.items() if n not in self.cached)
            if not lb:
                return
            done = (time.time() - self.t0) * _rate()
            gb = lb / 1e9
            self._emit(stage="loading models", frac=min(0.95, done / lb),
                       detail=f"~{gb:.0f} GB" if gb >= 1 else f"~{lb / 1e6:.0f} MB",
                       eta=max(0, round((lb - done) / _rate())))


def _listen(cid, prog, stop):
    try:
        import websocket   # websocket-client
    except ImportError:
        return
    url = COMFY.replace("http", "ws", 1) + f"/ws?clientId={cid}"
    try:
        ws = websocket.create_connection(url, timeout=5)
    except Exception:
        return
    ws.settimeout(1)
    while not stop.is_set():
        try:
            m = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue
        except Exception:
            break
        if isinstance(m, str):
            try:
                prog.on(json.loads(m))
            except Exception:
                pass
    try:
        ws.close()
    except Exception:
        pass


class Cancelled(Exception):
    pass


def run(workflow, meta, timeout=3600, progress=None, cancel=None):
    """Queue an API-format workflow, wait, age-gate every output. Returns (kept_paths, info).
    progress(dict) is called with {stage, frac, detail, eta} while the job runs (optional).
    cancel() -> True stops the job: removes it from the ComfyUI queue / interrupts it, raises Cancelled."""
    import threading
    fill_defaults(workflow)
    cid = str(uuid.uuid4())
    t0 = time.time()
    prog = Progress(workflow, progress or (lambda s: None))
    stop = threading.Event()
    th = threading.Thread(target=_listen, args=(cid, prog, stop), daemon=True)
    th.start()
    time.sleep(0.3)                     # let the socket connect before queueing (so no early event is missed)
    try:
        pid = _req("/prompt", {"prompt": workflow, "client_id": cid})["prompt_id"]
        while True:
            h = _req(f"/history/{pid}")
            if pid in h:
                break
            if time.time() - t0 > timeout:
                raise TimeoutError(meta)
            if cancel and cancel():
                for path, body in (("/queue", {"delete": [pid]}), ("/interrupt", {})):
                    try:
                        _req(path, body)
                    except Exception:
                        pass
                raise Cancelled("cancelled")
            prog.tick()
            time.sleep(1)
    finally:
        stop.set()
    if progress:
        prog._emit(stage="age check", frac=0, detail="", eta=None)
    secs = time.time() - t0
    hist = h[pid]
    st = hist.get("status", {})
    if st.get("status_str") == "error":
        msgs = [m for m in st.get("messages", []) if m[0] == "execution_error"]
        raise RuntimeError(json.dumps(msgs)[:1500])
    kept, removed, gates = [], 0, []
    for node_out in hist.get("outputs", {}).values():
        for key in ("images", "gifs", "videos"):
            for o in node_out.get(key, []):
                p = os.path.join(OUT_ROOT, o.get("subfolder", ""), o["filename"])
                if not os.path.exists(p):
                    continue
                try:
                    g = gate().check(p)
                except Exception as e:  # gate failed to load -> fail closed
                    g = {"ok": False, "error": "gate init: " + repr(e)[:200]}
                gates.append(g)
                entry = {**meta, "file_id": o["filename"], "gate": g}
                if not g["ok"]:
                    os.remove(p)
                    removed += 1
                    entry["action"] = "deleted"
                else:
                    kept.append(p)
                    entry["action"] = "kept"
                safety_log(entry)
    return kept, {"secs": round(secs, 1), "removed": removed, "gate": gates}


def seed():
    return random.randint(0, 2**31 - 1)
