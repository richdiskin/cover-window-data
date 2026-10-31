#!/usr/bin/env python3
"""Cover Window in-game highlights: a `halftime` and a `final` YouTube clip per game, written to SEPARATE files
(live_highlights.json for college, nfl/live_highlights.json for NFL), keyed by game_id. games.json and nfl/games.json
are only read, never written (forward-only rule of tools/highlights.py stays).

  python3 tools/live_highlights.py                 # dry run, both leagues, prints a JSON summary
  python3 tools/live_highlights.py --write         # write the two live files (only when every check passes)
  python3 tools/live_highlights.py --league college --games 401858505 --write

Slots (Rich, Oct 31, 2026):
  - halftime_eligible is set the first time the script sees a game and is never flipped to true later. It is true only
    if the game had not kicked off yet or was still in the 1st or 2nd quarter (not at halftime) at that moment.
  - halftime: searched only when halftime_eligible is true, the game is at halftime or in the 3rd quarter, and kickoff
    was within the last 8 hours. Once the game reaches the 4th quarter (or is final) without a halftime clip, the
    search stops for good. Final and nearly-final games get only a final clip.
  - final: searched when ESPN says the game is final (or ESPN status is unreadable and kickoff was 3+ hours ago),
    kickoff within the last 36 hours, and the entry has no final clip yet. The clip must be uploaded 2.5+ hours after kickoff.
  - A slot that has a clip is kept as is (no churn).
Clip rules: allowlisted channel (tools/highlights_allowlist.json) only; title names BOTH teams (ESPN-built aliases from
highlights.Matcher); highlight wording for the slot; house text rules (highlights.text_problems); football only
(highlights.sport_problem); public, not live, not age-gated, playableInEmbed, oEmbed 200 and youtube-nocookie
third-party embed status OK; uploaded on/after the game date (ET) and after kickoff.
No cookies, no YouTube login, no API key. At most 3 YouTube requests in flight (shared highlights.YT_SLOTS).
"""
import argparse, concurrent.futures as cf, datetime as dt, hashlib, html as _html, json, os, re, subprocess, sys, threading, time
import urllib.parse
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import highlights as H      # reuse: allowlist, Matcher, text/sport rules, yt-dlp wrapper, embed checks, ESPN getj

REPO = H.REPO
ET = ZoneInfo("America/New_York")
FEED = {"college": "games.json", "nfl": "nfl/games.json"}
OUT = {"college": "live_highlights.json", "nfl": "nfl/live_highlights.json"}
CACHE_DIR = os.path.expanduser(os.environ.get("CW_LIVE_CACHE", "~/.cache/cw_live_highlights"))
os.makedirs(CACHE_DIR, exist_ok=True)
SEARCH_TTL = 15 * 60          # live searches go stale fast
VIDEO_TTL = 24 * 3600
REGISTER_AHEAD = dt.timedelta(hours=24)
HALF_WINDOW = dt.timedelta(hours=8)
FINAL_WINDOW = dt.timedelta(hours=36)
FINAL_MIN_AGE = dt.timedelta(hours=3)
FINAL_UPLOAD_AFTER = dt.timedelta(hours=2, minutes=30)

# ---------------------------------------------------------------- channels per TV network
CH = {c["channel"]: c["channel_id"] for c in H.ALLOW["networks"] + H.ALLOW["conferences"]}
TV_CH = {
    "college": [
        (r"^(ESPN|ESPN2|ESPNU|ESPNEWS|ABC|ESPN\+|ACC Network|ACCN|SEC Network|SECN|SEC Network\+)$", ["ESPN College Football"]),
        (r"^(FOX|Fox|FS1|FS2|Big Ten Network|BTN)$", ["CFB ON FOX", "Big Ten Network", "Big Ten Football"]),
        (r"^(CBS|CBSSN|Paramount\+)$", ["CBS Sports College Football"]),
        (r"^(NBC|Peacock|USA Net|USA)$", ["NBC Sports", "Big Ten Network"]),
        (r"^(MW\+|MWN)$", ["MountainWestConf"]),
    ],
    "nfl": [
        (r"^(ESPN|ESPN2|ABC|ESPN\+)$", ["ESPN"]),
        (r"^(FOX|Fox|FS1)$", ["FOX Sports"]),
        (r"^(CBS|Paramount\+|Nickelodeon)$", ["CBS Sports"]),
        (r"^(NBC|Peacock)$", ["NBC Sports"]),
    ],
}
LEAGUE_FALLBACK = {"college": ["ESPN College Football", "CFB ON FOX", "CBS Sports College Football"],
                   "nfl": ["NFL", "ESPN", "FOX Sports", "CBS Sports", "NBC Sports"]}

# ---------------------------------------------------------------- slot title rules
HALF_RX = re.compile(r"\b(first[- ]half|1st[- ]half|halftime|half[- ]time)\b", re.I)
HALF_BAD = re.compile(r"\b(halftime (show|performance|speech|adjustments?|report|interview|band)|marching|band|second[- ]half|2nd[- ]half|"
                      r"full game|final|extended)\b", re.I)
PART_RX = re.compile(r"\b(first|1st|second|2nd)[- ]half\b|\bhalf[- ]?time\b|\b(1st|2nd|3rd|4th|first|second|third|fourth)[- ]quarter\b|\bQ[1-4]\b", re.I)
HL_WORD = re.compile(r"\bhighlights?\b|\bcondensed\b", re.I)
PLAYERISH = re.compile(r"\b(defensive|offensive|defense|offense|special teams|kicking game|recruit\w*|every (catch|td|touchdown|run|throw|pass|sack|snap)|top plays|best plays|career|season)\b|'s (\d+|three|two|four|five) (td|touchdown)s?|\bmic'?d\b", re.I)

def half_ok(M, a, h, t):
    return (M.mentions(t, a) and M.mentions(t, h) and HL_WORD.search(t) is not None and HALF_RX.search(t) is not None
            and not HALF_BAD.search(t) and not PLAYERISH.search(t) and not POSITION.search(t))

POSITION = re.compile(r"\b(QB|RB|WR|TE|LB|CB|DB|DE|DT|EDGE|OL|OT|ATH|K|P)\s+[A-Z][a-z]")


def final_ok(M, a, h, t):
    return (M.mentions(t, a) and M.mentions(t, h) and HL_WORD.search(t) is not None and not PART_RX.search(t)
            and not PLAYERISH.search(t) and not POSITION.search(t))

# ---------------------------------------------------------------- caches
_lock = threading.Lock()
def _cp(n): return os.path.join(CACHE_DIR, n + ".json")
def _ld(n):
    try: return json.load(open(_cp(n)))
    except Exception: return {}
C = {"search": _ld("search"), "video": _ld("video")}
TTL = {"search": SEARCH_TTL, "video": VIDEO_TTL}
def cget(k, key):
    v = C[k].get(key)
    if v and time.time() - v.get("_ts", 0) < TTL[k]: return v["data"]
def cput(k, key, d):
    with _lock: C[k][key] = {"_ts": time.time(), "data": d}
def save_cache():
    with _lock:
        for k, v in C.items():
            json.dump({a: b for a, b in v.items() if time.time() - b.get("_ts", 0) < TTL[k]}, open(_cp(k), "w"))

# ---------------------------------------------------------------- YouTube (same wrappers and limits as highlights.py)
def _flat(args, cid=None):
    p = H.ytdlp(["--flat-playlist", "-j"] + args)
    out = []
    for l in p.stdout.splitlines():
        if not l.strip(): continue
        try: d = json.loads(l)
        except Exception: continue
        out.append({"id": d.get("id"), "title": d.get("title") or "", "channel_id": cid or d.get("channel_id")})
    return p.returncode, [x for x in out if x["id"]]

def chsearch(cid, q, n=8):
    key = "c|%s|%d|%s" % (cid, n, q)
    r = cget("search", key)
    if r is None:
        rc, r = _flat(["-I", "1:%d" % n, "https://www.youtube.com/channel/%s/search?query=%s" % (cid, urllib.parse.quote_plus(q))], cid)
        if rc == 0: cput("search", key, r)
    return r

RSS_NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015", "media": "http://search.yahoo.com/mrss/"}
def rss(cid):
    """Public channel upload feed (latest 15 uploads, exact publish time, description). No key, no login."""
    import xml.etree.ElementTree as X
    key = "r|" + cid
    r = cget("search", key)
    if r is not None: return r
    for i in range(3):
        with H.YT_SLOTS:
            H.STATS["yt_requests"] += 1
            p = subprocess.run(["curl", "-sS", "--compressed", "--max-time", "20", "-A", H.UA,
                                "https://www.youtube.com/feeds/videos.xml?channel_id=" + cid], capture_output=True)
        try:
            root = X.fromstring(p.stdout)
        except Exception:
            time.sleep(2 * (i + 1)); continue
        out = []
        for e in root.findall("a:entry", RSS_NS):
            g = lambda path: (e.find(path, RSS_NS).text if e.find(path, RSS_NS) is not None else None)
            out.append({"id": g("yt:videoId"), "title": g("a:title") or "", "channel_id": g("yt:channelId") or cid,
                        "published": zs(utc(g("a:published"))), "description": g("media:group/media:description") or "", "via": "rss"})
        cput("search", key, out)
        return out
    raise RuntimeError("rss unreadable " + cid)

def search_date(q, n=12):
    """YouTube search sorted by upload date (sp=CAI%3D), flat: newest first."""
    key = "d|%d|%s" % (n, q)
    r = cget("search", key)
    if r is None:
        rc, r = _flat(["-I", "1:%d" % n, "https://www.youtube.com/results?search_query=%s&sp=CAI%%253D" % urllib.parse.quote_plus(q)])
        if rc == 0: cput("search", key, r)
    return r

GATED = {"n": 0}
def watch_page(vid):
    """Full metadata when YouTube serves the watch page without a bot check. Returns None when gated or unreadable."""
    if GATED["n"] >= 2: return None
    with H.YT_SLOTS:
        H.STATS["yt_requests"] += 1
        p = subprocess.run(["curl", "-sL", "--compressed", "--max-time", "25", "-A", H.UA, "-H", "Accept-Language: en-US,en;q=0.9",
                            "https://www.youtube.com/watch?v=" + vid], capture_output=True)
    h = p.stdout.decode("utf-8", "ignore")
    def g(rx):
        m = re.search(rx, h); return m.group(1) if m else None
    status = g(r'"playabilityStatus":\{"status":"(\w+)"')
    if status == "LOGIN_REQUIRED":
        GATED["n"] += 1; return None
    cid, up = g(r'"channelId":"(UC[\w-]{22})"'), g(r'"uploadDate":"([^"]+)"')
    if not (status == "OK" and cid and up and "T" in up): return None
    live = g(r'"isLiveContent":(true|false)') == "true" or g(r'"isLive":(true|false)') == "true"
    return {"channel_id": cid, "published": zs(utc(up)), "time_source": "watch_page",
            "playable_in_embed": g(r'"playableInEmbed":(true|false)') == "true",
            "public": g(r'"isUnlisted":(true|false)') != "true" and g(r'"isPrivate":(true|false)') != "true",
            "family_safe": g(r'"isFamilySafe":(true|false)') != "false", "live": live,
            "duration": int(g(r'"lengthSeconds":"(\d+)"') or 0) or None, "description": H._desc(h), "keywords": H._kw(h)}

def embed_page(vid, tries=3):
    """youtube-nocookie embed page with a third-party Referer, no cookies (same check as highlights.embed_status),
    plus playableInEmbed and live flags from the same page."""
    for i in range(tries):
        with H.YT_SLOTS:
            H.STATS["yt_requests"] += 1
            p = subprocess.run(["curl", "-sL", "--compressed", "--max-time", "25", "-A", H.UA, "-e", "https://example.com/",
                                "-H", "Accept-Language: en-US,en;q=0.9", "https://www.youtube-nocookie.com/embed/" + vid], capture_output=True)
        h = p.stdout.decode("utf-8", "ignore").replace('\\"', '"')
        if "blocked it from display" in h: return {"status": "UNPLAYABLE"}
        m = re.search(r'"previewPlayabilityStatus":\{"status":"(\w+)"', h)
        if m:
            pe = re.search(r'"playableInEmbed":(true|false)', h)
            return {"status": m.group(1), "playable_in_embed": (pe.group(1) == "true") if pe else None,
                    "live": bool(re.search(r'"isLive(Content)?":true', h))}
        time.sleep(3 * (i + 1))
    return {"status": None}

def thumb_time(vid):
    """Fallback upload time when neither the channel feed nor the watch page gives one: the i.ytimg.com thumbnail
    ETag is the Unix time the thumbnail was made, minutes before publishing (checked Oct 31, 2026 against feed times:
    20:40:11Z vs 20:48:18Z, 19:52:05Z vs 19:54:33Z). Public CDN header, no login."""
    for i in range(3):
        with H.YT_SLOTS:
            H.STATS["yt_requests"] += 1
            p = subprocess.run(["curl", "-sSI", "--max-time", "15", "https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid], capture_output=True)
        m = re.search(r'(?im)^etag:\s*"?(\d{9,11})"?', p.stdout.decode("utf-8", "ignore"))
        if m: return zs(dt.datetime.fromtimestamp(int(m.group(1)), dt.timezone.utc))
        if b" 404" in p.stdout[:40]: return None
        time.sleep(2 * (i + 1))
    return None

FOOTBALL_CH = {CH.get(n) for n in ("ESPN College Football", "CFB ON FOX", "CBS Sports College Football", "Big Ten Football", "NFL")} - {None}

def video(c):
    """Validate one candidate. Returns a dict with error or the verified fields."""
    vid = c["id"]
    v = cget("video", vid)
    if v is None:
        v = {"id": vid, "title": c.get("title") or "", "channel_id": c.get("channel_id"), "description": c.get("description") or "",
             "keywords": [], "duration": c.get("duration")}
        w = watch_page(vid)
        if w: v.update(w)
        if c.get("published"):
            v["published"], v["time_source"] = c["published"], "channel_feed"
        if not v.get("published"):
            t = thumb_time(vid)
            if t: v["published"], v["time_source"] = t, "thumbnail_etag"
        e = embed_page(vid)
        v["embed_status"] = e["status"]
        if v.get("playable_in_embed") is None: v["playable_in_embed"] = e.get("playable_in_embed")
        v["live"] = bool(v.get("live") or e.get("live"))
        if e["status"] is None or not v.get("published"):
            return {"id": vid, "error": "unverifiable"}
        cput("video", vid, v)
    if c.get("published") and v.get("time_source") == "thumbnail_etag":
        v = dict(v, published=c["published"], time_source="channel_feed")
    return v

def utc(s): return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
def zs(t): return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

class Run:
    def __init__(self): self.rej = {}; self.lock = threading.Lock()
    def reject(self, why):
        with self.lock: self.rej[why] = self.rej.get(why, 0) + 1

def pick(run, sources, title_ok, kick, min_upload, maxcheck=4):
    seen = set()
    game_date_et = kick.astimezone(ET).date()
    for src in sources:
        pre = []
        try: cands = src()
        except Exception: run.reject("search_error"); continue
        for c in cands:
            if c["id"] in seen: continue
            t = H.norm(c.get("title", ""))
            if c.get("channel_id") not in H.OFFICIAL: continue          # feeds/searches return many other channels
            if not title_ok(t): continue
            seen.add(c["id"])
            if H.OTHER.search(t): run.reject("other_format"); continue
            if H.text_problems(c.get("title", "")): run.reject("title_rules"); continue
            pre.append(c)
        pre.sort(key=lambda c: c.get("published") or "", reverse=True)
        for c in pre[:maxcheck]:
            v = video(c)
            if v.get("error"): run.reject("unverifiable"); continue
            if v.get("channel_id") not in H.OFFICIAL: run.reject("not_allowlisted_channel"); continue
            up = utc(v["published"])
            if up.astimezone(ET).date() < game_date_et or up < min_upload: run.reject("uploaded_too_early"); continue
            sp = H.sport_problem(v, v["channel_id"] not in FOOTBALL_CH)
            if sp: run.reject("non_football"); continue
            if v.get("embed_status") != "OK": run.reject("not_embeddable_third_party"); continue
            if v.get("playable_in_embed") is False or v.get("public") is False or v.get("family_safe") is False or v.get("live"):
                run.reject("not_embeddable"); continue
            if "oembed" not in v:
                v["oembed"] = H.oembed(v["id"])
                if v["oembed"] in (200, 401, 403, 404): cput("video", v["id"], v)
            if v["oembed"] != 200: run.reject("oembed_not_200"); continue
            return v
    return None

def make_item(v, slot, now):
    return {"youtube_id": v["id"], "title": v["title"], "channel": H.OFFICIAL[v["channel_id"]]["name"], "channel_id": v["channel_id"],
            "url": "https://www.youtube.com/watch?v=" + v["id"], "embed_url": "https://www.youtube-nocookie.com/embed/" + v["id"],
            "thumb": "https://i.ytimg.com/vi/%s/hqdefault.jpg" % v["id"], "duration_s": v.get("duration"),
            "uploaded_utc": v["published"], "upload_time_source": v.get("time_source"), "found_at_utc": zs(now), "slot": slot,
            "embeddable": True, "oembed": 200, "official": True}

# ---------------------------------------------------------------- ESPN status
def scoreboard(lg, dates):
    st = {}
    for d in sorted(dates):
        url = "https://site.api.espn.com/apis/site/v2/sports/football/%s/scoreboard?dates=%s&limit=400" % (H.ESPN_LG[lg], d)
        if lg == "college": url += "&groups=80"
        try: doc = H.getj(url, tries=5)
        except Exception: continue
        for e in doc.get("events", []):
            st[str(e["id"])] = status_of(e["competitions"][0]["status"])
    return st

def status_of(s):
    t = s.get("type", {})
    return {"state": t.get("state"), "name": t.get("name"), "period": s.get("period") or 0, "clock": s.get("displayClock"),
            "completed": bool(t.get("completed"))}

def summary_status(lg, gid):
    d = H.getj("https://site.api.espn.com/apis/site/v2/sports/football/%s/summary?event=%s" % (H.ESPN_LG[lg], gid), tries=6)
    return status_of(d["header"]["competitions"][0]["status"])

def at_half_or_later(s): return s["name"] in ("STATUS_HALFTIME",) or (s["name"] == "STATUS_END_PERIOD" and s["period"] == 2) or s["period"] >= 3
def eligible_first_sight(s):
    """Rich, Oct 31 2026: true only if not kicked off yet, or in the 1st/2nd quarter and not yet at halftime."""
    if s is None: return False
    if s["state"] == "pre": return True
    return s["state"] == "in" and s["period"] in (1, 2) and not at_half_or_later(s)
def fourth_or_later(s): return s["completed"] or s["state"] == "post" or s["period"] >= 4

def teams_of(lg, g, M):
    by_name = {H.norm(t["displayName"]).lower(): tid for tid, t in M.teams.items()}
    names = [((g.get(side) or {}).get("team") if isinstance(g.get(side), dict) else g.get(side)) for side in ("away", "home")]
    ids = [by_name.get(H.norm(n or "").lower()) for n in names]
    if all(ids): return ids[0], ids[1]
    d = H.getj("https://site.api.espn.com/apis/site/v2/sports/football/%s/summary?event=%s" % (H.ESPN_LG[lg], g["game_id"]))
    comps = {c["homeAway"]: c["team"]["id"] for c in d["header"]["competitions"][0]["competitors"]}
    return str(comps["away"]), str(comps["home"])

def channels_for(lg, g, M, a, h):
    chans = []
    for tv in g.get("tv") or []:
        for rx, names in TV_CH[lg]:
            if re.search(rx, tv.strip()): chans += [CH[n] for n in names if n in CH]
    if lg == "college":
        for side in ("away", "home"):
            conf = (g.get(side) or {}).get("conference") if isinstance(g.get(side), dict) else None
            chans += H.CONF_CH.get(conf, [])
    chans += [CH[n] for n in LEAGUE_FALLBACK[lg] if n in CH]
    for tid in (a, h):
        chans += H.TEAM_CH.get(M.teams[tid]["displayName"], [])[:1]
    seen, uniq = set(), []
    for c in chans:
        if c not in seen: seen.add(c); uniq.append(c)
    return uniq

def sources(lg, g, M, a, h, slot):
    """1) upload feeds of the game's TV network, both conferences, league networks and both team channels (exact times);
    2) one YouTube search sorted by upload date; 3) a channel search on the game's main network channel."""
    A, Hm = M.teams[a], M.teams[h]
    sa, sh = H.short(A, lg), H.short(Hm, lg)
    word = "first half highlights" if slot == "halftime" else "highlights"
    chans = channels_for(lg, g, M, a, h)
    def feeds():
        out = []
        for c in chans:
            try: out += rss(c)
            except Exception: pass
        return out
    q = [feeds, lambda: search_date("%s vs %s %s" % (A["displayName"], Hm["displayName"], word))]
    if chans: q.append(lambda: chsearch(chans[0], "%s %s %s" % (sa, sh, word)))
    return q

# ---------------------------------------------------------------- per-league run
def load_out(lg):
    p = os.path.join(REPO, OUT[lg])
    if os.path.exists(p):
        raw = open(p, encoding="utf-8").read(); return raw, json.loads(raw)
    return None, {"schema": "cw_live_highlights_v1", "league": lg, "updated_utc": None,
                  "about": "In-game YouTube highlights per game_id: optional `halftime` (first-half clip) and `final` (full-game clip). "
                           "Official allowlisted channels only, embeddable. Times are UTC for data only; apps show none of them.",
                  "games": {}}

def sha(p):
    return hashlib.sha256(open(os.path.join(REPO, p), "rb").read()).hexdigest()

def run_league(lg, a, now, run, seed):
    feed = json.load(open(os.path.join(REPO, FEED[lg]), encoding="utf-8"))
    raw, doc = load_out(lg)
    before = json.dumps(doc["games"], sort_keys=True)
    want = set(a.games.split(",")) if a.games else None
    M = H.Matcher(lg)
    rep = {"file": OUT[lg], "games_in_window": 0, "registered_new": 0, "halftime_eligible_new": 0, "not_eligible_new": 0,
           "halftime_searched": 0, "halftime_found": 0, "halftime_closed_no_clip": 0, "final_searched": 0, "final_found": 0,
           "halftime_total": 0, "final_total": 0, "errors": {}, "status_skipped": {}, "found": []}
    games = []
    for g in feed["games"]:
        gid = str(g["game_id"]); k = H.kickoff(g)
        if k is None or (want and gid not in want): continue
        if now - FINAL_WINDOW <= k <= now + REGISTER_AHEAD: games.append((g, k))
    rep["games_in_window"] = len(games)
    dates = {k.astimezone(ET).strftime("%Y%m%d") for _, k in games}
    st = scoreboard(lg, dates) if games else {}
    for g, k in games:
        gid = str(g["game_id"])
        if gid not in st and k <= now:
            try: st[gid] = summary_status(lg, gid)
            except Exception as e: rep["status_skipped"][gid] = "%s: %s" % (type(e).__name__, str(e)[:120])   # game skipped this run only
    jobs = []
    for g, k in games:
        gid = str(g["game_id"]); s = st.get(gid)
        e = doc["games"].get(gid)
        if e is None:
            if k > now:
                s0 = {"state": "pre", "name": "STATUS_SCHEDULED", "period": 0, "clock": None, "completed": False}
            else:
                s0 = seed.get(gid) or s
            if s0 is None: continue                      # status unknown: register later
            e = {"game_id": gid, "matchup": g["matchup"], "kickoff_utc": g["kickoff_utc"], "first_seen_utc": zs(now),
                 "first_seen_status": "%s Q%s %s" % (s0["name"], s0["period"], s0.get("clock") or ""), "halftime_eligible": eligible_first_sight(s0)}
            doc["games"][gid] = e
            rep["registered_new"] += 1
            rep["halftime_eligible_new" if e["halftime_eligible"] else "not_eligible_new"] += 1
        do_half = (e["halftime_eligible"] and "halftime" not in e and s is not None and s["state"] == "in"
                   and at_half_or_later(s) and not fourth_or_later(s) and now - k <= HALF_WINDOW)
        if e["halftime_eligible"] and "halftime" not in e and s is not None and fourth_or_later(s):
            rep["halftime_closed_no_clip"] += 1
        final_ready = (s is not None and (s["completed"] or s["state"] == "post")) or (s is None and now - k >= FINAL_MIN_AGE)
        do_final = "final" not in e and final_ready and now - k <= FINAL_WINDOW and (s is None or s["name"] not in ("STATUS_POSTPONED", "STATUS_CANCELED"))
        if do_half: jobs.append((g, k, "halftime"))
        if do_final: jobs.append((g, k, "final"))
    rep["halftime_searched"] = sum(1 for j in jobs if j[2] == "halftime")
    rep["final_searched"] = sum(1 for j in jobs if j[2] == "final")
    def work(j):
        g, k, slot = j
        try:
            ta, th = teams_of(lg, g, M)
            ok = (lambda t: half_ok(M, ta, th, t)) if slot == "halftime" else (lambda t: final_ok(M, ta, th, t))
            mn = k if slot == "halftime" else k + FINAL_UPLOAD_AFTER
            return j, pick(run, sources(lg, g, M, ta, th, slot), ok, k, mn), None
        except Exception as ex:
            return j, None, "%s: %s" % (type(ex).__name__, ex)
    with cf.ThreadPoolExecutor(3) as ex:
        for (g, k, slot), v, err in ex.map(work, jobs):
            gid = str(g["game_id"])
            if err: rep["errors"]["%s/%s" % (gid, slot)] = err; print("ERR", lg, gid, slot, err, file=sys.stderr); continue
            print("%s %s %-8s %s %s" % (lg, gid, slot, g["matchup"], ("-> %s | %s | %s | %s" % (v["id"], H.OFFICIAL[v["channel_id"]]["name"], v["title"], v["published"])) if v else "none"), file=sys.stderr)
            if v and slot not in doc["games"][gid]:
                doc["games"][gid][slot] = make_item(v, slot, dt.datetime.now(dt.timezone.utc))
                rep[slot + "_found"] += 1
                rep["found"].append("%s %s %s | %s | %s" % (gid, slot, v["id"], H.OFFICIAL[v["channel_id"]]["name"], v["title"]))
    # checks on every clip in the file
    errs = []
    for gid, e in doc["games"].items():
        for slot in ("halftime", "final"):
            it = e.get(slot)
            if not it: continue
            if not re.fullmatch(r"[\w-]{11}", it.get("youtube_id", "")): errs.append("%s %s bad id" % (gid, slot))
            if it.get("channel_id") not in H.OFFICIAL: errs.append("%s %s channel not allowlisted" % (gid, slot))
            for f in ("title", "channel"):
                errs += ["%s %s %s: %s" % (gid, slot, f, x) for x in H.text_problems(it.get(f))]
            if slot == "halftime" and not e.get("halftime_eligible"): errs.append("%s halftime on non-eligible game" % gid)
    rep["clip_rule_errors"] = errs
    rep["halftime_total"] = sum(1 for e in doc["games"].values() if e.get("halftime"))
    rep["final_total"] = sum(1 for e in doc["games"].values() if e.get("final"))
    changed = json.dumps(doc["games"], sort_keys=True) != before or raw is None
    if changed: doc["updated_utc"] = zs(dt.datetime.now(dt.timezone.utc))
    rep["changed"] = changed
    return doc, raw, rep

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", choices=["nfl", "college", "both"], default="both")
    ap.add_argument("--games", help="comma-separated game_ids")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--seed-scoreboard", action="append", default=[],
                    help="ESPN scoreboard JSON saved earlier; its status decides halftime_eligible for games first seen now (go-live use)")
    a = ap.parse_args()
    t0 = time.time(); now = dt.datetime.now(dt.timezone.utc); run = Run()
    seed = {}
    for f in a.seed_scoreboard:
        for e in json.load(open(f)).get("events", []):
            seed[str(e["id"])] = status_of(e["competitions"][0]["status"])
    feeds_before = {p: sha(p) for p in FEED.values()}
    leagues = ["college", "nfl"] if a.league == "both" else [a.league]
    out = {"started_et": dt.datetime.now(ET).strftime("%Y-%m-%d %I:%M %p ET"), "leagues": {}}
    failed = False; results = {}
    for lg in leagues:
        doc, raw, rep = run_league(lg, a, now, run, seed)
        results[lg] = (doc, raw); out["leagues"][lg] = rep
        if rep["errors"] or rep["clip_rule_errors"]: failed = True
    save_cache()
    feeds_after = {p: sha(p) for p in FEED.values()}
    out["feeds_untouched"] = feeds_before == feeds_after
    if not out["feeds_untouched"]: failed = True
    for lg in leagues:
        doc, raw = results[lg]; rep = out["leagues"][lg]
        if a.write and not failed and rep["changed"]:
            s = json.dumps(doc, indent=1, ensure_ascii=False) + "\n"; json.loads(s)
            p = os.path.join(REPO, OUT[lg]); tmp = p + ".tmp"
            open(tmp, "w", encoding="utf-8").write(s); os.replace(tmp, p)
            rep["written"] = True
        else:
            rep["written"] = False
    tot = lambda k: sum(r[k] for r in out["leagues"].values())
    out.update({"games_checked": tot("games_in_window"), "halftime_found": tot("halftime_found"), "final_found": tot("final_found"),
                "errors": sum(len(r["errors"]) + len(r["clip_rule_errors"]) for r in out["leagues"].values()) + (0 if out["feeds_untouched"] else 1),
                "pass": not failed, "rejections": run.rej, "yt_requests": H.STATS["yt_requests"], "run_seconds": round(time.time() - t0, 1)})
    print(json.dumps(out, indent=1, ensure_ascii=False))
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
