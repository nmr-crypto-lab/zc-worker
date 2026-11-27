"""'Tell a story': idea -> screenplay (Claude) -> heroes (one portrait each) -> pictures -> [film: clips with voices] -> result.

Kinds:
  comic  pictures with lines; the lines are our own text layer under the pictures (exact, any language)
  film   the same pictures, then every picture comes alive with voices (MiniMax H3 first-frame -> video+audio),
         then the clips are joined into one film
  us     a film whose heroes come from people's own photos: the photo is turned into a drawn character first,
         and only that drawing is used further on (the real photo never goes to a video model)

Pictures are drawn by Qwen-Image 2.1 with the hero portraits as references (the recipe that keeps heroes consistent).
The screenplay also carries Pip's comments (written by the same call) so the helper reacts to THIS story.
"""
import random, re

import llm

STYLES = {
    "comic": "American comic book art, bold black ink outlines, flat vibrant colors, halftone shading",
    "manga": "black and white manga art, clean ink lines, screentone shading, expressive faces",
    "toon3d": "3D animated cartoon movie look, soft studio lighting, expressive stylized characters",
    "toon2d": "classic 2D cartoon style, clean bold lines, bright flat colors",
    "storybook": "storybook watercolor illustration, soft pastel colors, gentle textures",
}
NO_TEXT = "No text, no letters, no speech bubbles, no captions, no logos."
LANGS = {"en": "English", "ru": "Russian", "id": "Indonesian"}
FILM_W, FILM_H = 768, 1344          # vertical: films are watched and sent on phones

SYSTEM = """You are a comic and cartoon screenwriter for a casual app, and also the voice of Pip, the app's cheerful little helper.
Turn the user's idea, joke, tale, poem or family story into a short KIND.
Return ONLY JSON:
{"title": "...",
 "style": "comic|manga|toon3d|toon2d|storybook",
 "characters": [{"id": "c1", "name": "...", "role": "...", "look": "..."}],
 "panels": [{"scene": "...", "motion": "...", "chars": ["c1"], "caption": "...", "lines": [{"who": "c1", "text": "..."}]}],
 "pip": {"start": "...", "heroes": {"c1": "..."}, "panels": ["...", "..."], "final": "..."}}
Rules:
- title, name, role, caption, line texts and everything in "pip" are in LANG. "look", "scene" and "motion" are in English (they go to image and video models).
- "style": the drawing style that suits this story best (cute family stories: toon3d or storybook, action and jokes: comic or toon2d, drama: manga).
- 1 to 3 characters. "role" = 2-4 words about the hero (e.g. "the star of the show"). "look" = what stays the same in every picture, one line:
  species, "man/woman/boy/girl, about N years old", hair, clothes, one or two striking details (no beauty marks, freckles or piercings - they drift).
- Each panel: "scene" = one concrete picture (who does what, where, camera: close-up / medium / wide). Mention characters by name.
- At most 2 lines per panel, each under 90 characters; "caption" is optional narration under 80 characters (use "" if none).
- Keep the punchline or the point of the story. Family-friendly. No real famous people, no brands.
- "pip": short playful comments (each under 70 characters) from a friendly helper who is making this KIND right now and enjoys the story:
  "start" when the story is ready and the heroes are being drawn; "heroes" one remark per hero when its picture appears;
  "panels" one remark per panel when it appears (same count as panels); "final" when everything is ready, inviting to send it.
  Warm humour, no emojis, never mean.
EXTRA"""

FILM_EXTRA = """- This is an animated film: every panel is one shot of 5-8 seconds that will come alive with sound.
  "motion" = what moves in the shot and how (gestures, facial expressions, camera move), in English, one sentence.
  Lines are spoken aloud: natural, short, easy to say; at most 2 lines per shot. Captions are read by a narrator."""
TALE_EXTRA = """- This is a bedtime story for a small child, read aloud by a parent. The child named in the idea is the hero.
  Each panel is a page: "caption" = the story text of that page in LANG, 2-3 short sentences, up to 280 characters;
  "lines" = [] (no dialogue). Gentle and warm, a little adventure, no scary parts; the last page ends with the hero falling asleep.
  "style" = storybook unless the idea asks otherwise."""
US_EXTRA = """- The heroes are real people from the user's photos. Use EXACTLY these characters (ids and names), add none:
PEOPLE
  For them "look" = only clothes and outfit for this story (the face, hair and age come from their photo)."""


def plan(idea, style, n, lang, kind="comic", people=None, variant=0):
    n = max(3, min(8, int(n or 6)))
    lang_name = LANGS.get(lang, "English")
    extra = ""
    if kind in ("film", "us"):
        extra += FILM_EXTRA
    if kind == "tale":
        extra += TALE_EXTRA
    if kind == "us" and people:
        extra += "\n" + US_EXTRA.replace("PEOPLE", "\n".join(f'  {{"id": "c{i + 1}", "name": "{p["name"]}"}}' for i, p in enumerate(people)))
    system = SYSTEM.replace("EXTRA", extra).replace("LANG", lang_name).replace("KIND", "animated film" if kind in ("film", "us") else "bedtime story with pictures" if kind == "tale" else "comic")
    user = f"LANG: {lang_name}\nPANELS: exactly {n}\n" + (f"STYLE: {style}\n" if style in STYLES else "") + \
           (f"VARIANT: {variant} (write a clearly different take than an obvious first version)\n" if variant else "") + \
           f"IDEA:\n{idea.strip()[:3000]}"
    d = llm.ask_json(system, user, max_tokens=4000)
    d["kind"] = kind
    d = normalize(d, n)
    if kind == "us" and people:                       # attach the photos; the model was told to keep ids/names
        for i, p in enumerate(people[:3]):
            ch = next((c for c in d["characters"] if c["name"].strip().lower() == p["name"].strip().lower()), None)
            if ch is None and i < len(d["characters"]):
                ch = d["characters"][i]
            if ch is not None:
                ch["photo"] = p["path"]
    return d


def _s(v, n):
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def normalize(d, n=8):
    chars, ids = [], set()
    for i, c in enumerate((d.get("characters") or [])[:3]):
        cid = _s(c.get("id"), 8) or f"c{i + 1}"
        if cid in ids:
            cid = f"c{i + 1}"
        ids.add(cid)
        chars.append({"id": cid, "name": _s(c.get("name"), 30) or cid, "role": _s(c.get("role"), 40), "look": _s(c.get("look"), 300),
                      "portrait": c.get("portrait") if isinstance(c.get("portrait"), str) else None,
                      "photo": c.get("photo") if isinstance(c.get("photo"), str) else None})
    panels = []
    for p in (d.get("panels") or [])[:max(n, 1)]:
        lines = [{"who": _s(l.get("who"), 8), "text": _s(l.get("text"), 120)}
                 for l in (p.get("lines") or [])[:2] if _s(l.get("text"), 120)]
        panels.append({"scene": _s(p.get("scene"), 400), "motion": _s(p.get("motion"), 300),
                       "chars": [c for c in (p.get("chars") or []) if c in ids][:3],
                       "caption": _s(p.get("caption"), 300 if d.get("kind") == "tale" else 100), "lines": lines,
                       "image": p.get("image") if isinstance(p.get("image"), str) else None})
    pip = d.get("pip") if isinstance(d.get("pip"), dict) else {}
    pip = {"start": _s(pip.get("start"), 120), "final": _s(pip.get("final"), 120),
           "heroes": {k: _s(v, 120) for k, v in (pip.get("heroes") or {}).items() if k in ids} if isinstance(pip.get("heroes"), dict) else {},
           "panels": [_s(x, 120) for x in (pip.get("panels") or [])][:len(panels)] if isinstance(pip.get("panels"), list) else []}
    style = d.get("style") if d.get("style") in STYLES else None
    return {"title": _s(d.get("title"), 80), "kind": d.get("kind") if d.get("kind") in ("comic", "film", "us", "tale") else "comic",
            "style": style, "characters": chars, "panels": panels, "pip": pip}


def all_text(d):
    """everything a user can type or the writer wrote, for the minors filter"""
    parts = [d.get("title", "")]
    parts += [c["name"] + " " + c.get("role", "") + " " + c["look"] for c in d["characters"]]
    for p in d["panels"]:
        parts += [p["scene"], p.get("motion", ""), p["caption"]] + [l["text"] for l in p["lines"]]
    return "\n".join(parts)


def cast_inputs(style, ch, n=1):
    st = STYLES.get(style, STYLES["comic"])
    if ch.get("photo"):      # a real person -> a drawn character that keeps them recognizable (the photo stays here)
        p = (f"{st}. Character design: turn the person from reference image 1 into a cartoon character in this style. "
             f"Everything is drawn in this style, the face too: stylized cartoon features, big expressive eyes, simplified shapes, "
             f"NOT a photo and no photographic skin. Keep them recognizable: the same hairstyle and hair color, face shape, "
             f"glasses, beard or moustache if any, age. Outfit: {ch['look'] or 'their everyday clothes'}. "
             f"Full body, front view, standing, plain white background. {NO_TEXT}")
        return [{"family": "qwen_image21", "prompt": p, "seed": s, "width": 1024, "height": 1024,
                 "images": [{"path": ch["photo"]}], "_real": True} for s in random.sample(range(1, 10 ** 6), n)]
    p = (f"{st}. Character design of {ch['name']}: {ch['look']}. "
         f"Full body, front view, standing, plain white background. {NO_TEXT}")
    return [{"family": "qwen_image21", "prompt": p, "seed": s, "width": 1024, "height": 1024}
            for s in random.sample(range(1, 10 ** 6), n)]


def panel_input(style, d, i):
    p = d["panels"][i]
    by = {c["id"]: c for c in d["characters"]}
    present = [by[c] for c in p["chars"] if c in by]
    refs = [c for c in present if c.get("portrait")]
    who = " ".join(f"{c['name']} is the character from reference image {refs.index(c) + 1} ({c['look']})." if c in refs
                   else f"{c['name']}: {c['look']}." for c in present)
    film = d.get("kind") in ("film", "us")
    prompt = (f"{STYLES.get(style, STYLES['comic'])}. {'Animation film still, vertical frame' if film else 'Comic panel'}. {p['scene']} {who} "
              f"Keep every character exactly as in their reference image, but NOT their plain white background: "
              f"show the full scene with a detailed background that fits the place (never a plain white or empty background). {NO_TEXT}")
    inp = {"family": "qwen_image21", "prompt": prompt, "width": FILM_W if film else 1024, "height": FILM_H if film else 1024}
    if refs:
        inp["images"] = [{"path": c["portrait"]} for c in refs]
    return inp


def clip_seconds(p):
    n = len(p["caption"]) + sum(len(l["text"]) for l in p["lines"])
    return int(max(4, min(8, round(2.5 + n / 13))))


def clip_input(style, d, i, lang):
    """the panel picture comes alive with voices: MiniMax H3, first frame = the picture"""
    p = d["panels"][i]
    by = {c["id"]: c for c in d["characters"]}
    lang_name = LANGS.get(lang, "English")
    speech = []
    if p["caption"]:
        speech.append(f'A warm narrator voice says in {lang_name}: "{p["caption"]}"')
    for l in p["lines"]:
        c = by.get(l["who"])
        who = f"{c['name']} ({' '.join(c['look'].split()[:8])})" if c else "A character"
        speech.append(f'{who} says in {lang_name}: "{l["text"]}"')
    kind = {"toon3d": "3D cartoon", "manga": "anime", "storybook": "storybook"}.get(style, "cartoon")
    prompt = (f"{kind} animation. {p.get('motion') or p['scene']} " + " ".join(speech) +
              " Keep the characters and the picture style exactly as in the image. Lively expressive voices, matching lip movement, "
              "soft ambient sound, no music, no subtitles, no text.")
    return {"family": "h3_fl2va", "prompt": prompt, "images": [{"path": p["image"]}], "seconds": clip_seconds(p),
            "megapixels": 0.6, "_story": True}


FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/local/lib/python3.12/dist-packages/cv2/qt/fonts")


def _font(bold, size):
    from PIL import ImageFont
    import os
    for d in FONT_DIRS:
        p = os.path.join(d, "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def compose(d, root, out):
    """the finished comic as one picture, the same layout as the app: title, pictures in 2 columns,
    caption and lines UNDER each picture. Built on the server from pictures that already passed the age gate."""
    import os
    from PIL import Image, ImageDraw
    ps = [p for p in d["panels"] if p.get("image")]
    names = {c["id"]: c["name"] for c in d["characters"]}
    W, col, gap, top, fs, pad = 1080, 2, 18, 112, 27, 12
    pw = (W - gap * 3) // col
    F, FB, FT = _font(False, fs), _font(True, fs), _font(True, 56)
    lh = fs + 9
    tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    def wrap(t, w, first, font):
        out_, cur, lim = [], "", w - first
        for wd in t.split(" "):
            tr = (cur + " " + wd) if cur else wd
            if tmp.textlength(tr, font=font) > lim and cur:
                out_.append(cur)
                cur, lim = wd, w
            else:
                cur = tr
        if cur:
            out_.append(cur)
        return out_

    blocks = []
    for p in ps:
        bl = []
        if p.get("caption"):
            bl.append({"cap": True, "nm": "", "nw": 0, "ls": wrap(p["caption"], pw - 2 * pad, 0, F)})
        for l in p.get("lines", []):
            nm = (names.get(l["who"]) + ": ") if names.get(l["who"]) else ""
            nw = tmp.textlength(nm, font=FB) if nm else 0
            bl.append({"cap": False, "nm": nm, "nw": nw, "ls": wrap(l["text"], pw - 2 * pad, nw, F)})
        blocks.append(bl)
    th = lambda bs: sum(len(b["ls"]) * lh + 2 * pad - 6 + 8 for b in bs) + (6 if bs else 0)
    rows = [max(th(b) for b in blocks[n:n + col]) for n in range(0, len(ps), col)]
    H = top + sum(pw + rh + gap for rh in rows) + gap
    im = Image.new("RGB", (W, H), "white")
    g = ImageDraw.Draw(im)
    g.text((W // 2, 76), d.get("title") or "", font=FT, fill="#111111", anchor="ms")
    y = top
    for r, rh in enumerate(rows):
        for c2 in range(col):
            n = r * col + c2
            if n >= len(ps):
                break
            x = gap + c2 * (pw + gap)
            pic = Image.open(os.path.join(root, ps[n]["image"])).convert("RGB")
            s = min(pic.width, pic.height)       # square crop from the middle (film stills are vertical)
            pic = pic.crop(((pic.width - s) // 2, (pic.height - s) // 2, (pic.width + s) // 2, (pic.height + s) // 2)).resize((pw, pw))
            im.paste(pic, (x, y))
            g.rectangle((x, y, x + pw, y + pw), outline="#111111", width=4)
            by = y + pw + 10
            for b in blocks[n]:
                bh = len(b["ls"]) * lh + 2 * pad - 6
                g.rounded_rectangle((x, by, x + pw, by + bh), radius=4 if b["cap"] else 14,
                                    fill="#fef08a" if b["cap"] else "#f1f5f9", outline="#111111", width=3)
                for k, ln in enumerate(b["ls"]):
                    tx, ty = x + pad, by + pad + k * lh
                    if k == 0 and b["nm"]:
                        g.text((tx, ty), b["nm"], font=FB, fill="#1d4ed8")
                        tx += b["nw"]
                    g.text((tx, ty), ln, font=F, fill="#111111")
                by += bh + 8
        y += pw + rh + gap
    os.makedirs(os.path.dirname(out), exist_ok=True)
    im.save(out, "PNG", optimize=True)
