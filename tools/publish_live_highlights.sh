#!/usr/bin/env bash
# Live highlights publish routine. See tools/LIVE_HIGHLIGHTS_RUNBOOK.md.
# Never force-pushes, never resets, never stashes. Exits non-zero on any failed check.
set -u
cd "$(dirname "$0")/.." || exit 2
FILES="live_highlights.json nfl/live_highlights.json"
RAW="https://raw.githubusercontent.com/richdiskin/cover-window-data/main"
retry() { local n=0; until "$@"; do n=$((n+1)); [ $n -ge 4 ] && return 1; sleep $((n*10)); done; }

run_write() {
  python3 tools/live_highlights.py --write "$@" > /tmp/live_hl_summary.json || { echo "STOP: live_highlights.py failed"; cat /tmp/live_hl_summary.json; return 1; }
  cat /tmp/live_hl_summary.json
  python3 - <<'PY' || return 1
import json, sys
s = json.load(open("/tmp/live_hl_summary.json"))
if not s.get("pass") or s.get("errors") or not s.get("feeds_untouched"):  # status_skipped games are retried next run
    print("STOP: summary has errors"); sys.exit(1)
PY
}

# 1-3. fetch, check for local changes, fast-forward
retry git fetch origin || { echo "STOP: fetch failed"; exit 1; }
dirty=$(git status --porcelain --untracked-files=no)
if [ -n "$dirty" ]; then echo "STOP: local changes:"; echo "$dirty"; exit 1; fi
retry git pull --ff-only origin main || { echo "STOP: pull --ff-only failed"; exit 1; }

# 4. write
run_write "$@" || exit 1

# 5. publish only if the diff touches only the two live files
changed=$( { git diff --name-only; git ls-files --others --exclude-standard -- $FILES; } | sort -u)
if [ -z "$changed" ]; then echo "NO CHANGE"; exit 0; fi
for f in $changed; do case " $FILES " in *" $f "*) ;; *) echo "STOP: unexpected change: $f"; exit 1;; esac; done

# 6. commit
read H F < <(python3 -c "import json;s=json.load(open('/tmp/live_hl_summary.json'));print(s['halftime_found'],s['final_found'])")
msg="Live highlights $(TZ=America/New_York date '+%Y-%m-%d %-I:%M %p') ET (${H} halftime, ${F} final)"
git add $FILES && git commit -q -m "$msg" || { echo "STOP: commit failed"; exit 1; }

# 7. push; on rejection rebase and rerun once. Never force.
if ! retry git push origin HEAD:main; then
  echo "push rejected: rebasing and rerunning once"
  if ! retry git pull --rebase origin main; then git rebase --abort 2>/dev/null; echo "STOP: rebase failed (aborted, nothing pushed)"; exit 1; fi
  run_write "$@" || exit 1
  changed=$( { git diff --name-only; git ls-files --others --exclude-standard -- $FILES; } | sort -u)
  if [ -z "$changed" ]; then retry git push origin HEAD:main || { echo "STOP: second push failed"; exit 1; }; else
  for f in $changed; do case " $FILES " in *" $f "*) ;; *) echo "STOP: unexpected change: $f"; exit 1;; esac; done
  read H F < <(python3 -c "import json;s=json.load(open('/tmp/live_hl_summary.json'));print(s['halftime_found'],s['final_found'])")
  msg="Live highlights $(TZ=America/New_York date '+%Y-%m-%d %-I:%M %p') ET (${H} halftime, ${F} final)"
  git add $FILES && git commit -q -m "$msg" && retry git push origin HEAD:main || { echo "STOP: second push failed"; exit 1; }
  fi
fi

# 8. verify
retry git fetch origin
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || { echo "STOP: HEAD != origin/main"; exit 1; }
for f in $FILES; do
  ok=0
  for i in 1 2 3 4 5 6; do
    if curl -fsS "$RAW/$f?t=$(date +%s)" | python3 -c "import json,sys;d=json.load(sys.stdin);assert d['schema']=='cw_live_highlights_v1'" 2>/dev/null; then ok=1; break; fi
    sleep 30
  done
  [ $ok = 1 ] || { echo "WARN: raw $f did not parse yet (CDN can lag up to 5 min)"; }
done
echo "PUSHED $(git rev-parse --short HEAD) $msg"
