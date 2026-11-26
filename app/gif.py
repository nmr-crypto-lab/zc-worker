"""Greeting GIF: prompt -> still (Qwen-Image 2.1) -> seamless loop (Wan 2.2 first=last frame) -> text overlay
-> .gif + .mp4 for sharing (WhatsApp etc.). Every ComfyUI output passes the age gate inside zc.run();
the final files are built only from gated outputs. Safe-for-work only: the prompt gets no NSFW LoRA."""
import glob, os, re, subprocess, json

import zc_paths as P
import zc_client as zc
import registry as R
import templates as T

FPS = 16
LENGTH = 49            # Wan: 4n+1 frames -> ~3 s at 16 fps; the last frame equals the first and is dropped
SIDE = 512             # loop size (square)
GIF_W, GIF_FPS = 480, 12

FONT_CANDIDATES = [
    f"{P.ROOT}/fonts/*.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/local/lib/python3*/dist-packages/matplotlib/mpl-data/fonts/ttf/DejaVuSans-Bold.ttf",
    "/usr/lib/python3/dist-packages/matplotlib/mpl-data/fonts/ttf/DejaVuSans-Bold.ttf",
]
# emoji / pictographs are not in the fonts -> drop them instead of drawing empty boxes
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")


def available():
    return bool(R.mains("qwen_image21")) and bool(R.mains("wan22_i2v"))


def font_path():
    for pat in FONT_CANDIDATES:
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    return None


def _wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def text_layer(text, w, h, pos="bottom", out="/tmp/zc_gif_text.png"):
    """transparent PNG with white text + black outline, auto-sized to fit (max 3 lines)"""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    text = _EMOJI.sub("", text or "").strip()
    if not text:
        img.save(out)
        return out
    d = ImageDraw.Draw(img)
    fp = font_path()
    size = int(h / 9)
    while True:
        font = ImageFont.truetype(fp, size) if fp else ImageFont.load_default(size=size)
        lines = _wrap(d, text, font, w * 0.9)
        lh = int(size * 1.15)
        if (len(lines) <= 3 and max(d.textlength(l, font=font) for l in lines) <= w * 0.92
                and lh * len(lines) <= h * 0.4) or size <= 14:
            break
        size -= 2
    stroke = max(2, size // 12)
    total = lh * len(lines)
    y = int(h * 0.05) if pos == "top" else (h - total) // 2 if pos == "middle" else h - total - int(h * 0.06)
    for l in lines:
        x = (w - d.textlength(l, font=font)) / 2
        d.text((x, y), l, font=font, fill=(255, 255, 255, 255), stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
        y += lh
    img.save(out)
    return out


def _probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
                        "stream=width,height,nb_read_frames", "-of", "json", path],
                       capture_output=True, text=True, check=True, timeout=60)
    s = json.loads(r.stdout)["streams"][0]
    return int(s["width"]), int(s["height"]), int(s.get("nb_read_frames") or 0)


def finish(src, text, pos, base):
    """src video -> <base>.mp4 + <base>.gif with the text burned in; drops the duplicated last frame"""
    w, h, n = _probe(src)
    png = text_layer(text, w, h, pos, out=base + ".text.png")
    keep = max(1, n - 1) if n > 1 else 9999
    pre = f"[0:v]trim=end_frame={keep},setpts=PTS-STARTPTS[v];[v][1:v]overlay=0:0"
    mp4, gif = base + ".mp4", base + ".gif"
    # mp4: one loop (~3 s). WhatsApp offers its "GIF" toggle only for videos up to 6 s; sent as GIF it loops forever
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-i", png, "-filter_complex",
                    pre + ",format=yuv420p[o]", "-map", "[o]", "-r", str(FPS),
                    "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-movflags", "+faststart", "-an", mp4],
                   check=True, timeout=300)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-i", png, "-filter_complex",
                    pre + f",fps={GIF_FPS},scale={GIF_W}:-1:flags=lanczos,split[a][b];"
                    "[a]palettegen=max_colors=192:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle[o]",
                    "-map", "[o]", "-loop", "0", gif], check=True, timeout=300)
    try:
        os.remove(png)
    except OSError:
        pass
    return [gif, mp4]


def make(inp, job_id, progress=None, cancel=None):
    """returns (final_paths, info); raises ValueError for user errors"""
    if not available():
        raise ValueError("GIF needs Qwen-Image 2.1 and Wan 2.2 on the volume")
    prompt = (inp.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("write a prompt")
    text = (inp.get("text") or "").strip()[:80]
    pos = inp.get("text_pos") if inp.get("text_pos") in ("top", "middle", "bottom") else "bottom"
    seed = inp.get("seed")
    seed = int(seed) if isinstance(seed, (int, float)) else zc.seed()
    work = f"gif/.work/{job_id}"
    room = "leave empty space at the top for a caption" if pos == "top" else \
        "leave empty space at the bottom for a caption" if pos == "bottom" else ""
    still_prompt = ", ".join(x for x in [prompt, "bright, warm, beautiful greeting card illustration, no text, no letters", room] if x)
    gates, removed, secs = [], 0, 0.0

    def stage(name):
        def cb(st):
            if progress:
                progress({**st, "detail": f"{name} · {st.get('detail') or ''}".strip(" ·")})
        return cb

    wf = T.build("qwen_image21", prompt=still_prompt, negative="text, letters, watermark, logo", seed=seed,
                 width=1024, height=1024, prefix=f"{work}_still")
    kept, info = zc.run(wf, {"job": job_id, "family": "gif", "step": "still", "seed": seed},
                        progress=stage("1/2 picture"), cancel=cancel)
    gates += info["gate"]; removed += info["removed"]; secs += info["secs"]
    if not kept:
        raise ValueError("the picture was removed by the age filter, try another prompt")
    name = zc.upload_image(kept[0])
    motion = (inp.get("motion") or "").strip() or \
        f"{prompt}. Gentle, smooth looping motion, cinemagraph, the camera does not move, soft natural movement"
    if prompt_bad(motion):
        raise ValueError("prompt refused: content involving minors is not allowed")
    wf2 = T.build("wan22_i2v", image=name, end_image=name, mp4=True, prompt=motion, seed=seed, loras={},
                  width=SIDE, height=SIDE, length=LENGTH, prefix=f"{work}_loop")
    kept2, info2 = zc.run(wf2, {"job": job_id, "family": "gif", "step": "loop", "seed": seed},
                          progress=stage("2/2 animation"), cancel=cancel)
    gates += info2["gate"]; removed += info2["removed"]; secs += info2["secs"]
    if not kept2:
        raise ValueError("the animation was removed by the age filter, try another prompt")
    if progress:
        progress({"stage": "saving", "frac": 1, "detail": "adding the text", "eta": None})
    os.makedirs(f"{P.ROOT}/output/gif", exist_ok=True)
    outs = finish(kept2[0], text, pos, f"{P.ROOT}/output/gif/{job_id}")
    return outs, {"gate": gates, "removed": removed, "secs": round(secs, 1), "seed": seed}


def prompt_bad(text):
    import handler as H
    return H.prompt_violation(text, True)
