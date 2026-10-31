# Live highlights runbook (halftime and final clips)

`tools/live_highlights.py` adds in-game YouTube clips to two separate files, keyed by `game_id`:

- `live_highlights.json` (college, repo root)
- `nfl/live_highlights.json`

It never touches `games.json` or `nfl/games.json`. It checks their sha256 before and after each run and fails if either changed. Each game entry can have a `halftime` item and a `final` item. Each item has the YouTube id, title, channel, url, embed_url, upload time and found_at. Times are ISO UTC and exist for data only. The apps show no times from this file.

## Commands
- Dry run, writes nothing: `cd /workspace/cwfeed && python3 tools/live_highlights.py`
- Write files: `python3 tools/live_highlights.py --write`
- Other flags:
  - `--league college|nfl`
  - `--games ID,ID`
  - `--seed-scoreboard FILE`: ESPN scoreboard JSON used as the first-sight status for games already kicked off. This was used once at go-live.
- **Routine command:** `cd /workspace/cwfeed && tools/publish_live_highlights.sh`. It runs all the publish steps below.
- The script prints a JSON summary:
  - `games_checked`, `halftime_found`, `final_found`, `errors`, `pass`, `rejections`, `yt_requests`, `run_seconds`.
  - Per league: `halftime_eligible_new`, `not_eligible_new`, `halftime_searched`, `halftime_closed_no_clip`, `final_searched`, `halftime_total`, `final_total`, `found`, `clip_rule_errors`.

## Which games get which slot (Rich, Oct 31, 2026)
Status comes from the ESPN scoreboard: state, period and clock. The summary endpoint is the fallback.

- **Registration:**
  - A game is registered the first time the script sees it, from 36 h before now to 24 h after.
  - At that point it records `halftime_eligible`. The flag is true only if the game has not kicked off or is in the 1st or 2nd quarter before halftime. It is never flipped to true later.
- **Halftime:**
  - Searched only when `halftime_eligible` is true, the game has reached halftime or later, it is not yet in the 4th quarter, and kickoff was within 8 h.
  - Once the game reaches the 4th quarter or is final with no halftime clip, the search stops. These games are counted in `halftime_closed_no_clip`.
  - Final and nearly-final games, meaning final or in the 4th quarter or later at first sight, get only a final clip.
  - Go-live was about 5:00 PM ET on Oct 31, 2026. Games already past halftime at go-live were registered as not eligible, using the saved go-live scoreboard. They get finals only. Halftime is not backfilled.
- **Final:**
  - Searched when the game is final (ESPN `completed` or state `post`) and kickoff was within 36 h. If ESPN status is unavailable, a kickoff 3 h or more ago also qualifies.
  - The clip must be uploaded at least 2.5 h after kickoff.
- **No churn:** once a slot has a clip, it is kept.

## Clip rules
- **Channel and upload checks:**
  - Allowlisted official channels only, using `tools/highlights_allowlist.json` through highlights.py.
  - The title must name both teams and include a highlights word (highlights or condensed).
  - Upload date in ET must be on or after the game date.
- **Halftime title:**
  - Must say first half, 1st half or halftime.
  - Rejected if it is a halftime show, performance, band, second half, full game or final.
- **Final title:**
  - Full-game or condensed highlights.
  - Rejected for quarter or half clips and for unit or player clips: defensive, offensive, special teams, a position plus a name ("RB Name"), every catch, top plays, season, career.
- **Playback checks:**
  - Embeddable: the youtube-nocookie embed page returns playability OK, and `playableInEmbed` is not false.
  - Public, family-safe and not live.
  - oEmbed returns 200.
- **Access limits:** no cookies, no YouTube login, no API key. At most 3 YouTube requests in flight.
- **Where clips come from:**
  - Channel RSS feeds for the game's TV network, both conferences and both team channels.
  - One date-sorted YouTube search.
  - One channel search.
- **Upload time:**
  - Taken from the channel feed when available. Otherwise the watch page.
  - YouTube watch pages are bot-gated from the box (LOGIN_REQUIRED). The fallback is the thumbnail ETag, which is minutes before publish.
  - `upload_time_source` records which source was used.

## Publish steps (what `tools/publish_live_highlights.sh` does)
1. Run `git fetch origin`, retried on TLS drops.
2. If `git status --porcelain --untracked-files=no` shows any local change, stop and report. Untracked scratch such as `tools/__pycache__` is fine. Never reset, stash or force anything.
3. Run `git pull --ff-only origin main`.
4. Run `python3 tools/live_highlights.py --write`.
5. Publish only if the summary has `"pass": true`, `errors` is 0 and `feeds_untouched` is true, and the changed files are only `live_highlights.json` and/or `nfl/live_highlights.json`. If nothing changed, report "NO CHANGE" and stop.
6. Commit with the message `Live highlights <YYYY-MM-DD> <h:mm AM/PM> ET (H halftime, F final)`. H and F are this run's new clips.
7. Run `git push origin HEAD:main`. If it is rejected, run `git pull --rebase origin main`, rerun step 4 once, re-check step 5 and push again. Never force-push.
8. Verify that `git rev-parse HEAD` equals `origin/main`, and that both raw files parse with schema `cw_live_highlights_v1`:
   - `https://raw.githubusercontent.com/richdiskin/cover-window-data/main/live_highlights.json?t=<now>`
   - `.../main/nfl/live_highlights.json?t=<now>`

   The CDN can lag up to 5 min.

## Routine (Rich creates it; times are America/New_York)
- **Schedule:** every 20 minutes on game days. That covers Saturday about 11:30 AM to 1:30 AM for college, and Sunday 12:30 PM to Monday 12:30 AM, Monday and Thursday nights, plus any other game nights, for NFL. Halftime clips are only searched between halftime and the 4th quarter, so runs farther apart miss them.
- **Routine prompt:**

> In /workspace/cwfeed run `tools/publish_live_highlights.sh`. If it prints STOP, report the line and do nothing else (no reset, stash or force-push). If it prints PUSHED, report the commit hash, `halftime_found`, `final_found` and any rejections. If it prints NO CHANGE, report that. Never run build_data.py, make_closes.py, build_skeleton.py, kern_v2_scale_check.py, build_source_notes.py or validate_skeleton.py. Never edit kern_model_v2.py. Never touch games.json or nfl/games.json. No YouTube cookies or login. Never print secrets. Don't message anyone.

## When it fails
- **TLS drops (curl 35) to GitHub or YouTube:** the script retries. If it still fails, run it again in 20 minutes.
- **`clip_rule_errors` not empty:** a clip in the file breaks a rule. Nothing is written. Fix the entry by hand and commit only that file.
- **`feeds_untouched` false:** stop. Something else changed games.json or nfl/games.json during the run.
- **Many `unverifiable` rejections:** the YouTube embed page or oEmbed is failing. Wait 30 to 60 minutes.

## Coverage gaps seen Oct 31, 2026
- Allowlisted networks and conferences almost never post "first half" or "halftime" highlight clips. During the game they post single plays instead, for example "X finds Y for the TD", and the halftime rule correctly rejects those. Expect the halftime slot to be mostly empty.
- Final clips come mostly from ESPN College Football, CFB ON FOX, CBS Sports College Football, FOX Sports, ACC Digital Network and Big Ten Football, usually 3 to 4 h after kickoff. Some team channels post condensed games (e.g. BaylorAthletics).
- No network channel exists for TNT, CW, ESPN+, Peacock or MW+ games. Those depend on conference and team channels and the date-sorted search.
- The ESPN College Football RSS feed holds only the latest 15 uploads, about 1 to 1.5 h on a busy Saturday, so the search step fills the gap.
