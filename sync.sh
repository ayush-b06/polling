#!/usr/bin/env bash
# Pull the newest downloaded copies of any jobwatch file into this folder.
#
#   ./sync.sh
#
# Downloading a file you already have makes the browser save it as main-1.py,
# "main (2).py" and so on. This finds the newest copy of each project file in
# ~/Downloads, strips that suffix, and moves it into place — so you download
# everything and run one command instead of renaming files by hand.
#
# Every file it replaces is backed up to .bak/ first, and if the folder is a git
# repo it prints a diff summary so you can see exactly what changed.

set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOWNLOADS="${DOWNLOADS:-$HOME/Downloads}"
BACKUP="$PROJECT/.bak"

FILES=(main.py adapters.py check.py dashboard.py discover.py why.py sync.sh
       config.yaml companies.txt requirements.txt README.md
       manual-checklist.md Dockerfile poll.yml)

mkdir -p "$BACKUP"
moved=0
skipped=0

for f in "${FILES[@]}"; do
  stem="${f%.*}"
  ext="${f##*.}"

  # newest match for: main.py, main-1.py, main (1).py, main-2.py ...
  # (a while-read loop, not `xargs ls -t` — with no matches that lists the
  #  whole current directory and happily "updates" every file with garbage)
  newest=""
  while IFS= read -r -d '' cand; do
    if [ -z "$newest" ] || [ "$cand" -nt "$newest" ]; then newest="$cand"; fi
  done < <(find "$DOWNLOADS" -maxdepth 1 -type f \
             \( -name "$stem.$ext" -o -name "$stem-[0-9]*.$ext" -o -name "$stem ([0-9]*).$ext" \) \
             -print0 2>/dev/null)

  [ -z "$newest" ] && continue
  [ -f "$newest" ] || continue

  # unchanged? leave it alone and bin the download
  if [ -f "$PROJECT/$f" ] && cmp -s "$newest" "$PROJECT/$f"; then
    rm -f "$newest"
    skipped=$((skipped + 1))
    continue
  fi

  [ -f "$PROJECT/$f" ] && cp "$PROJECT/$f" "$BACKUP/$f"

  dest="$PROJECT/$f"
  [ "$f" = "poll.yml" ] && { mkdir -p "$PROJECT/.github/workflows"; dest="$PROJECT/.github/workflows/$f"; }

  mv "$newest" "$dest"
  echo "  updated  $f"
  moved=$((moved + 1))
done

# clear out any leftover duplicate downloads of these files
for f in "${FILES[@]}"; do
  stem="${f%.*}"; ext="${f##*.}"
  find "$DOWNLOADS" -maxdepth 1 -type f \
       \( -name "$stem-[0-9]*.$ext" -o -name "$stem ([0-9]*).$ext" \) -delete 2>/dev/null
done

echo
echo "  $moved updated, $skipped already current"

if [ "$moved" -eq 0 ]; then
  echo "  nothing new in $DOWNLOADS"
  exit 0
fi

chmod +x "$PROJECT/sync.sh" 2>/dev/null

# sanity: config types must all have adapters, or the poller silently watches nothing
if command -v python3 >/dev/null && [ -f "$PROJECT/config.yaml" ]; then
  (cd "$PROJECT" && python3 - <<'PY' 2>/dev/null
import sys, yaml
sys.path.insert(0, ".")
try:
    import adapters
    cfg = yaml.safe_load(open("config.yaml"))
    missing = {s["type"] for s in cfg["sources"]} - set(adapters.REGISTRY)
    print("  ! config uses types with no adapter: " + ", ".join(sorted(missing))
          if missing else "  config and adapters agree")
except Exception as e:
    print(f"  ! could not verify: {type(e).__name__}: {e}")
PY
  )
fi

if [ -d "$PROJECT/.git" ]; then
  echo
  (cd "$PROJECT" && git --no-pager diff --stat)
  echo "  revert anything with:  git checkout -- <file>"
else
  echo "  backups of replaced files are in .bak/"
fi
