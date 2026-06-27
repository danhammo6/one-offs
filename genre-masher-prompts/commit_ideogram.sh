#!/usr/bin/env bash
# Stage ONLY the ideogram4 data + the poster JPEGs actually referenced by the
# CSV (never a blanket `git add images/` — the dir holds orphan posters from
# aborted runs), then commit if anything changed. Prints "COMMITTED <n>" or
# "NOCHANGE". Safe to call repeatedly.
set -u
REPO=/Users/dhammond/spike/one-offs
PROJ="$REPO/genre-masher-prompts"
CSV="$PROJ/mashups_ideogram4.csv"
VENV="$PROJ/.venv/bin/python"
cd "$REPO" || exit 1

[ -f "$CSV" ] || { echo "NOCHANGE (no csv)"; exit 0; }

count=$("$VENV" -c "
import csv
rows=list(csv.DictReader(open('$CSV')))
print(sum(1 for r in rows if r.get('image_file')))
")

# Stage the data files (always safe — tracked).
git add genre-masher-prompts/mashups_ideogram4.csv \
        genre-masher-prompts/mashups_ideogram4.html \
        genre-masher-prompts/mashups_ideogram4.json \
        genre-masher-prompts/generate_mashups.py \
        genre-masher-prompts/auto_ideogram.sh \
        genre-masher-prompts/commit_ideogram.sh \
        genre-masher-prompts/commit_watcher.sh \
        genre-masher-prompts/.gitignore 2>/dev/null

# Stage exactly the images the CSV references, by filename.
"$VENV" -c "
import csv
for r in csv.DictReader(open('$CSV')):
    f=r.get('image_file')
    if f: print('genre-masher-prompts/images/ideogram4/'+f)
" | while IFS= read -r path; do
  [ -f "$REPO/$path" ] && git add "$path" 2>/dev/null
done

# Anything actually staged?
if git diff --cached --quiet; then
  echo "NOCHANGE"
  exit 0
fi

git commit -S --no-verify -m "ideogram4 batch: $count posters (auto)" >/dev/null 2>&1

# Auto-push the current branch. --force is never used; a rejected push (e.g.
# remote moved) is reported but not fatal — the next batch will try again.
branch=$(git rev-parse --abbrev-ref HEAD)
if git push origin "$branch" >/dev/null 2>&1; then
  echo "COMMITTED $count (pushed $branch)"
else
  echo "COMMITTED $count (PUSH FAILED for $branch — will retry next batch)"
fi
