#!/bin/bash
# One image, two modes:
#   ZC_MODE=serverless  ComfyUI (localhost) + RunPod serverless handler              (volume at /runpod-volume)
#   ZC_MODE=pod         ComfyUI + ZC Studio UI/API on :8000 + Jupyter/SSH + age watcher
#                       + local NVMe model cache + auto-stop watchdog                (volume at /workspace)
# Pod options (env):
#   ZC_MAX_HOURS=3          auto-stop after N hours
#   ZC_CACHE=/cache/models  local model cache dir ("off" disables); ZC_CACHE_FAMILIES=h3_ref2va,zimage copies these first
#   ZC_GIT_REF=main         run the app code from GitHub (this branch) instead of the copy baked into the image
#   ZC_FETCH=1              download missing registry models;  ZC_UI=1  expose the ComfyUI UI on :8188
set -o pipefail
APP=/opt/zc/app
if [ -n "${ZC_GIT_REF:-}" ]; then
  # quick code updates without rebuilding the image
  if git clone -q --depth 1 --branch "$ZC_GIT_REF" "${ZC_REPO:-https://github.com/nmr-crypto-lab/zc-worker}" /opt/zc-git 2>/dev/null; then
    APP=/opt/zc-git/app; echo "[zc] app code from GitHub ($ZC_GIT_REF)"
  else
    echo "[zc] git clone failed, using the baked app code"
  fi
fi
export PYTHONPATH=$APP
eval "$(python -c 'import zc_paths as P; print(f"ROOT={P.ROOT} MODE={P.MODE} OUTPUT={P.OUTPUT} LOGS={P.LOGS}")')"
export ZC_MODE=$MODE
mkdir -p $LOGS $OUTPUT
LOG=$LOGS/start-$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1
T0=$(date +%s)
el() { echo "[zc +$(( $(date +%s) - T0 ))s] $*"; }
el "start mode=$MODE root=$ROOT output=$OUTPUT app=$APP gpu=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null)"

if [ ! -d "$ROOT/models" ]; then el "WARNING: no models at $ROOT/models (is the global volume attached?)"; fi
C=/opt/ComfyUI
export HF_HOME=$ROOT/hf_home

if [ "$MODE" = "pod" ]; then
  # auto-stop: never leave a pod running by accident
  ( sleep $(( ${ZC_MAX_HOURS:-3} * 3600 )); el "WATCHDOG: stopping pod"; sync; runpodctl stop pod "$RUNPOD_POD_ID" ) &
  [ -x /start.sh ] && (/start.sh >/dev/null 2>&1 &)          # Jupyter / SSH from the RunPod base image
  # copy logs to the volume now and then (never append to files on the volume: it can block)
  ( while true; do sleep 30; mkdir -p $ROOT/logs; timeout 20 cp -f $LOGS/*.log $LOGS/*.jsonl $LOGS/*.json $ROOT/logs/ 2>/dev/null; done ) &
  # models: symlinks to the volume now, local NVMe copies in the background
  rm -rf $C/models
  python -u $APP/cache_models.py --farm-only
  if [ "${ZC_CACHE:-/cache/models}" != "off" ]; then
    ( python -u $APP/cache_models.py > $LOGS/cache.log 2>&1 ) &
    el "model cache filling in the background ($LOGS/cache.log)"
  fi
  if [ "${ZC_FETCH:-0}" = "1" ]; then
    ( python -u $APP/fetch_models.py 2>&1 | grep --line-buffered -v -E 'it/s|%\|' > $LOGS/fetch.log ) &
  fi
  mkdir -p $ROOT/comfy_user $ROOT/input
  rm -rf $C/user $C/input && ln -s $ROOT/comfy_user $C/user && ln -s $ROOT/input $C/input
else
  rm -rf $C/models && ln -s $ROOT/models $C/models
fi

LISTEN=127.0.0.1; [ "${ZC_UI:-0}" = "1" ] && LISTEN=0.0.0.0
cd $C
python main.py --listen $LISTEN --port 8188 --output-directory $OUTPUT --disable-auto-launch ${ZC_COMFY_ARGS:-} \
  > $LOGS/comfy.log 2>&1 &
el "ComfyUI starting on $LISTEN:8188"

if [ "$MODE" = "pod" ]; then
  # the watcher also gates files made directly in the ComfyUI UI
  ( python -u $APP/age_watcher.py > $LOGS/watch.log 2>&1 ) &
  el "ZC Studio on :8000 (password: \$ZC_UI_PASSWORD or $ROOT/state/ui_password.txt)"
  cd $APP && exec python -u ui_server.py
else
  exec python -u $APP/handler.py
fi
