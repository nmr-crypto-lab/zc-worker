"""Family album: many photos -> chapters with captions (Claude looks at the photos) -> old ones cleaned up (Qwen) ->
every photo comes alive (Wan, a few seconds) -> a book you leaf through; plus a video version and a public link.

The album lives in a JSON file under state/albums/. The UI server runs the jobs and calls on_job() when one finishes.
Videos that only join clips which already passed the age gate go to output/video/montage/ (same rule as memory videos).
"""
import base64, io, json, os, re, secrets, subprocess, threading, time, uuid

import llm
import zc_paths as P

DIR = os.path.join(P.STATE, "albums")
LOCK = threading.Lock()
LANGS = {"en": "English", "ru": "Russian", "id": "Indonesian"}
MOTION = ("very gentle natural motion, the people softly smile, breathe and blink, hair and clothes move slightly, "
          "a slow subtle camera push-in, warm nostalgic mood, keep every face exactly the same, no new people")
RESTORE = ("Restore this old photo: remove scratches, dust, creases, stains and tears, repair faded areas, make it sharp and clear. "
           "Keep its colors: if it is black and white, keep it black and white. Keep every face, expression, pose, clothes and detail exactly the same. Photorealistic.")


def _path(aid):
    return os.path.join(DIR, re.sub(r"[^a-z0-9]", "", aid) + ".json")


def load(aid):
    try:
        return json.load(open(_path(aid)))
    except Exception:
        return None


def save(a):
    os.makedirs(DIR, exist_ok=True)
    tmp = _path(a["id"]) + ".tmp"
    json.dump(a, open(tmp, "w"), ensure_ascii=False)
    os.replace(tmp, _path(a["id"]))


def update(aid, fn):
    with LOCK:
        a = load(aid)
        if a:
            fn(a)
            save(a)
        return a


def by_share(code):
    for f in os.listdir(DIR) if os.path.isdir(DIR) else []:
        if f.endswith(".json"):
            a = load(f[:-5])
            if a and a.get("share") == code:
                return a
    return None


def mine(uid):
    out = []
    for f in os.listdir(DIR) if os.path.isdir(DIR) else []:
        if f.endswith(".json"):
            a = load(f[:-5])
            if a and a["user"] == uid:
                out.append(a)
    return sorted(out, key=lambda a: -a["created"])


def create(uid, title, photos, lang):
    """photos: [{"path", "file"(absolute), "w", "h"}]"""
    a = {"id": uuid.uuid4().hex[:12], "user": uid, "title": (title or "").strip()[:60], "lang": lang, "created": time.time(),
         "share": secrets.token_urlsafe(9), "status": "making", "stage": "sort", "sub": "",
         "photos": [{"orig": p["path"], "path": p["path"], "w": p["w"], "h": p["h"]} for p in photos], "chapters": [], "video": None}
    save(a)
    return a


# ------------------------------------------------------------------ 1. chapters and captions (Claude sees the photos)
PLAN = """You arrange a family photo album. You get the photos numbered from 0. Return ONLY JSON:
{"title": "a warm album title if the user gave none (max 5 words)", "years": "e.g. 1975 — 2024, or empty if you can't tell",
 "chapters": [{"title": "short chapter title (2–4 words)", "photos": [indexes in a nice order]}],
 "photos": {"<index>": {"caption": "a short warm caption, max 6 words, like a handwritten note in an album; empty for some photos",
                       "old": true if it is an old/scanned/damaged/faded photo that needs cleaning, "motion": "how this exact photo could gently come alive in 3 seconds, one short phrase in English (e.g. 'she laughs and looks at him', 'the dog wags its tail', 'leaves move in the wind')"}}}
Rules: every photo in exactly one chapter; 2–6 chapters, chronological when you can tell (old photos first); captions in LANG;
never guess names, ages or relationships you can't see — prefer neutral captions ("Summer by the lake", "All together"); no jokes about looks."""


def _thumb_b64(file, side=640):
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(Image.open(file)).convert("RGB")
    im.thumbnail((side, side))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=82)
    return base64.b64encode(b.getvalue()).decode()


def plan(a, files):
    n = len(a["photos"])
    try:
        content = []
        for i, f in enumerate(files):
            content += [{"type": "text", "text": f"Photo {i}:"}, {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _thumb_b64(f)}}]
        content.append({"type": "text", "text": f"Album title from the user: {a['title'] or '(none)'}. {n} photos."})
        d = llm.ask_json(PLAN.replace("LANG", LANGS.get(a["lang"], "English")), content, max_tokens=3000)
    except llm.NotConfigured:
        d = {}
    except Exception as e:
        print("[album] plan failed:", repr(e)[:200], flush=True)
        d = {}
    seen, chapters = set(), []
    for ch in d.get("chapters") or []:
        idx = [int(i) for i in ch.get("photos") or [] if str(i).isdigit() and int(i) < n and int(i) not in seen]
        seen.update(idx)
        if idx:
            chapters.append({"title": str(ch.get("title") or "")[:40], "photos": idx})
    rest = [i for i in range(n) if i not in seen]
    if rest:
        if chapters:
            chapters[-1]["photos"] += rest
        else:
            chapters = [{"title": "", "photos": rest}]
    info = d.get("photos") or {}
    for i, p in enumerate(a["photos"]):
        x = info.get(str(i)) or {}
        p["caption"] = str(x.get("caption") or "")[:60]
        p["old"] = bool(x.get("old"))
        p["motion"] = re.sub(r"[^\w ,.'-]", "", str(x.get("motion") or ""))[:120]
    a["chapters"] = chapters
    a["sub"] = str(d.get("years") or "")[:20]
    if not a["title"]:
        a["title"] = str(d.get("title") or "")[:60]


# ------------------------------------------------------------------ 2. jobs for every photo
def wan_size(p):
    w, h = p["w"] or 1, p["h"] or 1
    return (832, 480) if w > h * 1.15 else (480, 832) if h > w * 1.15 else (640, 640)


def restore_input(a, i):
    p = a["photos"][i]
    return {"family": "qwen_image21", "prompt": RESTORE, "images": [{"path": p["orig"]}], "keep_size": True, "_real": True, "loras": {},
            "_album": {"id": a["id"], "idx": i, "step": "restore"}}


def alive_input(a, i, safe_motion):
    p = a["photos"][i]
    w, h = wan_size(p)
    m = p.get("motion") or ""
    prompt = (m + ", " if m and safe_motion(m) else "") + MOTION
    return {"family": "wan22_i2v", "prompt": prompt, "images": [{"path": p["path"]}], "seconds": 4, "mp4": True, "width": w, "height": h,
            "_real": True, "loras": {}, "_album": {"id": a["id"], "idx": i, "step": "alive"}}


def progress(a):
    ph = a["photos"]
    alive = sum(1 for p in ph if p.get("video") or p.get("failed"))
    olds = [p for p in ph if p.get("old")]
    return {"sorted": a["stage"] != "sort", "restored": a["stage"] != "sort" and all(p.get("restored") or p.get("failed") or p.get("video") for p in olds),
            "alive": alive, "total": len(ph), "frac": (0.1 if a["stage"] != "sort" else 0.02) + 0.9 * alive / max(1, len(ph))}


def out(a, url=lambda p: p):
    o = {"id": a["id"], "title": a["title"], "sub": a["sub"], "status": a["status"], "share": a["share"], "video": a.get("video")}
    if a["status"] != "ready":
        o["progress"] = progress(a)
    else:
        o["chapters"] = [{"title": ch["title"], "pages": [{"path": url(a["photos"][i]["path"]), "video": url(a["photos"][i]["video"]) if a["photos"][i].get("video") else None,
                                                          "caption": a["photos"][i].get("caption", ""), "tilt": [-1.5, 1, -0.5, 1.5][k % 4]}
                                                         for k, i in enumerate(ch["photos"])]} for ch in a["chapters"]]
    return o


# ------------------------------------------------------------------ 3. the video version: every page is a paper page with the living photo
W, H = 720, 1280
FONT_DIRS = ("/usr/share/fonts/truetype/dejavu",)


def _font(size, serif=True, bold=False, italic=False):
    """DejaVu from the system; the italic serif is not always installed, so fall back step by step"""
    from PIL import ImageFont
    fam = "DejaVuSerif" if serif else "DejaVuSans"
    names = ([fam + "-BoldItalic"] if bold and italic else []) + ([fam + "-Bold"] if bold else []) + \
            ([fam + "-Italic", fam + "-Oblique"] if italic else []) + [fam, "DejaVuSans"]
    for d in FONT_DIRS:
        for n in names:
            p = os.path.join(d, n + ".ttf")
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    return ImageFont.load_default(size=size)


def _paper():
    from PIL import Image, ImageDraw, ImageFilter
    im = Image.new("RGB", (W, H), (245, 236, 217))
    d = ImageDraw.Draw(im)
    for r in range(0, 900, 6):                       # soft light spot like on old paper
        c = int(10 * (1 - r / 900))
        d.ellipse([W * 0.3 - r, H * 0.25 - r, W * 0.3 + r, H * 0.25 + r], outline=(245 + c // 2, 236 + c, 217 + c))
    im = im.filter(ImageFilter.GaussianBlur(6))
    d = ImageDraw.Draw(im)
    for x in range(40):                              # the spine shadow
        a = int(60 * (1 - x / 40))
        d.line([(x, 0), (x, H)], fill=(245 - a, 236 - a, 217 - a))
    return im


def _wrap(d, text, font, maxw):
    words, lines, cur = text.split(), [], ""
    for w_ in words:
        t = (cur + " " + w_).strip()
        if d.textlength(t, font=font) <= maxw:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w_
    if cur:
        lines.append(cur)
    return lines


def page_bg(p, chapter, file_png):
    """paper page with the photo frame and the texts; returns the frame box for the photo"""
    from PIL import ImageDraw
    im, pw, ph = _paper(), p["w"] or 1, p["h"] or 1
    fw = 560
    fh = int(fw * ph / pw)
    if fh > 720:
        fh, fw = 720, int(720 * pw / ph)
    y0 = 230 if chapter else 190
    x, y = (W - fw) // 2, y0
    d = ImageDraw.Draw(im)
    d.rectangle([x - 22, y - 22, x + fw + 22, y + fh + 34], fill=(255, 255, 255))
    d.rectangle([x - 22, y + fh + 34, x + fw + 22, y + fh + 38], fill=(225, 214, 194))
    if chapter:
        f = _font(40, bold=True)
        d.text((W // 2, 120), chapter, font=f, fill=(107, 74, 43), anchor="mm")
    if p.get("caption"):
        f = _font(40, italic=True)
        for k, line in enumerate(_wrap(d, p["caption"], f, W - 120)[:2]):
            d.text((W // 2, y + fh + 100 + k * 52), line, font=f, fill=(74, 56, 38), anchor="mm")
    im.save(file_png)
    return x, y, fw, fh


def cover_bg(a, file_png):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (W, H), (30, 51, 40))
    d = ImageDraw.Draw(im)
    for yy in range(H):
        k = yy / H
        d.line([(0, yy), (W, yy)], fill=(int(47 - 25 * k), int(74 - 36 * k), int(58 - 29 * k)))
    f = _font(58, bold=True)
    for k, line in enumerate(_wrap(d, a["title"] or "", f, W - 120)[:3]):
        d.text((W // 2, 250 + k * 72), line, font=f, fill=(233, 207, 143), anchor="mm")
    if a.get("sub"):
        d.text((W // 2, 1080), a["sub"], font=_font(30), fill=(201, 169, 98), anchor="mm")
    d.rectangle([150, 430, 570, 850], outline=(201, 169, 98), width=3)
    im.save(file_png)
    return 165, 445, 390, 390


def page_clip(bg_png, box, media, out, secs=4.0):
    x, y, w, h = box
    still = not media.lower().endswith((".mp4", ".webm", ".mov"))
    src = ["-loop", "1", "-t", f"{secs}", "-i", media] if still else ["-stream_loop", "-1", "-t", f"{secs}", "-i", media]
    graph = (f"[1:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"
             + (f",zoompan=z='min(zoom+0.0008,1.06)':d=1:s={w}x{h}:fps=24" if still else ",fps=24") + "[ph];"
             f"[0:v][ph]overlay={x}:{y},format=yuv420p[v]")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-t", f"{secs}", "-i", bg_png] + src +
                   ["-filter_complex", graph, "-map", "[v]", "-t", f"{secs}", "-r", "24", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", out],
                   check=True, timeout=300)


def make_video(a, media, montage, out_file):
    """media(path) -> absolute file; montage(paths, out, landscape) joins clips with soft fades"""
    tmp = os.path.join(DIR, "tmp_" + a["id"])
    os.makedirs(tmp, exist_ok=True)
    clips = []
    first = a["photos"][a["chapters"][0]["photos"][0]] if a["chapters"] else None
    bg = os.path.join(tmp, "cover.png")
    box = cover_bg(a, bg)
    if first:
        c = os.path.join(tmp, "cover.mp4")
        page_clip(bg, box, media(first.get("video") or first["path"]), c, 3.5)
        clips.append(c)
    n = 0
    for ch in a["chapters"]:
        for k, i in enumerate(ch["photos"]):
            p = a["photos"][i]
            bg = os.path.join(tmp, f"p{n}.png")
            box = page_bg(p, ch["title"] if k == 0 else "", bg)
            c = os.path.join(tmp, f"p{n}.mp4")
            page_clip(bg, box, media(p.get("video") or p["path"]), c, 4.0)
            clips.append(c)
            n += 1
    montage(clips, out_file, False)
    for f in os.listdir(tmp):
        os.remove(os.path.join(tmp, f))
    os.rmdir(tmp)
