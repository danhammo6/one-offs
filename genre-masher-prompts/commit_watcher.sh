#!/usr/bin/env bash
# Independent commit watcher: polls the ideogram4 image count and commits (via
# commit_ideogram.sh) every time it crosses a batch boundary (multiple of 10),
# so each completed batch of 10 lands as its own commit. Runs detached from the
# generator and from any assistant session. Exits when the driver process is
# gone AND no further progress is seen.
set -u
PROJ=/Users/dhammond/spike/one-offs/genre-masher-prompts
VENV="$PROJ/.venv/bin/python"
CSV="$PROJ/mashups_ideogram4.csv"
DRIVER_PID="${1:-}"          # the auto_ideogram.sh PID, so we know when to stop
BATCH=10
cd "$PROJ" || exit 1

log() { echo "[$(date '+%H:%M:%S')] $*"; }
count() {
  [ -f "$CSV" ] || { echo 0; return; }
  "$VENV" -c "
import csv
try: print(sum(1 for r in csv.DictReader(open('$CSV')) if r.get('image_file')))
except Exception: print(0)
"
}

last_committed=$(( $(count) / BATCH * BATCH ))   # floor to batch boundary already on disk
log "watcher start. current images: $(count); last batch boundary: $last_committed"

while true; do
  c=$(count)
  boundary=$(( c / BATCH * BATCH ))
  if [ "$boundary" -gt "$last_committed" ]; then
    log "crossed boundary: $c images (was committed through $last_committed). committing."
    result=$(bash "$PROJ/commit_ideogram.sh")
    log "commit result: $result"
    last_committed=$boundary
  fi

  # Stop condition: driver gone, no batch python running, and we've committed
  # everything currently on disk.
  driver_alive=0; [ -n "$DRIVER_PID" ] && kill -0 "$DRIVER_PID" 2>/dev/null && driver_alive=1
  batch_alive=0; pgrep -f "generate_mashups.py" >/dev/null 2>&1 && batch_alive=1
  if [ "$driver_alive" -eq 0 ] && [ "$batch_alive" -eq 0 ]; then
    # Final sweep: commit any remainder (partial batch) before exiting.
    if [ "$c" -gt "$last_committed" ]; then
      log "driver gone; final commit of remainder ($c images)."
      result=$(bash "$PROJ/commit_ideogram.sh")
      log "final commit result: $result"
    fi
    log "driver and batches gone. watcher exiting at $c images."
    break
  fi
  sleep 30
done
