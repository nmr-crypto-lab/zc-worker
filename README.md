# zc-worker

One Docker image for image/video generation (ComfyUI + model registry + mandatory age filter) that runs either as a
**RunPod Serverless worker** or on a **normal pod** with the **same API**. The site only changes the base URL.

Models are not in the image. They live on the global volume `zc/models`, listed in `app/models.yaml`.
A `zc/boot/models.yaml` on the volume overrides the baked copy, so new models need no rebuild.

## Modes

| | `ZC_MODE=serverless` | `ZC_MODE=pod` |
|---|---|---|
| Volume path | `/runpod-volume/zc` (auto) | `/workspace/zc` (auto) |
| API | RunPod endpoint `/run`, `/runsync`, `/status` | same API on port 8000 |
| Outputs | volume `zc/output` + inline base64 in the response (R2 optional) | volume `zc/output` |
| Extras | none | Jupyter/SSH, age watcher, auto-stop (`ZC_MAX_HOURS`), `ZC_FETCH=1`, `ZC_UI=1` (ComfyUI on 8188) |

## Environment

- **R2** (optional, later for the site): `ZC_R2_ENDPOINT`, `ZC_R2_BUCKET`, `ZC_R2_KEY_ID`, `ZC_R2_SECRET`.
  Without it, results stay on the volume and serverless also returns them inline (`ZC_INLINE_MAX`, default 15 MB).
- **Civitai token** (only for `fetch_models.py`): `CIVITAI_TOKEN`.

## API

```json
{"input": {"action": "models"}}
{"input": {"family": "zimage", "prompt": "...", "seed": 1}}
{"input": {"family": "h3_fl2va", "images": [{"path": "image/<job>/x_00001_.png"}],
           "prompt": "<Picture 1> ...", "seconds": 5, "loras": {"h3_lora_dr34ml4y": 0.7}}}
{"input": {"family": "h3_ref2va", "images": [{"path": "..."}, {"path": "..."}], "prompt": "<Picture 1> and <Picture 2> ..."}}
```

Families: `zimage`, `illustrious`, `qwen_aio`, `wan22_i2v`, `h3_fl2va`, `h3_ref2va`.

## Safety (always on)

- Prompts about minors are refused.
- Input images are accepted only from our own generated results (`zc/output` on the volume, or R2), and are re-checked.
- Every output passes the age gate before it is returned. That means InsightFace with a ViT age classifier, plus an
  anime tagger on the whole image.
- Anything flagged or uncheckable is deleted. Only metadata is logged.
- No face swap, no photos of real people.

## Add a model

- **Existing family:** add one entry to `models.yaml` on the volume, then run `python app/fetch_models.py <key>` on a pod.
- **New family:** add a template in `app/templates.py`, then rebuild.

## User app (casual UI)
`/` serves `app/ui/app.html` (task-based app for everyday users, EN/RU/ID); the full studio moved to `/admin`.
Example media for the app are read from `<ZC_ROOT>/ui_ex/` on the volume first, then `app/ui/ex/` in the image.
