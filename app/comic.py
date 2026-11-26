"""'Tell a story' -> Comic: idea -> screenplay (Claude) -> heroes (pick a portrait) -> panels -> lines on top.

Panels are drawn by Qwen-Image 2.1 with the chosen hero portraits as references (the same recipe that keeps
18+ heroines consistent). The pictures carry no text: lines and captions are our own text layer, so they are
always exactly as written and in any language.
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

SYSTEM = """You are a comic screenwriter for a casual app. Turn the user's idea, joke, tale or family story into a short comic.
Return ONLY JSON:
{"title": "...",
 "characters": [{"id": "c1", "name": "...", "look": "..."}],
 "panels": [{"scene": "...", "chars": ["c1"], "caption": "...", "lines": [{"who": "c1", "text": "..."}]}]}
Rules:
- title, name, caption and line texts are in LANG. "look" and "scene" are in English (they go to an image model).
- 1 to 3 characters. "look" = what stays the same in every panel, in one line: species, "man/woman/boy/girl, about N years old",
  hair, clothes, one or two striking details (no beauty marks, freckles or piercings - they drift between pictures).
- Each panel: "scene" = one concrete picture (who does what, where, camera: close-up / wide shot). Mention characters by name.
- At most 2 lines per panel, each under 90 characters; "caption" is optional narration under 80 characters (use "" if none).
- Keep the punchline or the point of the story. Family-friendly. No real people, no brands.
"""


def plan(idea, style, n, lang):
    n = max(3, min(8, int(n or 6)))
    user = f"LANG: {LANGS.get(lang, 'English')}\nPANELS: exactly {n}\nSTYLE: {style}\nIDEA:\n{idea.strip()[:3000]}"
    d = llm.ask_json(SYSTEM.replace("LANG", LANGS.get(lang, "English")), user)
    return normalize(d, n)


def _s(v, n):
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def normalize(d, n=8):
    chars, ids = [], set()
    for i, c in enumerate((d.get("characters") or [])[:3]):
        cid = _s(c.get("id"), 8) or f"c{i + 1}"
        if cid in ids:
            cid = f"c{i + 1}"
        ids.add(cid)
        chars.append({"id": cid, "name": _s(c.get("name"), 30) or cid, "look": _s(c.get("look"), 300),
                      "portrait": c.get("portrait") if isinstance(c.get("portrait"), str) else None})
    panels = []
    for p in (d.get("panels") or [])[:max(n, 1)]:
        lines = [{"who": _s(l.get("who"), 8), "text": _s(l.get("text"), 120)}
                 for l in (p.get("lines") or [])[:2] if _s(l.get("text"), 120)]
        panels.append({"scene": _s(p.get("scene"), 400), "chars": [c for c in (p.get("chars") or []) if c in ids][:3],
                       "caption": _s(p.get("caption"), 100), "lines": lines,
                       "image": p.get("image") if isinstance(p.get("image"), str) else None})
    return {"title": _s(d.get("title"), 80), "characters": chars, "panels": panels}


def all_text(d):
    """everything a user can type, for the minors filter"""
    parts = [d.get("title", "")]
    parts += [c["name"] + " " + c["look"] for c in d["characters"]]
    for p in d["panels"]:
        parts += [p["scene"], p["caption"]] + [l["text"] for l in p["lines"]]
    return "\n".join(parts)


def cast_inputs(style, ch, n=1):
    p = (f"{STYLES.get(style, STYLES['comic'])}. Character design of {ch['name']}: {ch['look']}. "
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
    prompt = (f"{STYLES.get(style, STYLES['comic'])}. Comic panel. {p['scene']} {who} "
              f"Keep every character exactly as in their reference image, but NOT their plain white background: "
              f"show the full scene with a detailed background that fits the place. {NO_TEXT}")
    inp = {"family": "qwen_image21", "prompt": prompt, "width": 1024, "height": 1024}
    if refs:
        inp["images"] = [{"path": c["portrait"]} for c in refs]
    return inp
