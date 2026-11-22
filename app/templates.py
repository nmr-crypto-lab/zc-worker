"""Workflow templates: one builder per FAMILY. Models / LoRAs / defaults come from the registry, so adding a model
or LoRA of an existing family needs no code here.

    import templates as T
    wf = T.build("zimage", prompt="...", seed=1)                 # API-format workflow, default model
    wf = T.build("wan22_i2v", image="x.png", loras={"lora_dr34ml4y_v2": 0.8})
    T.export_all()   # writes one API workflow per family (all LoRAs present at strength 0) for the UI library
"""
import json, os, random
import registry as R

import zc_paths as P
EXPORT_DIR = f"{P.ROOT}/workflows-api"
TITLE_PROMPT = "✏️ PROMPT — write here (English)"
TITLE_NEG = "negative (usually leave as is)"


def _n(cls, inputs, title=None):
    d = {"class_type": cls, "inputs": inputs}
    if title:
        d["_meta"] = {"title": title}
    return d


def _main(fam, key):
    m = next((x for x in R.mains(fam) if x["key"] == key), None) if key else R.default_main(fam)
    if not m:
        raise RuntimeError(f"family {fam}: model {key or '(default)'} not downloaded")
    return m


def _d(m, k, default=None):
    return (m.get("defaults") or {}).get(k, default)


def _lora_chain(wf, src, fam, loras, nid, part=None, lora_list=None):
    """append LoraLoaderModelOnly nodes; loras={key: strength}; None -> every registry LoRA at strength 0 (UI)"""
    items = lora_list if lora_list is not None else R.loras(fam)
    for m in items:
        if loras is not None and m["key"] not in loras:
            continue
        s = 0.0 if loras is None else float(loras[m["key"]])
        name = m.get(f"rel_{part}") if part else m["rel"]
        if not name:
            continue
        wf[str(nid)] = _n("LoraLoaderModelOnly", {"model": src, "lora_name": name, "strength_model": s},
                          f"LoRA {m.get('label', m['key'])}{' ' + part.upper() if part else ''}"
                          f"{' — ' + m['triggers'] if m.get('triggers') else ''}")
        src, nid = [str(nid), 0], nid + 1
    return src, nid


# ------------------------------------------------------------------ image: Z-Image family
def zimage(prompt="describe the image here", negative="lowres, blurry, deformed, watermark, text", seed=0,
           width=832, height=1216, model=None, prefix="image/zimage", **_):
    m = _main("zimage", model)
    te, vae = R.one("zimage", "te"), R.one("zimage", "vae")
    return {
        "1": _n("UNETLoader", {"unet_name": m["rel"], "weight_dtype": "default"}, "Model"),
        "2": _n("CLIPLoader", {"clip_name": te["rel"], "type": te.get("clip_type", "lumina2")}),
        "3": _n("VAELoader", {"vae_name": vae["rel"]}),
        "4": _n("ModelSamplingAuraFlow", {"model": ["1", 0], "shift": _d(m, "shift", 3.0)}),
        "5": _n("CLIPTextEncode", {"clip": ["2", 0], "text": prompt}, TITLE_PROMPT),
        "6": _n("CLIPTextEncode", {"clip": ["2", 0], "text": negative}, TITLE_NEG),
        "7": _n("EmptySD3LatentImage", {"width": width, "height": height, "batch_size": 1}),
        "8": _n("KSampler", {"model": ["4", 0], "positive": ["5", 0], "negative": ["6", 0], "latent_image": ["7", 0],
                             "seed": seed, "steps": _d(m, "steps", 8), "cfg": _d(m, "cfg", 1.0),
                             "sampler_name": _d(m, "sampler", "euler"), "scheduler": _d(m, "scheduler", "simple"),
                             "denoise": 1.0}),
        "9": _n("VAEDecode", {"samples": ["8", 0], "vae": ["3", 0]}),
        "10": _n("SaveImage", {"images": ["9", 0], "filename_prefix": prefix}),
    }


# ------------------------------------------------------------------ image: anime (Illustrious / SDXL checkpoints)
def illustrious(prompt="describe the image here (danbooru tags work best)", negative=None, seed=0,
                width=832, height=1216, model=None, prefix="image/anime", loras=None, **_):
    m = _main("illustrious", model)
    wf = {
        "1": _n("CheckpointLoaderSimple", {"ckpt_name": m["rel"]}, "Model"),
        "2": _n("CLIPSetLastLayer", {"clip": ["1", 1], "stop_at_clip_layer": _d(m, "clip_skip", -2)}),
    }
    src, _ = _lora_chain(wf, ["1", 0], "illustrious", loras, 100)
    wf.update({
        # prefix (mature female, adult) is part of the safety setup: keep it in front of the user prompt
        "5": _n("CLIPTextEncode", {"clip": ["2", 0], "text": m.get("prompt_prefix", "") + prompt}, TITLE_PROMPT),
        "6": _n("CLIPTextEncode", {"clip": ["2", 0], "text": negative or m.get("negative", "")}, TITLE_NEG),
        "7": _n("EmptyLatentImage", {"width": width, "height": height, "batch_size": 1}),
        "8": _n("KSampler", {"model": src, "positive": ["5", 0], "negative": ["6", 0], "latent_image": ["7", 0],
                             "seed": seed, "steps": _d(m, "steps", 28), "cfg": _d(m, "cfg", 6.0),
                             "sampler_name": _d(m, "sampler", "euler_ancestral"), "scheduler": _d(m, "scheduler", "normal"),
                             "denoise": 1.0}),
        "9": _n("VAEDecode", {"samples": ["8", 0], "vae": ["1", 2]}),
        "10": _n("SaveImage", {"images": ["9", 0], "filename_prefix": prefix}),
    })
    return wf


# ------------------------------------------------------------------ image: Qwen-Image 2.1 (text-to-image, or up to 10 references)
# Graph = the official ComfyUI templates (image_qwen_image_2_1[_turbo]_{t2i,image_edit}.json) without the optional
# prompt-enhancer subgraph. refs -> TextEncodeQwenImage21 images.image_N (+ vae, so they are spliced in as latents).
# keep_size=True (edit an image): the output takes the size of reference 1, like the template's default.
def qwen_image21(prompt="describe the image here", negative="", seed=0, width=1024, height=1024, model=None,
                 refs=None, keep_size=False, prefix="image/qwen21", **_):
    m = _main("qwen_image21", model)
    te, vae = R.one("qwen_image21", "te"), R.one("qwen_image21", "vae")
    refs = list(refs or [])[:10]
    wf = {
        "1": _n("UNETLoader", {"unet_name": m["rel"], "weight_dtype": "default"}, "Model"),
        "2": _n("QwenImage21Cache", {"model": ["1", 0], "device": "auto", "dtype": "default"}),
        "3": _n("CLIPLoader", {"clip_name": te["rel"], "type": te.get("clip_type", "qwen_image"), "device": "default"}),
        "4": _n("VAELoader", {"vae_name": vae["rel"]}),
    }
    enc = {"clip": ["3", 0], "prompt": prompt, "negative_prompt": negative or "",
           "resolution": 0 if keep_size else 1024}
    for i, name in enumerate(refs):
        wf[str(20 + i)] = _n("LoadImage", {"image": name}, f"Reference {i + 1} (our own generated image)")
        enc[f"images.image_{i + 1}"] = [str(20 + i), 0]
    if refs:
        enc["vae"] = ["4", 0]
    wf["5"] = _n("TextEncodeQwenImage21", enc, TITLE_PROMPT)
    if keep_size and refs:
        latent = ["5", 2]
    else:
        wf["6"] = _n("EmptyLatentImage", {"width": int(width), "height": int(height), "batch_size": 1})
        latent = ["6", 0]
    sig = _d(m, "sigmas")
    if sig:                       # Turbo: fixed 8-step schedule from the official template
        wf.update({
            "7": _n("RandomNoise", {"noise_seed": seed}),
            "8": _n("KSamplerSelect", {"sampler_name": _d(m, "sampler", "euler")}),
            "9": _n("ManualSigmas", {"sigmas": sig}),
            "10": _n("CFGGuider", {"model": ["2", 0], "positive": ["5", 0], "negative": ["5", 1], "cfg": _d(m, "cfg", 1.0)}),
            "11": _n("SamplerCustomAdvanced", {"noise": ["7", 0], "guider": ["10", 0], "sampler": ["8", 0],
                                               "sigmas": ["9", 0], "latent_image": latent}),
        })
        out = ["11", 0]
    else:
        wf["11"] = _n("KSampler", {"model": ["2", 0], "positive": ["5", 0], "negative": ["5", 1], "latent_image": latent,
                                   "seed": seed, "steps": _d(m, "steps", 25), "cfg": _d(m, "cfg", 1.0),
                                   "sampler_name": _d(m, "sampler", "euler"), "scheduler": _d(m, "scheduler", "simple"),
                                   "denoise": 1.0})
        out = ["11", 0]
    wf["12"] = _n("VAEDecode", {"samples": out, "vae": ["4", 0]})
    wf["13"] = _n("SaveImage", {"images": ["12", 0], "filename_prefix": prefix})
    return wf


# ------------------------------------------------------------------ image: FLUX 3 Image Klein (text-to-image, or up to 10 references)
# Graph = BFL's ComfyUI pack workflows flux3_klein_bf16_{t2i,mi2i}.json without the optional prompt-upsampler subgraph.
def flux3_klein(prompt="describe the image here", seed=0, width=1024, height=1024, model=None, refs=None,
                prefix="image/klein", **_):
    import math
    m = _main("flux3_klein", model)
    te, vae = R.one("flux3_klein", "te"), R.one("flux3_klein", "vae")
    shift = round(math.log(float(_d(m, "alpha", 16))), 5)
    w, h = int(width) // 16 * 16, int(height) // 16 * 16
    wf = {
        "1": _n("UNETLoader", {"unet_name": m["rel"], "weight_dtype": "default"}, "Model"),
        "2": _n("ModelSamplingFlux", {"model": ["1", 0], "max_shift": shift, "base_shift": shift, "width": 1024, "height": 1024}),
        "3": _n("CLIPLoader", {"clip_name": te["rel"], "type": te.get("clip_type", "flux3"), "device": "default"}),
        "4": _n("VAELoader", {"vae_name": vae["rel"]}),
        "5": _n("CLIPTextEncode", {"clip": ["3", 0], "text": prompt}, TITLE_PROMPT),
        "6": _n("CLIPTextEncode", {"clip": ["3", 0], "text": ""}, TITLE_NEG),
        "7": _n("EmptyFlux2LatentImage", {"width": w, "height": h, "batch_size": 1}),
    }
    pos, neg = ["5", 0], ["6", 0]
    refs = list(refs or [])[:10]
    if refs:
        ref = {"positive": pos, "negative": neg, "vae": ["4", 0], "resolution": "resize", "resolution.megapixels": 1.0}
        for i, name in enumerate(refs):
            wf[str(20 + i)] = _n("LoadImage", {"image": name}, f"Reference {i + 1} (our own generated image)")
            ref[f"images.image{i}"] = [str(20 + i), 0]
        wf["8"] = _n("ReferenceMultiLatent", ref)
        pos, neg = ["8", 0], ["8", 1]
    wf["9"] = _n("KSampler", {"model": ["2", 0], "positive": pos, "negative": neg, "latent_image": ["7", 0], "seed": seed,
                             "steps": _d(m, "steps", 16), "cfg": _d(m, "cfg", 1.0), "sampler_name": _d(m, "sampler", "euler"),
                             "scheduler": _d(m, "scheduler", "simple"), "denoise": 1.0})
    wf["10"] = _n("VAEDecode", {"samples": ["9", 0], "vae": ["4", 0]})
    wf["11"] = _n("SaveImage", {"images": ["10", 0], "filename_prefix": prefix})
    return wf


# ------------------------------------------------------------------ edit: Qwen Rapid-AIO
def qwen_aio(image="example.png", prompt="describe the edit here", seed=0, model=None, prefix="edit/qwen_aio", **_):
    m = _main("qwen_aio", model)
    return {
        "1": _n("CheckpointLoaderSimple", {"ckpt_name": m["rel"]}, "Model"),
        "4": _n("LoadImage", {"image": image}, "Input image (our own generated image)"),
        "5": _n("ImageScaleToTotalPixels", {"image": ["4", 0], "upscale_method": "lanczos", "megapixels": 1.0}),
        "6": _n("TextEncodeQwenImageEditPlus", {"clip": ["1", 1], "vae": ["1", 2], "image1": ["5", 0], "prompt": prompt},
                "✏️ EDIT INSTRUCTION — write here (English)"),
        "7": _n("TextEncodeQwenImageEditPlus", {"clip": ["1", 1], "vae": ["1", 2], "image1": ["5", 0], "prompt": ""}, TITLE_NEG),
        "9": _n("VAEEncode", {"pixels": ["5", 0], "vae": ["1", 2]}),
        "10": _n("KSampler", {"model": ["1", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["9", 0],
                              "seed": seed, "steps": _d(m, "steps", 4), "cfg": _d(m, "cfg", 1.0),
                              "sampler_name": _d(m, "sampler", "euler_ancestral"), "scheduler": _d(m, "scheduler", "beta"),
                              "denoise": 1.0}),
        "11": _n("VAEDecode", {"samples": ["10", 0], "vae": ["1", 2]}),
        "12": _n("SaveImage", {"images": ["11", 0], "filename_prefix": prefix}),
    }


# ------------------------------------------------------------------ video: Wan 2.2 I2V (high/low pair)
def wan22_i2v(image="example.png", prompt="describe the motion and the scene here",
              negative="static, blurry, distorted, low quality", seed=0, loras=None, width=None, height=None,
              length=None, prefix="video/wan22", **_):
    m = _main("wan22_i2v", None)
    low = next(x for x in R.reg()["models"] if x["key"] == "wan_low")
    lh, ll = R.one("wan22_i2v", "speed_lora", "high"), R.one("wan22_i2v", "speed_lora", "low")
    te, vae = R.one("wan22_i2v", "te"), R.one("wan22_i2v", "vae")
    wf = {
        "1": _n("UNETLoader", {"unet_name": m["rel"], "weight_dtype": "default"}, "Wan HIGH"),
        "2": _n("UNETLoader", {"unet_name": low["rel"], "weight_dtype": "default"}, "Wan LOW"),
        "3": _n("LoraLoaderModelOnly", {"model": ["1", 0], "lora_name": lh["rel"], "strength_model": 1.0}, "Lightning HIGH"),
        "4": _n("LoraLoaderModelOnly", {"model": ["2", 0], "lora_name": ll["rel"], "strength_model": 1.0}, "Lightning LOW"),
    }
    hi, nid = _lora_chain(wf, ["3", 0], "wan22_i2v", loras, 100, part="high")
    lo, nid = _lora_chain(wf, ["4", 0], "wan22_i2v", loras, nid, part="low")
    steps = _d(m, "steps", 4)
    wf.update({
        "5": _n("ModelSamplingSD3", {"model": hi, "shift": _d(m, "shift", 8.0)}),
        "6": _n("ModelSamplingSD3", {"model": lo, "shift": _d(m, "shift", 8.0)}),
        "7": _n("CLIPLoader", {"clip_name": te["rel"], "type": te.get("clip_type", "wan")}),
        "8": _n("VAELoader", {"vae_name": vae["rel"]}),
        "9": _n("CLIPTextEncode", {"clip": ["7", 0], "text": prompt}, "✏️ MOTION PROMPT — write here (English)"),
        "10": _n("CLIPTextEncode", {"clip": ["7", 0], "text": negative}, TITLE_NEG),
        "11": _n("LoadImage", {"image": image}, "Start image (our own generated image)"),
        "12": _n("WanImageToVideo", {"positive": ["9", 0], "negative": ["10", 0], "vae": ["8", 0], "start_image": ["11", 0],
                                     "width": width or _d(m, "width", 480), "height": height or _d(m, "height", 832),
                                     "length": length or _d(m, "length", 81), "batch_size": 1},
                "Size + length (frames = 16*seconds+1)"),
        "13": _n("KSamplerAdvanced", {"model": ["5", 0], "positive": ["12", 0], "negative": ["12", 1], "latent_image": ["12", 2],
                                      "add_noise": "enable", "noise_seed": seed, "steps": steps, "cfg": 1.0,
                                      "sampler_name": "euler", "scheduler": "simple", "start_at_step": 0,
                                      "end_at_step": steps // 2, "return_with_leftover_noise": "enable"}),
        "14": _n("KSamplerAdvanced", {"model": ["6", 0], "positive": ["12", 0], "negative": ["12", 1], "latent_image": ["13", 0],
                                      "add_noise": "disable", "noise_seed": seed, "steps": steps, "cfg": 1.0,
                                      "sampler_name": "euler", "scheduler": "simple", "start_at_step": steps // 2,
                                      "end_at_step": steps, "return_with_leftover_noise": "disable"}),
        "15": _n("VAEDecode", {"samples": ["14", 0], "vae": ["8", 0]}),
        "16": _n("SaveAnimatedWEBP", {"images": ["15", 0], "filename_prefix": prefix, "fps": float(_d(m, "fps", 16)),
                                      "lossless": False, "quality": 85, "method": "default"}),
    })
    return wf


# ------------------------------------------------------------------ video+audio: MiniMax H3
# Built from the official ComfyUI template (see h3_template.json, captured on the pod): the template's model / LoRA
# loaders are re-pointed to registry entries, so the node graph stays exactly what Comfy ships.
H3_TPL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "h3")


def _h3(fam, image=None, prompt=None, seed=None, loras=None, model=None, prefix=None, refs=None, **kw):
    import h3_adapt  # written on the pod after inspecting the official template
    return h3_adapt.build(fam, _main(fam, model), image=image, prompt=prompt, seed=seed, loras=loras,
                          prefix=prefix or f"video/{fam}", refs=refs, **kw)


def h3_fl2va(**kw):
    return _h3("h3_fl2va", **kw)


def h3_ref2va(**kw):
    return _h3("h3_ref2va", **kw)


BUILDERS = {"zimage": zimage, "illustrious": illustrious, "qwen_image21": qwen_image21, "flux3_klein": flux3_klein, "qwen_aio": qwen_aio, "wan22_i2v": wan22_i2v,
            "h3_fl2va": h3_fl2va, "h3_ref2va": h3_ref2va}
UI_NAMES = {"zimage": "ZC 1 - image (Z-Image family)", "illustrious": "ZC 4 - image anime (Illustrious)",
            "qwen_image21": "ZC 7 - image by text or references (Qwen-Image 2.1)",
            "flux3_klein": "ZC 8 - image by text or references (FLUX 3 Klein)",
            "qwen_aio": "ZC 2 - edit (Qwen Rapid-AIO NSFW)", "wan22_i2v": "ZC 3 - video (Wan 2.2 I2V + LoRA)",
            "h3_fl2va": "ZC 5 - video+audio (MiniMax H3 + LoRA)", "h3_ref2va": "ZC 6 - video+audio by references (H3)"}


def build(fam, **kw):
    kw.setdefault("seed", random.randint(0, 2**31 - 1))
    return BUILDERS[R.family(fam)["template"]](**kw)


def export_all():
    """one UI workflow per family that has its models; every LoRA of the family included at strength 0"""
    os.makedirs(EXPORT_DIR, exist_ok=True)
    done, skipped = [], {}
    for fam, f in R.reg()["families"].items():
        if fam not in BUILDERS:
            continue
        try:
            wf = build(fam, seed=0)
        except Exception as e:
            skipped[fam] = repr(e)[:150]
            continue
        name = UI_NAMES[fam] + (" [testing]" if f.get("status") != "ready" else "")
        json.dump(wf, open(f"{EXPORT_DIR}/{name}.json", "w"), indent=1, ensure_ascii=False)
        done.append(name)
    return done, skipped
