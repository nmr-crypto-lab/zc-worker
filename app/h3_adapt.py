"""MiniMax H3 workflows in API format, reproducing the official ComfyUI templates
(video_minimax_h3_i2v.json / video_minimax_h3_r2v.json, captured in /workspace/zc/h3tpl) with models from the registry.

fl2va: first frame (+ optional last frame) + prompt -> video with audio.
ref2va: up to 9 reference images (our own generated characters) + prompt -> video with audio.
       Prompts refer to references as <Picture 1>, <Picture 2>, ...
"""
import math
import registry as R

FPS = 24


def frames_for(seconds):
    """same rule as the template: >=5 frames, rounded up to the 17k+5 grid"""
    n = max(5, round(seconds * FPS))
    return n + (5 - (n % 17)) % 17


def wh_for(aspect="9:16", megapixels=0.9, step=32):
    a, b = [float(x) for x in aspect.split(":")]
    h = math.sqrt(megapixels * 1e6 * b / a)
    w = h * a / b
    return int(round(w / step) * step), int(round(h / step) * step)


def _n(cls, inputs, title=None):
    d = {"class_type": cls, "inputs": inputs}
    if title:
        d["_meta"] = {"title": title}
    return d


def _common(fam, main, wf, turbo, loras):
    te, vae, avae = R.one(fam, "te"), R.one(fam, "vae"), R.one(fam, "aux")
    wf["1"] = _n("UNETLoader", {"unet_name": main["rel"], "weight_dtype": "default"}, "Model")
    wf["2"] = _n("CLIPLoader", {"clip_name": te["rel"], "type": "minimax", "device": "default"})
    wf["3"] = _n("VAELoader", {"vae_name": vae["rel"]}, "Video VAE")
    wf["4"] = _n("VAELoader", {"vae_name": avae["rel"]}, "Audio VAE")
    src, nid = ["1", 0], 100
    sp = [m for m in R.speed_loras(fam) if not m.get("optional")] or R.speed_loras(fam)
    if turbo and sp:
        wf["5"] = _n("LoraLoaderModelOnly", {"model": src, "lora_name": sp[0]["rel"], "strength_model": 1.0},
                     f"Turbo LoRA ({sp[0].get('label', '')})")
        src = ["5", 0]
    for m in R.loras(fam):
        if loras is not None and m["key"] not in loras:
            continue
        s = 0.0 if loras is None else float(loras[m["key"]])
        wf[str(nid)] = _n("LoraLoaderModelOnly", {"model": src, "lora_name": m["rel"], "strength_model": s},
                          f"LoRA {m.get('label', m['key'])}{' — ' + m['triggers'] if m.get('triggers') else ''}")
        src, nid = [str(nid), 0], nid + 1
    return src


def _sample_and_save(wf, model_src, cond, latent, seed, steps, prefix):
    wf["10"] = _n("RandomNoise", {"noise_seed": seed})
    wf["11"] = _n("KSamplerSelect", {"sampler_name": "res_multistep"})
    wf["12"] = _n("BasicScheduler", {"model": model_src, "scheduler": "simple", "steps": steps, "denoise": 1.0}, "Steps")
    wf["13"] = _n("BasicGuider", {"model": model_src, "conditioning": cond})
    wf["14"] = _n("SamplerCustomAdvanced", {"noise": ["10", 0], "guider": ["13", 0], "sampler": ["11", 0],
                                            "sigmas": ["12", 0], "latent_image": latent})
    wf["15"] = _n("VAEDecode", {"samples": ["14", 0], "vae": ["3", 0]})
    wf["16"] = _n("VAEDecodeAudio", {"samples": ["14", 0], "vae": ["4", 0]})
    wf["17"] = _n("CreateVideo", {"images": ["15", 0], "audio": ["16", 0], "fps": float(FPS)})
    wf["18"] = _n("SaveVideo", {"video": ["17", 0], "filename_prefix": prefix, "format": "auto", "codec": "auto"})
    return wf


def build(fam, main, image=None, prompt=None, seed=0, loras=None, prefix="video/h3", refs=None,
          seconds=None, megapixels=0.9, aspect="9:16", turbo=True, steps=None, last_image=None, **_):
    d = main.get("defaults") or {}
    seconds = seconds or d.get("seconds", 5)
    wf = {}
    src = _common(fam, main, wf, turbo, loras)
    if fam == "h3_fl2va":
        steps = steps or (d.get("steps", 8) if turbo else 20)
        wf["6"] = _n("LoadImage", {"image": image or "example.png"}, "First frame (our own generated image)")
        wf["7"] = _n("ImageScaleToTotalPixels", {"image": ["6", 0], "upscale_method": "lanczos",
                                                 "megapixels": megapixels, "resolution_steps": 32}, "Size (megapixels)")
        wf["8"] = _n("GetImageSize", {"image": ["7", 0]})
        inp = {"clip": ["2", 0], "vae": ["3", 0], "first_frame": ["7", 0],
               "prompt": prompt or "Describe the scene, motion, camera and sound. The image is <Picture 1>.",
               "width": ["8", 0], "height": ["8", 1], "length": frames_for(seconds)}
        if last_image:
            wf["19"] = _n("LoadImage", {"image": last_image}, "Last frame (optional)")
            wf["20"] = _n("ImageScale", {"image": ["19", 0], "upscale_method": "lanczos", "width": ["8", 0],
                                         "height": ["8", 1], "crop": "center"})
            inp["last_frame"] = ["20", 0]
        wf["9"] = _n("MiniMaxH3ImageToVideo", inp, "✏️ PROMPT + length (frames = 24*sec, 17k+5 grid)")
        return _sample_and_save(wf, src, ["9", 0], ["9", 1], seed, steps, prefix)

    if fam == "h3_ref2va":
        steps = steps or (4 if turbo else d.get("steps", 25))
        refs = refs or ["example.png"]
        w, h = wh_for(aspect, megapixels)
        inp = {"clip": ["2", 0], "vae": ["3", 0], "audio_vae": ["4", 0],
               "prompt": prompt or "Describe the scene. Refer to the characters as <Picture 1>, <Picture 2>...",
               "width": w, "height": h, "length": frames_for(seconds)}
        for i, r in enumerate(refs[:9]):
            wf[str(30 + i)] = _n("LoadImage", {"image": r}, f"Reference {i + 1} (our own generated character)")
            inp[f"ref_images.ref_image_{i}"] = [str(30 + i), 0]
        wf["9"] = _n("MiniMaxH3ReferenceToVideo", inp, "✏️ PROMPT + size + length")
        return _sample_and_save(wf, src, ["9", 0], ["9", 1], seed, steps, prefix)
    raise ValueError(fam)
