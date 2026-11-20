# ZC worker: ComfyUI (pinned) + registry/templates + age gate + RunPod handler.
# Models are NOT in the image: they live on the global volume (see app/models.yaml); pods copy them to a local
# NVMe cache at start (app/cache_models.py).
FROM runpod/pytorch:2.1.0-cu1320-torch280-ubuntu2404
LABEL org.opencontainers.image.source=https://github.com/nmr-crypto-lab/zc-worker

ARG COMFY_TAG=v0.45.6
ENV PIP_BREAK_SYSTEM_PACKAGES=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 HF_HUB_ENABLE_HF_TRANSFER=1

RUN apt-get update && apt-get install -y --no-install-recommends git ffmpeg libgl1 libglib2.0-0 build-essential python3-dev && rm -rf /var/lib/apt/lists/* \
 && command -v python >/dev/null || ln -sf "$(command -v python3)" /usr/local/bin/python

# ComfyUI core only: every template (Z-Image, SDXL/Illustrious, Qwen edit, Wan 2.2, MiniMax H3) uses built-in nodes
RUN git clone --depth 1 --branch ${COMFY_TAG} https://github.com/comfyanonymous/ComfyUI /opt/ComfyUI \
 && pip install -r /opt/ComfyUI/requirements.txt

COPY requirements.txt /opt/zc/requirements.txt
# debian-installed packages (cryptography, ...) have no pip RECORD and cannot be upgraded in place
RUN pip install --ignore-installed cryptography \
 && pip install -r /opt/zc/requirements.txt
# the CPU 'onnxruntime' shadows the GPU build (age gate would run on CPU)
RUN (pip uninstall -y onnxruntime || true) \
 && pip install --force-reinstall --no-deps onnxruntime-gpu \
 && python -c "import insightface, onnxruntime, runpod, boto3, yaml, fastapi, websocket; print('deps ok', onnxruntime.__version__, onnxruntime.get_available_providers())"

COPY app /opt/zc/app
RUN chmod +x /opt/zc/app/start.sh && python -m py_compile /opt/zc/app/*.py && mkdir -p /cache/models /root/zclogs

EXPOSE 8188 8000
CMD ["/opt/zc/app/start.sh"]
