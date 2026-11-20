#!/bin/bash
# One image, two modes:
#   ZC_MODE=serverless  ComfyUI (localhost) + RunPod serverless handler       (volume at /runpod-volume)
#   ZC_MODE=pod         ComfyUI + the same handler as an HTTP API on :8000 + Jupyter/SSH + age watcher
#                       + auto-stop watchdog                                    (volume at /workspace)
# Optional on pods: ZC_FETCH=1 downloads missing registry models; ZC_UI=1 exposes the ComfyUI UI on :8188.
set -o pipefail
APP=/opt/zc/app
export PYTHONPATH=$APP
eval "$(python -c 'import zc_paths as P; print(f"ROOT={P.ROOT} MODE={P.MODE} OUTPUT={P.OUTPUT} LOGS={P.LOGS}")')"
export ZC_MODE=$MODE
mkdir -p $LOGS $OUTPUT
LOG=$LOGS/start-$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1
T0=$(date +%s)
el() { echo "[zc +$(( $(date +%s) - T0 ))s] $*"; }
el "start mode=$MODE root=$ROOT output=$OUTPUT gpu=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null)"

if [ ! -d "$ROOT/models" ]; then el "WARNING: no models at $ROOT/models (is the global volume attached?)"; fi
C=/opt/ComfyUI
rm -rf $C/models && ln -s $ROOT/models $C/models
export HF_HOME=$ROOT/hf_home

if [ "$MODE" = "pod" ]; then
  # auto-stop: never leave a pod running by accident
  ( sleep $(( ${ZC_MAX_HOURS:-3} * 3600 )); el "WATCHDOG: stopping pod"; runpodctl stop pod "$RUNPOD_POD_ID" ) &
  [ -x /start.sh ] && (/start.sh >/dev/null 2>&1 &)          # Jupyter / SSH from the RunPod base image
  # copy logs to the volume now and then (never append to files on the volume)
  ( while true; do sleep 60; mkdir -p $ROOT/logs; timeout 20 cp -f $LOGS/*.log $LOGS/*.jsonl $ROOT/logs/ 2>/dev/null; done ) &
  if [ "${ZC_FETCH:-0}" = "1" ]; then
    ( python -u $APP/fetch_models.py 2>&1 | grep --line-buffered -v -E 'it/s|%\|' > $LOGS/fetch.log ) &
  fi
  mkdir -p $ROOT/comfy_user $ROOT/input
  rm -rf $C/user $C/input && ln -s $ROOT/comfy_user $C/user && ln -s $ROOT/input $C/input
fi

LISTEN=127.0.0.1; [ "${ZC_UI:-0}" = "1" ] && LISTEN=0.0.0.0
cd $C
python main.py --listen $LISTEN --port 8188 --output-directory $OUTPUT --disable-auto-launch ${ZC_COMFY_ARGS:-} \
  > $LOGS/comfy.log 2>&1 &
el "ComfyUI starting on $LISTEN:8188"

if [ "$MODE" = "pod" ]; then
  # the watcher also gates files made directly in the ComfyUI UI
  ( python -u $APP/age_watcher.py > $LOGS/watch.log 2>&1 ) &
  el "handler API on :8000 (same /run /runsync /status as serverless)"
  exec python -u $APP/handler.py --rp_serve_api --rp_api_host 0.0.0.0 --rp_api_port 8000
else
  exec python -u $APP/handler.py
fi
