"""Create Movie projects: named references + a JSON plan of clips -> H3 ref2va clips -> montage.

Layout (inside the output folder, so the age watcher covers it; hidden from the general gallery):
    output/projects/<slug>/
        meta.json        title, refs {name: {file, desc, updated}}, selection {clip: variant}, created
        project.json     the plan: {"variants", "model", "megapixels", "aspect", "clips": [{"length","prompt","refs"}]}
        refs/<name>.<ext>
        clips/<NNN>_<v>.mp4  + <NNN>_<v>.json  (what the clip was made with: prompt, refs versions, seed)
        movies/movie_<NN>.mp4, movie_custom.mp4

The state of a project is read from the files on disk, so an interrupted run is resumed with "generate missing".
"""
import io, json, os, re, shutil, subprocess, time, zipfile

import zc_paths as P

OUT = os.path.realpath(f"{P.ROOT}/output")
BASE = os.path.join(OUT, "projects")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
FAMILY = "h3_ref2va"
LEN_MIN, LEN_MAX = 5, 15
MAX_REFS = 9
MAX_VARIANTS = 8
ASPECTS = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9")


class ProjectError(ValueError):
    pass


# ----------------------------------------------------------------------------- helpers
_TR = dict(zip("абвгдеёзийклмнопрстуфхцы", "abvgdeezijklmnoprstufhcy"))
_TR.update({"ж": "zh", "ч": "ch", "ш": "sh", "щ": "sch", "ю": "yu", "я": "ya", "э": "e", "ъ": "", "ь": ""})


def slugify(title):
    t = "".join(_TR.get(ch, ch) for ch in title.lower())
    s = re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:40]
    return s or time.strftime("p-%Y%m%d-%H%M%S")


def pdir(slug):
    if not NAME_RE.match(slug or ""):
        raise ProjectError("bad project name")
    d = os.path.join(BASE, slug)
    if not os.path.isdir(d):
        raise ProjectError("no such project")
    return d


def _read(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def meta(slug):
    return _read(os.path.join(pdir(slug), "meta.json"), {})


def save_meta(slug, m):
    _write(os.path.join(pdir(slug), "meta.json"), m)


def plan(slug):
    return _read(os.path.join(pdir(slug), "project.json"), None)


def cname(i, v):
    return f"{i:03d}_{v}"


def rel(path):
    return os.path.relpath(path, OUT)


# ----------------------------------------------------------------------------- projects
def list_projects():
    out = []
    if not os.path.isdir(BASE):
        return out
    for slug in sorted(os.listdir(BASE)):
        d = os.path.join(BASE, slug)
        if not (NAME_RE.match(slug) and os.path.isdir(d)):
            continue
        m = _read(os.path.join(d, "meta.json"), {})
        p = _read(os.path.join(d, "project.json"), None) or {}
        clips = len(p.get("clips") or [])
        done = len([f for f in os.listdir(os.path.join(d, "clips")) if f.endswith(".mp4")]) \
            if os.path.isdir(os.path.join(d, "clips")) else 0
        out.append({"slug": slug, "title": m.get("title") or slug, "created": m.get("created"),
                    "clips": clips, "variants": p.get("variants", 1), "done": done, "refs": len(m.get("refs") or {})})
    out.sort(key=lambda x: -(x["created"] or 0))
    return out


def create(title):
    title = (title or "").strip()[:80]
    if not title:
        raise ProjectError("enter a project name")
    slug = base = slugify(title)
    n = 2
    while os.path.exists(os.path.join(BASE, slug)):
        slug = f"{base[:36]}-{n}"
        n += 1
    d = os.path.join(BASE, slug)
    for sub in ("refs", "clips", "movies"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    _write(os.path.join(d, "meta.json"), {"title": title, "created": time.time(), "refs": {}, "selection": {}})
    return slug


def delete_project(slug):
    shutil.rmtree(pdir(slug))


# ----------------------------------------------------------------------------- references
def add_ref(slug, name, src_rel, desc=""):
    """Copy one of our generated images into the project under a stable name. The same name replaces the picture:
    every clip that uses the name picks up the new one."""
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise ProjectError("reference name: lowercase letters, digits, - and _ (e.g. denis, room_wide)")
    src = os.path.realpath(os.path.join(OUT, src_rel or ""))
    if not src.startswith(OUT + os.sep) or not os.path.isfile(src):
        raise ProjectError("pick an image from the gallery")
    if not src.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        raise ProjectError("references must be images")
    d = pdir(slug)
    m = meta(slug)
    refs = m.setdefault("refs", {})
    old = refs.get(name)
    ext = os.path.splitext(src)[1].lower()
    dst = os.path.join(d, "refs", name + ext)
    if old and old.get("file") and os.path.join(d, old["file"]) != dst:
        try:
            os.remove(os.path.join(d, old["file"]))
        except OSError:
            pass
    shutil.copyfile(src, dst + ".tmp")
    os.replace(dst + ".tmp", dst)
    refs[name] = {"file": f"refs/{name}{ext}", "desc": (desc or (old or {}).get("desc") or "").strip()[:300],
                  "updated": time.time()}
    save_meta(slug, m)
    return name


def set_ref_desc(slug, name, desc):
    m = meta(slug)
    if name not in m.get("refs", {}):
        raise ProjectError("no such reference")
    m["refs"][name]["desc"] = (desc or "").strip()[:300]
    save_meta(slug, m)


def delete_ref(slug, name):
    d = pdir(slug)
    m = meta(slug)
    r = m.get("refs", {}).pop(name, None)
    if not r:
        raise ProjectError("no such reference")
    try:
        os.remove(os.path.join(d, r["file"]))
    except OSError:
        pass
    save_meta(slug, m)


# ----------------------------------------------------------------------------- plan (project.json)
def validate(data, refs, prompt_check=None):
    """Returns (clean plan, warnings). Raises ProjectError on anything that cannot be generated."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError as e:
            raise ProjectError(f"not valid JSON: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("clips"), list) or not data["clips"]:
        raise ProjectError('JSON must be an object with a non-empty "clips" list')
    warn = []
    try:
        variants = int(data.get("variants", 1))
    except (TypeError, ValueError):
        raise ProjectError('"variants" must be a number')
    if not 1 <= variants <= MAX_VARIANTS:
        raise ProjectError(f'"variants" must be 1..{MAX_VARIANTS}')
    clean = {"variants": variants}
    for k in ("model", "megapixels", "aspect", "negative"):
        if data.get(k) not in (None, ""):
            clean[k] = data[k]
    if "aspect" in clean and clean["aspect"] not in ASPECTS:
        raise ProjectError(f'"aspect" must be one of {", ".join(ASPECTS)}')
    if "megapixels" in clean and clean["megapixels"] not in (0.5, 0.9):
        raise ProjectError('"megapixels" must be 0.5 or 0.9')
    if len(data["clips"]) > 999:
        raise ProjectError("at most 999 clips")
    clips = []
    for n, c in enumerate(data["clips"]):
        where = f"clip {n:03d}"
        if not isinstance(c, dict):
            raise ProjectError(f"{where}: must be an object")
        prompt = (c.get("prompt") or "").strip()
        if not prompt:
            raise ProjectError(f"{where}: empty prompt")
        if prompt_check:
            v = prompt_check(prompt)
            if v:
                raise ProjectError(f"{where}: prompt refused (content involving minors is not allowed)")
        try:
            length = float(c.get("length", 5))
        except (TypeError, ValueError):
            raise ProjectError(f"{where}: length must be a number of seconds")
        if not LEN_MIN <= length <= LEN_MAX:
            raise ProjectError(f"{where}: length must be {LEN_MIN}-{LEN_MAX} s")
        rr = c.get("refs") or []
        if isinstance(rr, str):
            rr = [x.strip() for x in rr.split(",") if x.strip()]
        rr = [os.path.splitext(str(x).strip().lower())[0] for x in rr]   # "denis.png" -> "denis"
        if not rr:
            raise ProjectError(f"{where}: needs at least one reference")
        if len(rr) > MAX_REFS:
            raise ProjectError(f"{where}: at most {MAX_REFS} references")
        missing = [x for x in rr if x not in refs]
        if missing:
            warn.append(f"{where}: no reference named {', '.join(missing)} yet")
        cc = {"length": int(length) if length == int(length) else length, "prompt": prompt, "refs": rr}
        if c.get("seed") not in (None, ""):
            try:
                cc["seed"] = int(c["seed"])
            except (TypeError, ValueError):
                raise ProjectError(f"{where}: seed must be a number")
        if c.get("title"):
            cc["title"] = str(c["title"])[:120]
        clips.append(cc)
        if f"<Picture {len(rr) + 1}>" in prompt:
            warn.append(f"{where}: the prompt mentions <Picture {len(rr) + 1}> but has only {len(rr)} references")
    clean["clips"] = clips
    return clean, warn


def save_plan(slug, data, prompt_check=None):
    clean, warn = validate(data, meta(slug).get("refs", {}), prompt_check)
    _write(os.path.join(pdir(slug), "project.json"), clean)
    return warn


# ----------------------------------------------------------------------------- clips
def clip_files(slug):
    """{(i, v): {path, mtime, meta}} for the clips on disk"""
    d = os.path.join(pdir(slug), "clips")
    out = {}
    for f in os.listdir(d):
        m = re.fullmatch(r"(\d{3})_(\d+)\.mp4", f)
        if m:
            p = os.path.join(d, f)
            out[(int(m.group(1)), int(m.group(2)))] = {
                "path": rel(p), "mtime": int(os.path.getmtime(p)),
                "meta": _read(os.path.join(d, f[:-4] + ".json"), {})}
    return out


def _signature(clip, refs):
    return {"prompt": clip["prompt"], "length": clip["length"],
            "refs": {r: (refs.get(r) or {}).get("updated") for r in clip["refs"]}}


def job_input(slug, i, v, p=None, m=None):
    """The generation request for clip i, variant v (same format as /api/generate)."""
    p = p or plan(slug)
    m = m or meta(slug)
    if not p:
        raise ProjectError("upload the JSON plan first")
    if not 0 <= i < len(p["clips"]):
        raise ProjectError("no such clip")
    c = p["clips"][i]
    refs = m.get("refs", {})
    missing = [r for r in c["refs"] if r not in refs]
    if missing:
        raise ProjectError(f"clip {i:03d}: missing reference {', '.join(missing)}")
    inp = {"family": FAMILY, "prompt": c["prompt"], "seconds": c["length"],
           "images": [{"path": rel(os.path.join(BASE, slug, refs[r]["file"]))} for r in c["refs"]],
           "out_prefix": f"projects/{slug}/clips/{cname(i, v)}"}
    for k in ("model", "megapixels", "aspect", "negative"):
        if k in p:
            inp[k] = p[k]
    if "seed" in c:
        inp["seed"] = c["seed"] + v - 1
    inp["_project"] = {"slug": slug, "i": i, "v": v, "sig": _signature(c, refs)}
    return inp


def on_done(job, res):
    """Called by the worker when a project job finished: give the clip its final name, record what it was made with."""
    info = job["input"].get("_project")
    if not info:
        return
    outs = res.get("outputs") or []
    if not outs:
        return
    src = os.path.realpath(os.path.join(OUT, outs[0]["path"]))
    d = os.path.join(BASE, info["slug"], "clips")
    name = cname(info["i"], info["v"])
    dst = os.path.join(d, name + os.path.splitext(src)[1])
    os.replace(src, dst)
    _write(os.path.join(d, name + ".json"),
           dict(info["sig"], seed=res.get("seed"), job=job["id"], created=time.time()))
    outs[0]["path"] = rel(dst)


def delete_clip(slug, i, v):
    d = os.path.join(pdir(slug), "clips")
    n = cname(i, v)
    found = False
    for ext in (".mp4", ".json"):
        try:
            os.remove(os.path.join(d, n + ext))
            found = True
        except OSError:
            pass
    m = meta(slug)
    if m.get("selection", {}).get(str(i)) == v:
        del m["selection"][str(i)]
        save_meta(slug, m)
    if not found:
        raise ProjectError("no such clip")


def select(slug, i, v):
    """Custom montage: one variant per clip; choosing another variant of the same clip replaces it, choosing the
    selected one again unselects it."""
    m = meta(slug)
    sel = m.setdefault("selection", {})
    if sel.get(str(i)) == v:
        del sel[str(i)]
    else:
        sel[str(i)] = v
    save_meta(slug, m)
    return sel


def state(slug, jobs=()):
    """Everything the project page shows. `jobs` = live queue entries (dicts with id/status/input/error)."""
    d = pdir(slug)
    m = meta(slug)
    p = plan(slug)
    refs = m.get("refs", {})
    files = clip_files(slug)
    live = {}
    for j in jobs:
        info = (j.get("input") or {}).get("_project")
        if info and info.get("slug") == slug:
            live[(info["i"], info["v"])] = j            # newest last wins
    clips = []
    nv = (p or {}).get("variants", 1)
    used = {}
    for i, c in enumerate((p or {}).get("clips", [])):
        sig = _signature(c, refs)
        for r in c["refs"]:
            used.setdefault(r, []).append(i)
        vs = []
        allv = sorted(set(range(1, nv + 1)) | {v for (ii, v) in files if ii == i})
        for v in allv:
            f = files.get((i, v))
            j = live.get((i, v))
            st = "missing"
            if f:
                st = "done"
            if j and j["status"] in ("queued", "running"):
                st = j["status"]
            elif j and j["status"] == "error" and not f:
                st = "error"
            e = {"v": v, "name": cname(i, v), "status": st}
            if f:
                e.update(path=f["path"], mtime=f["mtime"])
                fm = f["meta"]
                e["outdated"] = bool(fm) and any(fm.get(k) != sig[k] for k in ("prompt", "length", "refs"))
            if j and j["status"] == "error":
                e["error"] = (j.get("error") or j.get("note") or "")[:200]
            if j and j.get("note") and j["status"] == "done" and not f:
                e["status"], e["error"] = "error", j["note"]
            vs.append(e)
        clips.append({"i": i, "name": f"{i:03d}", "length": c["length"], "prompt": c["prompt"], "refs": c["refs"],
                      "title": c.get("title"), "missing_refs": [r for r in c["refs"] if r not in refs],
                      "variants": vs})
    movies = []
    md = os.path.join(d, "movies")
    for f in sorted(os.listdir(md)):
        if f.endswith(".mp4"):
            fp = os.path.join(md, f)
            movies.append({"name": f[:-4], "path": rel(fp), "mtime": int(os.path.getmtime(fp)),
                           "size": os.path.getsize(fp), "info": _read(fp[:-4] + ".json", {})})
    return {"slug": slug, "title": m.get("title") or slug, "plan": p,
            "refs": [{"name": n, "path": rel(os.path.join(d, r["file"])), "desc": r.get("desc", ""),
                      "updated": r.get("updated"), "used_by": used.get(n, [])} for n, r in sorted(refs.items())],
            "clips": clips, "selection": {int(k): v for k, v in (m.get("selection") or {}).items()},
            "movies": movies, "orphans": sorted(cname(i, v) for (i, v) in files if not p or i >= len(p["clips"]))}


# ----------------------------------------------------------------------------- montage
def _probe(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,"
                            "r_frame_rate,sample_rate,channels", "-of", "json", path],
                           capture_output=True, text=True, timeout=30)
        return json.loads(r.stdout).get("streams", [])
    except Exception:
        return []


def _concat(files, dst):
    """Join clips. Same codec/size/fps everywhere (normal for H3) -> stream copy (seconds); otherwise re-encode."""
    sig = [json.dumps(_probe(f), sort_keys=True) for f in files]
    tmp = dst + ".tmp.mp4"
    lst = dst + ".list.txt"
    with open(lst, "w") as fh:
        for f in files:
            fh.write("file '" + f.replace("'", "'\\''") + "'\n")
    ok = False
    if len(set(sig)) == 1:
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst,
                            "-c", "copy", "-movflags", "+faststart", tmp], capture_output=True, text=True, timeout=600)
        ok = r.returncode == 0
    if not ok:
        first = next((s for s in _probe(files[0]) if s.get("codec_type") == "video"), {})
        w, h = first.get("width", 1280), first.get("height", 720)
        args = ["ffmpeg", "-y", "-loglevel", "error"]
        for f in files:
            args += ["-i", f]
        has_audio = all(any(s.get("codec_type") == "audio" for s in _probe(f)) for f in files)
        parts = "".join(f"[{k}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                        f"fps=24,setsar=1[v{k}];" for k in range(len(files)))
        chain = "".join(f"[v{k}]" + (f"[{k}:a]" if has_audio else "") for k in range(len(files)))
        fc = parts + chain + f"concat=n={len(files)}:v=1:a={1 if has_audio else 0}[v]" + ("[a]" if has_audio else "")
        args += ["-filter_complex", fc, "-map", "[v]"] + (["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
                                                         if has_audio else []) + \
            ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
             tmp]
        r = subprocess.run(args, capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            try:
                os.remove(lst)
            except OSError:
                pass
            raise ProjectError("montage failed: " + (r.stderr or "")[-300:])
    os.remove(lst)
    os.replace(tmp, dst)


def montage(slug, variant=None, custom=False, fallback=False):
    """variant N: clip 000_N + 001_N + ... -> movie_0N; custom: the selected variant of each clip -> movie_custom.
    Uses the continuous run of clips from 000 that exist now; stops at the first gap (or, with fallback, takes any
    existing variant of that clip)."""
    d = pdir(slug)
    p = plan(slug)
    if not p:
        raise ProjectError("no JSON plan yet")
    files = clip_files(slug)
    sel = {int(k): v for k, v in (meta(slug).get("selection") or {}).items()}
    used, subs, stop = [], [], None
    for i in range(len(p["clips"])):
        v = sel.get(i) if custom else variant
        f = files.get((i, v)) if v else None
        if not f and fallback and not custom:
            alts = sorted(vv for (ii, vv) in files if ii == i)
            if alts:
                f = files[(i, alts[0])]
                subs.append(f"{cname(i, variant)} -> {cname(i, alts[0])}")
                v = alts[0]
        if not f:
            stop = f"{i:03d}" + (f"_{v}" if v else " (nothing selected)")
            break
        used.append(cname(i, v))
    if not used:
        raise ProjectError(f"nothing to join: clip {stop} is missing")
    name = "movie_custom" if custom else f"movie_{int(variant):02d}"
    dst = os.path.join(d, "movies", name + ".mp4")
    _concat([os.path.join(d, "clips", u + ".mp4") for u in used], dst)
    info = {"clips": used, "stopped_at": stop if len(used) < len(p["clips"]) else None, "substituted": subs,
            "total_clips": len(p["clips"]), "made": time.time()}
    _write(os.path.join(d, "movies", name + ".json"), info)
    return dict(info, path=rel(dst), name=name)


def delete_movie(slug, name):
    if not re.fullmatch(r"movie_(\d{2}|custom)", name or ""):
        raise ProjectError("bad movie name")
    d = os.path.join(pdir(slug), "movies")
    found = False
    for ext in (".mp4", ".json"):
        try:
            os.remove(os.path.join(d, name + ext))
            found = True
        except OSError:
            pass
    if not found:
        raise ProjectError("no such movie")


# ----------------------------------------------------------------------------- export for an external LLM
BRIEF = """# Film project: {title}

You are helping to turn a story into a short film made of video clips.
Each clip is generated by **MiniMax H3 reference-to-video**: it takes 1-{max_refs} reference images (characters,
places, objects) and a text prompt, and generates a {lmin}-{lmax} second clip **with sound and speech**.

## References available in this project
The images are attached (file name = reference name).

{refs}

## What to produce
Return **only** a JSON object in this format:

```json
{{
  "variants": 3,
  "aspect": "16:9",
  "clips": [
    {{
      "length": 10,
      "refs": ["investigator", "room"],
      "prompt": "<Picture 1> sits at the desk in <Picture 2> ..."
    }}
  ]
}}
```

Rules:
- `variants`: how many alternative takes to generate per clip (1-{max_variants}).
- `aspect` (optional): one of {aspects}.
- `clips`: in story order. `length` is {lmin}-{lmax} seconds.
- `refs`: names from the list above, 1-{max_refs} per clip. In the prompt, refer to them as `<Picture 1>`,
  `<Picture 2>`, ... **in the same order** as in `refs`.
- Use only the references a clip actually needs (a close-up of one face: just that face).
- Write prompts in English. Describe the shot (framing, camera movement), what happens, the mood and lighting,
  and the sound. Put spoken lines in quotes and say who speaks and how, e.g.
  `<Picture 1> says quietly in Russian: "..."`.
- Keep each clip to one continuous shot; split longer scenes into several clips.
- All characters are adults. Never describe minors.
{current}"""


def export_zip(slug):
    d = pdir(slug)
    m = meta(slug)
    p = plan(slug)
    refs = m.get("refs", {})
    lines = []
    for n, r in sorted(refs.items()):
        lines.append(f"- **{n}** (`{os.path.basename(r['file'])}`)" + (f": {r['desc']}" if r.get("desc") else ""))
    cur = ""
    if p:
        cur = ("\n## Current plan\nThe current `project.json` is attached. Edit it instead of starting over unless "
               "asked to.\n")
    brief = BRIEF.format(title=m.get("title") or slug, refs="\n".join(lines) or "(none yet)", max_refs=MAX_REFS,
                         lmin=LEN_MIN, lmax=LEN_MAX, max_variants=MAX_VARIANTS, aspects=", ".join(ASPECTS),
                         current=cur)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("brief.md", brief)
        for n, r in refs.items():
            fp = os.path.join(d, r["file"])
            if os.path.exists(fp):
                z.write(fp, os.path.basename(r["file"]))
        if p:
            z.writestr("project.json", json.dumps(p, ensure_ascii=False, indent=2))
    return buf.getvalue()
