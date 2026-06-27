#!/usr/bin/env bash
# Unattended ideogram4 batch driver.
#
# Runs batches of 10 --append, switching resolution once we hit 40 images:
#   < 40 images : 1152x1728  (krea-matched, ~2 MP)
#   >= 40 images: 1664x2496  (2:3 "native 2K" budget, ~4.15 MP)
# Gates each batch on ComfyUI being reachable (the box flakes / gets restarted),
# counts ACTUAL image files (robust to failed renders), and caps itself so it
# can never run away while unattended.
set -u
cd /Users/dhammond/spike/one-offs/genre-masher-prompts

SERVER="192.168.33.101:8188"
IMAGES_DIR="images/ideogram4"
VENV=".venv/bin/python"
STOP_AT=200                 # hard cap on total ideogram4 images (160 of them hi-res)
MAX_HOURS=48                # wall-clock safety cap (weekend run)
SWITCH_AT=40                # image count at which we go to native-2K res
LO_RES="1152x1728"
HI_RES="1664x2496"
WAIT_PID="${1:-}"           # optional: PID of an in-flight batch to wait on first
LOCK="/tmp/auto_ideogram.lock"

# Single-instance guard: refuse to start a second driver (would race the CSV and
# collide poster filenames). Stale lock (dead PID) is reclaimed.
if [ -f "$LOCK" ]; then
  other=$(cat "$LOCK" 2>/dev/null)
  if [ -n "$other" ] && kill -0 "$other" 2>/dev/null; then
    echo "another driver (PID $other) is already running; exiting." >&2
    exit 1
  fi
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

start_ts=$(date +%s)
log() { echo "[$(date '+%H:%M:%S')] $*"; }

# Count is taken from the CSV (rows with a saved image_file), NOT a file glob:
# the images dir still holds orphaned posters from earlier aborted runs, and
# --append numbers new posters off the CSV row count. CSV is the source of truth.
CSV="mashups_ideogram4.csv"
img_count() {
  [ -f "$CSV" ] || { echo 0; return; }
  "$VENV" -c "
import csv,sys
try:
    rows=list(csv.DictReader(open('$CSV')))
    print(sum(1 for r in rows if r.get('image_file')))
except Exception:
    print(0)
"
}

server_up() {
  local code
  code=$(curl -s -m 5 -o /dev/null -w "%{http_code}" "http://$SERVER/system_stats" 2>/dev/null)
  [ "$code" = "200" ]
}

# Block until the server answers 200, polling every 30s. Never gives up (the
# user may restart the box at any time) but logs every couple minutes.
wait_for_server() {
  local tries=0
  while ! server_up; do
    if [ $((tries % 4)) -eq 0 ]; then log "waiting for ComfyUI at $SERVER ..."; fi
    sleep 30
    tries=$((tries + 1))
  done
}

# 1) Wait for any in-flight batch to finish so we don't double-run.
if [ -n "$WAIT_PID" ]; then
  log "waiting for in-flight batch PID $WAIT_PID to finish before driving..."
  while kill -0 "$WAIT_PID" 2>/dev/null; do sleep 30; done
  log "in-flight batch PID $WAIT_PID has exited. starting driver."
fi

batch_n=0
while true; do
  count=$(img_count)
  elapsed_h=$(( ($(date +%s) - start_ts) / 3600 ))

  if [ "$count" -ge "$STOP_AT" ]; then
    log "reached $count images (cap $STOP_AT). stopping."
    break
  fi
  if [ "$elapsed_h" -ge "$MAX_HOURS" ]; then
    log "hit ${MAX_HOURS}h wall-clock cap at $count images. stopping."
    break
  fi

  if [ "$count" -lt "$SWITCH_AT" ]; then RES="$LO_RES"; else RES="$HI_RES"; fi
  batch_n=$((batch_n + 1))
  log "=== batch #$batch_n : have $count images, rendering 10 @ $RES (append) ==="

  wait_for_server
  log "server up; launching batch #$batch_n"

  blog="ideogram4_auto_batch${batch_n}.log"
  $VENV -u generate_mashups.py 10 --backend ideogram4 --append \
        --poster-size "$RES" --comfy-server "$SERVER" > "$blog" 2>&1
  rc=$?

  new_count=$(img_count)
  oks=$(grep -c '\[ok\]' "$blog" 2>/dev/null || echo 0)
  errs=$(grep -c '\[img:' "$blog" 2>/dev/null || echo 0)
  log "batch #$batch_n done (rc=$rc): $oks ok, $errs img-errors. images now: $new_count (was $count)"

  # If the batch made zero progress, the box probably died mid-run. Pause,
  # then loop (wait_for_server will block until it's back).
  if [ "$new_count" -le "$count" ]; then
    log "no new images this batch; pausing 60s before retry."
    sleep 60
  fi
done

log "driver finished. final image count: $(img_count)"
