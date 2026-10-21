# Highlights runbook (videos + key_players)

`tools/highlights.py` adds two keys to every upcoming game in `nfl/games.json` (the NFL app) and `games.json` (the College app). The magazine reads the same two files.

- `key_players`: `[{team, player, position, basis, source}]`. Per team, this is ESPN's season passing-yards leader plus the bigger of the rushing-yards and receiving-yards leaders (from the ESPN game summary for that game_id). Players listed Out, Doubtful, on IR or suspended are skipped. ESPN names only; no player is ever invented.
- `videos` (max 7, in this order: matchup, team_recent (away, home), player):
  `{youtube_id, title, label, channel, channel_id, published, duration_s, thumb, kind, team?, opponent?, game_date?, player?, player_basis?, embeddable, oembed, official, gambling_language}`
  - `matchup`: the newest official highlights of the two teams meeting. Regular season or postseason only, published within 3 years and inside football season (college Aug 1 to Jan 21, NFL Aug 1 to Feb 20), so January basketball and spring clips can't slip in.
  - `team_recent`: highlights of the team's last game (opponent and date from ESPN lastFiveGames), published from the day before that game to 4 days after it.
  - `player`: the player is the subject of the title. Published within 45 days; the newest valid clip wins.
- An empty `videos` list means none was found, and the apps hide the slot.

## Rules every video must pass (applied before writing)
- **Channel:** `channel_id` is in `tools/highlights_allowlist.json`. That file lists league, network, conference and team channels, each with evidence; see its `about` field.
- **Embeddable:** the watch page says `playableInEmbed` true, the video is public, not age-gated and not live, and YouTube oEmbed returns 200. oEmbed is re-checked for every written video at the end of the run.
- **Title and label must not contain:**
  - gambling words (bet, odds, picks, spread, props, sportsbooks, ATS, favorite, underdog, fantasy and similar);
  - the house rule-183 Fanatic banned terms (`tools_fanatic_check.banned_terms`);
  - never-public terms (`week_checks.scan_never_public`);
  - BET/LEAN/PASS wording (`neutral_wording`);
  - W-L record or standings language (e.g. "0-3", undefeated, standings, ranked, poll, streak, bowl eligible);
  - other sports, pressers, recruiting or fan content.
- **Forward only:** a game is touched only if its kickoff is more than 30 minutes away. The clock is re-read at write time, and games already kicked off must stay byte-identical (this is checked).
- **House checks:** `week_checks.check_week` is run on the file before and after the change. The run fails if it adds any error. The live college file has known errors from its older shape; the gate is "no new errors". `check_week` now also scans video titles, labels and key_players as public strings.
- **Rate limits:** at most 3 YouTube requests in flight. Caches live in `~/.cache/cw_highlights` (searches 6 h, video checks 24 h, ESPN team lists 7 days).

## Commands
    cd /workspace/cwfeed            # local clone of richdiskin/cover-window-data
    git fetch origin && git status  # must be clean except tools/ work in progress
    git pull --ff-only origin main  # never reset or force; stop if this fails
    python3 tools/highlights.py                 # dry run, prints the report (exit 1 if any check fails)
    python3 tools/highlights.py --write         # writes both files only when every check passes
    git add nfl/games.json games.json && git commit -m "Highlights refresh ..." && git push origin HEAD:main

- **One league or some games:** use `--league nfl|college` and `--games ID,ID`.
- **Staged week:** `--file STAGE.json --league nfl` enriches a staged week before `roll_week.py`. This is optional: `week_checks` ignores the two optional keys when it compares templates.
- **Report:** `~/.cache/cw_highlights/last_report.json` (coverage per slot, per-game picks, rejection counts, run time).
- **Verify after a push:**
  - `git rev-parse origin/main` matches the commit;
  - `curl -s https://raw.githubusercontent.com/richdiskin/cover-window-data/main/nfl/games.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(sum(len(g.get('videos',[])) for g in d['games']))"`.

## When it fails
- **YouTube "confirm you're not a bot":** the tool reads the watch page first and falls back to yt-dlp, with retries. If many `extract_error` show up, wait 30 to 60 minutes and rerun; the caches make reruns cheap. Never add cookies or log in.
- **ESPN summary errors:** the game is reported under `errors`, the run fails and nothing is written. Rerun later.
- **Push rejected:** someone else pushed. Run `git pull --rebase origin main`, rerun `--write` (fast, cached), then commit and push.
- **New season or new teams:** rebuild the allowlist evidence. The scripts used on Oct 21, 2026 are in `/workspace/hl_build/` (`build_allowlist.py`, `evidence2.py`, `evidence3.py`, `orgs2.py`). A team that cannot be tied to its official site stays out (see `left_out`).

## Timing (Oct 21, 2026, full slate: 14 NFL + 53 college upcoming games)
- **Cold** (empty caches): 704 s, about 12 minutes. That was the first full dry run, 3 workers, at most 3 YouTube requests in flight.
- **Warm** (inside the 6 h search cache): 55 to 59 s and 324 YouTube requests (the publishing runs).
- **Daily 6 AM run:** expect about 10 to 15 minutes, because the search cache will have expired.
- **Sunday 11 PM and Monday 9 AM runs:** they cover fewer upcoming NFL games, so they are faster.

## Routine (Rich creates it; times are America/New_York)
- **Schedule:**
  - daily 06:00;
  - Sunday 23:00 (new team_recent clips after the Sunday games);
  - Monday 09:00 (Sunday-night clips and late player clips).
- **Routine prompt:**

> Refresh The Cover Window highlights in richdiskin/cover-window-data (local clone /workspace/cwfeed).
>
> 1. Run `cd /workspace/cwfeed && git fetch origin && git status --short`. If there are local changes other than untracked scratch files, stop and report; never reset, stash or force anything. Otherwise run `git pull --ff-only origin main`.
> 2. Run `python3 tools/highlights.py --write`. Keep the defaults: 3 workers, at most 3 YouTube requests in flight, and the caches in ~/.cache/cw_highlights. Do not add cookies, log in to YouTube or use an API key.
> 3. Read the JSON summary it prints. Publish only if both leagues show `"pass": true` and `"written": true`, `new_house_check_errors`, `video_rule_errors`, `frozen_changed` and `errors` are all empty, and `git diff --stat` touches only nfl/games.json and/or games.json. If nothing changed, report "no change" and stop.
> 4. Then run `git add nfl/games.json games.json && git commit -m "Highlights refresh <date> <time> ET (NFL x/y games, college x/y games with video)" && git push origin HEAD:main`. If the push is rejected, run `git pull --rebase origin main`, rerun step 2 once, re-check step 3, and push again. Never force-push.
> 5. Verify that `git rev-parse HEAD` equals `git rev-parse origin/main` and that https://raw.githubusercontent.com/richdiskin/cover-window-data/main/nfl/games.json and .../main/games.json parse and contain `videos` (the CDN can lag up to 5 minutes).
> 6. If any check fails, write nothing and push nothing. Report the failing check, and for YouTube bot checks wait 30 to 60 minutes and try once more.
>
> Rules:
> - Never touch a game after kickoff.
> - Never run build_data.py, make_closes.py, build_skeleton.py, kern_v2_scale_check.py, build_source_notes.py or validate_skeleton.py, and never edit kern_model_v2.py.
> - No gambling language and nothing never-public (records, standings, betting units).
> - Never print secrets and don't message anyone.
> - Report coverage per league and per slot, the commit hash, the run time and any failure.
