#!/usr/bin/env python3
"""Cover Window highlight videos: writes `videos` and `key_players` into every upcoming game
of nfl/games.json (NFL app) and games.json (College app); the magazine reads the same files.

  python3 tools/highlights.py                  # dry run, both leagues, prints report
  python3 tools/highlights.py --write          # write files (forward only) after all checks pass
  python3 tools/highlights.py --league nfl --games 401873020,401873022 --write
  python3 tools/highlights.py --file STAGE.json --league nfl --write   # enrich a staged week before roll_week

Sources: ESPN public summary API (season leaders, last five games, injuries), yt-dlp ytsearch and
channel-scoped search (no API key), yt-dlp playable_in_embed + YouTube oEmbed for validation.
Only channels in tools/highlights_allowlist.json are used. Never touches a game at or after kickoff.
At most 3 YouTube requests in flight. Caches under $CW_HL_CACHE (default ~/.cache/cw_highlights).
"""
import argparse, concurrent.futures as cf, datetime as dt, json, os, re, subprocess, sys, threading, time
import unicodedata, urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ALLOW = json.load(open(os.path.join(HERE, "highlights_allowlist.json"), encoding="utf-8"))
CW_TOOLS = os.environ.get("CW_TOOLS", "/workspace/coverwindow/tools")
sys.path.insert(0, CW_TOOLS)
import week_checks as W            # never-public scan + house Fanatic (rule 183) checker
import neutral_wording as NW       # BET/LEAN/PASS label regexes
CACHE_DIR = os.path.expanduser(os.environ.get("CW_HL_CACHE", "~/.cache/cw_highlights"))
os.makedirs(CACHE_DIR, exist_ok=True)
YT_SLOTS = threading.BoundedSemaphore(3)
FILES = {"nfl": "nfl/games.json", "college": "games.json"}
ESPN_LG = {"nfl": "nfl", "college": "college-football"}
KICKOFF_BUFFER = dt.timedelta(minutes=30)
MAX_VIDEOS = 7

# ---------------------------------------------------------------- allowlist
OFFICIAL = {}          # channel_id -> {"name", "kind": league|network|conference|team, "team"?}
for c in ALLOW["networks"]:
    OFFICIAL[c["channel_id"]] = {"name": c["channel"], "kind": c["kind"], "leagues": c.get("leagues", [])}
for c in ALLOW["conferences"]:
    OFFICIAL[c["channel_id"]] = {"name": c["channel"], "kind": "conference", "conference": c["conference"]}
TEAM_CH = {}
for t in ALLOW["teams"]:
    for c in t["channels"]:
        OFFICIAL[c["channel_id"]] = {"name": c["channel"], "kind": "team", "team": t["team"]}
        TEAM_CH.setdefault(t["team"], []).append(c["channel_id"])
LEAGUE_CH = {lg: [c["channel_id"] for c in ALLOW["networks"] if lg in c.get("leagues", []) and c.get("player_search")] for lg in ("nfl", "college")}
CONF_CH = {}
for c in ALLOW["conferences"]:
    CONF_CH.setdefault(c["conference"], []).append(c["channel_id"])
PRIMARY = {cid for cid, v in OFFICIAL.items() if v["kind"] in ("league", "network", "conference")}

# ---------------------------------------------------------------- content rules
GAMBLING = re.compile(r"\b(bet|bets|betting|bettor|odds|picks?|predictions?|predict|spread|parlays?|props?|over/under|o/u|"
                      r"sportsbook|draftkings|fanduel|betmgm|caesars|bet365|best bets?|lock of|moneyline|ats|cover(s|ed|ing)? the|"
                      r"underdogs?|favou?rites?|vegas|gambl\w*|wager\w*|fantasy)\b", re.I)
RECORDS = re.compile(r"\b\d{1,2}-\d{1,2}(-\d)?\b|undefeated|unbeaten|winless|standings|playoff picture|bowl[- ]eligib|"
                     r"\brecords?\b(?! ?-?break)|\bwin streak|\blosing streak|\bstreak\b|\bseed(ed|ing)?\b|\branked\b|\brankings?\b|\bpoll\b", re.I)
OTHER = re.compile(r"\b(basketball|volleyball|soccer|baseball|softball|hockey|lacrosse|wrestling|women'?s|mbb|wbb|madden|"
                   r"ea sports|college football 2\d|simulation|reaction|reacts|throwback|classic|full game replay|asmr|"
                   r"press conference|presser|postgame|pregame|interview|podcast|recruit\w*|commit\w*|signing day|"
                   r"draft|combine|mock|trade|rumou?rs?|contract|camp|practice|media day|mic'?d up|hype|trailer|tickets?|"
                   r"cbb|ncaab|ncaaw|hoops|nba|wnba|mlb|nhl|mls|nwsl|ufl|xfl|high school|hs football|7v7|flag football|"
                   r"recovery|reflects?|surgery|injur\w*|emotional|tribute|memorial|"
                   r"msoc|wsoc|mvb|wvb|band|halftime show|cheer|dance team|cover|edit)\b", re.I)
HL = re.compile(r"highlights?|condensed|every (catch|td|touchdown|run|throw|pass|sack)|top plays|best plays", re.I)
ACTION = re.compile(r"\b(TD|TDs|touchdowns?|catch(es)?|grab|runs?|rush(es)?|throws?|strike|dime|laser|moonshot|scramble|"
                    r"sacks?|best plays|top plays|highlight reel|highlights?|interception|pick-six|score[sd]?|dash|burst|bomb|deep ball)\b", re.I)
QUESTION = ("Can ", "Is ", "Why ", "How ", "What ", "Should ", "Will ", "Does ", "Did ", "Who ")


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return s.replace("\u2019", "'").replace("`", "'")


def text_problems(s):
    """House + highlight rules for any reader-facing video string. Empty list = clean."""
    s = s or ""
    p = []
    if GAMBLING.search(s): p.append("gambling: " + GAMBLING.search(s).group(0))
    if RECORDS.search(s): p.append("record/standings: " + RECORDS.search(s).group(0))
    bad = W.FAN.banned_terms(s)
    if bad: p.append("rule 183: " + ", ".join(bad))
    p += ["never-public " + h for h in W.scan_never_public([s])]
    if NW.LABEL_RX.search(s) or NW.CAPS_RX.search(s): p.append("neutral wording")
    return p


# ---------------------------------------------------------------- cache + http
_lock = threading.Lock()
def _cache_path(name): return os.path.join(CACHE_DIR, name + ".json")
def _load(name):
    try: return json.load(open(_cache_path(name)))
    except Exception: return {}
CACHE = {"search": _load("search"), "video": _load("video"), "espn_teams": _load("espn_teams")}
TTL = {"search": 6 * 3600, "video": 24 * 3600, "espn_teams": 7 * 86400}
def cget(kind, key):
    v = CACHE[kind].get(key)
    if v and time.time() - v.get("_ts", 0) < TTL[kind]: return v["data"]
def cput(kind, key, data):
    with _lock: CACHE[kind][key] = {"_ts": time.time(), "data": data}
def save_cache():
    for k, v in CACHE.items():
        json.dump({a: b for a, b in v.items() if time.time() - b.get("_ts", 0) < TTL[k]}, open(_cache_path(k), "w"))

STATS = {"yt_requests": 0, "espn_requests": 0}
def getj(url, tries=3):
    for i in range(tries):
        try:
            STATS["espn_requests"] += 1
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "CoverWindow/1.0"}), timeout=30) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1: raise
            time.sleep(2 * (i + 1))

def ytdlp(args, timeout=120):
    with YT_SLOTS:
        STATS["yt_requests"] += 1
        return subprocess.run(["yt-dlp", "--no-warnings"] + args, capture_output=True, text=True, timeout=timeout)

def search(q, n=15):
    key = "g|%d|%s" % (n, q)
    r = cget("search", key)
    if r is None:
        p = ytdlp(["--flat-playlist", "-j", "ytsearch%d:%s" % (n, q)])
        r = [{"id": d["id"], "title": d.get("title", ""), "channel_id": d.get("channel_id")} for d in
             (json.loads(l) for l in p.stdout.splitlines() if l.strip())]
        if p.returncode == 0: cput("search", key, r)
    return r

def chsearch(cid, q, n=6):
    key = "c|%s|%d|%s" % (cid, n, q)
    r = cget("search", key)
    if r is None:
        p = ytdlp(["--flat-playlist", "-j", "-I", "1:%d" % n,
                   "https://www.youtube.com/channel/%s/search?query=%s" % (cid, urllib.parse.quote_plus(q))])
        r = [{"id": d["id"], "title": d.get("title", ""), "channel_id": cid} for d in
             (json.loads(l) for l in p.stdout.splitlines() if l.strip())]
        if p.returncode == 0: cput("search", key, r)
    return r

def oembed(vid, tries=3):
    for i in range(tries):
        try:
            with YT_SLOTS:
                STATS["yt_requests"] += 1
                with urllib.request.urlopen("https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v=%s&format=json" % vid, timeout=20) as r:
                    return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            time.sleep(3 * (i + 1))
    return -1

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
def watch_page(vid):
    """Primary validator: one GET of the public watch page (lighter than yt-dlp and less often bot-gated)."""
    import html as _html
    with YT_SLOTS:
        STATS["yt_requests"] += 1
        p = subprocess.run(["curl", "-sL", "--compressed", "--max-time", "25", "-A", UA, "-H", "Accept-Language: en-US,en;q=0.9",
                            "https://www.youtube.com/watch?v=" + vid], capture_output=True)
    h = p.stdout.decode("utf-8", "ignore")
    def g(rx):
        m = re.search(rx, h); return m.group(1) if m else None
    status, cid, up = g(r'"playabilityStatus":\{"status":"(\w+)"'), g(r'"channelId":"(UC[\w-]{22})"'), g(r'"(?:uploadDate|publishDate)":"(\d{4}-\d{2}-\d{2})')
    title = g(r'<meta name="title" content="([^"]*)"')
    if not (status and cid and up and title is not None): return None
    live = g(r'"isLiveContent":(true|false)') == "true" or g(r'"isLive":(true|false)') == "true"
    return {"title": _html.unescape(title), "channel_id": cid, "upload_date": up.replace("-", ""),
            "playable_in_embed": g(r'"playableInEmbed":(true|false)') == "true",
            "availability": "public" if status == "OK" and g(r'"isUnlisted":(true|false)') != "true" and g(r'"isPrivate":(true|false)') != "true" else status.lower(),
            "age_limit": 0 if status == "OK" and g(r'"isFamilySafe":(true|false)') != "false" else 18,
            "live_status": "is_live" if live else "not_live", "duration": int(g(r'"lengthSeconds":"(\d+)"') or 0) or None,
            "channel": OFFICIAL.get(cid, {}).get("name"), "via": "watch_page"}

def video(vid):
    v = cget("video", vid)
    if v is not None: return v
    v = {"id": vid}
    w = None
    for i in range(2):
        w = watch_page(vid)
        if w: break
        time.sleep(3)
    if w:
        v.update(w)
    else:
        for i in range(3):
            p = ytdlp(["-j", "--skip-download", "https://www.youtube.com/watch?v=" + vid], timeout=120)
            if p.returncode == 0:
                d = json.loads(p.stdout.strip().splitlines()[-1])
                v.update({k: d.get(k) for k in ("title", "channel", "channel_id", "upload_date", "playable_in_embed",
                                                "availability", "age_limit", "live_status", "duration")})
                v["via"] = "yt-dlp"; v.pop("error", None)
                break
            v["error"] = (p.stderr.strip().splitlines() or ["error"])[-1][:160]
            time.sleep(5 * (i + 1))
    if not v.get("error"): cput("video", vid, v)
    return v

def embed_ok(v):
    if not (v.get("playable_in_embed") is True and v.get("availability") == "public" and v.get("channel_id") in OFFICIAL):
        return False
    if "oembed" not in v:
        v["oembed"] = oembed(v["id"])
        if v["oembed"] in (200, 401, 403, 404): cput("video", v["id"], v)
    return (v.get("oembed") == 200 and v.get("playable_in_embed") is True and v.get("availability") == "public"
            and not v.get("age_limit") and v.get("live_status") == "not_live" and v.get("channel_id") in OFFICIAL)

def pub(v): return dt.datetime.strptime(v["upload_date"], "%Y%m%d").date()

# ---------------------------------------------------------------- team-name matching (built from ESPN)
def espn_teams(lg):
    r = cget("espn_teams", lg)
    if r is None:
        d = getj("https://site.api.espn.com/apis/site/v2/sports/football/%s/teams?limit=1000" % ESPN_LG[lg])
        r = [t["team"] for t in d["sports"][0]["leagues"][0]["teams"]]
        r = [{k: t.get(k) for k in ("id", "displayName", "shortDisplayName", "name", "nickname", "location", "abbreviation")} for t in r]
        cput("espn_teams", lg, r)
    return r

AMBIG_ABBR = {"OSU", "MSU", "USM", "UM", "MIA", "TEX", "WSU", "USF", "UNC", "ASU", "CSU", "SJSU", "NIU", "KSU", "ISU", "BSU", "UNM", "MTSU", "ODU"}
class Matcher:
    """aliases from ESPN displayName / location / shortDisplayName / nickname / mascot / abbreviation.
    Ambiguity is resolved against every team in the league: an alias that is the start of another team's
    alias gets a negative lookahead ('Texas' not followed by 'A&M', 'Tech', 'State' ...); an alias that is
    the end of another team's alias gets a negative lookbehind ('Michigan' not after 'Central ', 'Western ' ...).
    Mascots shared by two or more teams in the league, generic locations ('New York', 'Los Angeles') and
    ambiguous abbreviations are not used on their own."""
    def __init__(self, lg):
        self.lg = lg
        self.teams = {t["id"]: t for t in espn_teams(lg)}
        raw = {}
        for tid, t in self.teams.items():
            raw[tid] = self._raw_aliases(t)
        loc_count, mascot_count = {}, {}
        for t in self.teams.values():
            loc_count[norm(t.get("location") or "")] = loc_count.get(norm(t.get("location") or ""), 0) + 1
            mascot_count[norm(t.get("name") or "")] = mascot_count.get(norm(t.get("name") or ""), 0) + 1
        self.alias = {}
        for tid, t in self.teams.items():
            al = set()
            for a, kind in raw[tid]:
                if kind == "location" and loc_count.get(a, 0) > 1: continue
                if kind == "mascot" and mascot_count.get(a, 0) > 1: continue
                al.add((a, kind))
            self.alias[tid] = al
        self.all_alias = [(a, tid) for tid, al in self.alias.items() for a, _ in al]
        self.rx = {tid: self._compile(tid) for tid in self.teams}

    def _raw_aliases(self, t):
        out = set()
        dn = norm(t.get("displayName")); out.add((dn, "display"))
        loc = norm(t.get("location"))
        if loc: out.add((loc, "location")); out.add((loc.replace("'", ""), "location"))
        for k in ("shortDisplayName", "nickname"):
            v = norm(t.get(k))
            if v and len(v) >= 3 and v != norm(t.get("name")): out.add((v, "short"))
        m = norm(t.get("name"))
        if m: out.add((loc + " " + m, "display")); out.add((m, "mascot"))
        # ESPN abbreviations (TEN, MIN, CAR, DEN ...) collide with ordinary upper-case words in titles, so they
        # are not used; acronyms that are really used as names (LSU, TCU, BYU, UCF, SMU, USC) come in via shortDisplayName.
        if self.lg == "college" and loc.startswith("Miami (OH)"):
            out |= {("Miami OH", "location"), ("Miami (Ohio)", "location"), ("Miami Ohio", "location")}
        if loc == "Hawai'i": out.add(("Hawaii", "location"))
        if loc == "San Jose State": out.add(("San Jose St", "location"))
        if loc == "Ole Miss": out.add(("Mississippi Rebels", "display"))
        return {(a, k) for a, k in out if a}

    def _compile(self, tid):
        pats = []
        for a, kind in self.alias[tid]:
            after, before = set(), set()
            for b, other in self.all_alias:
                if other == tid: continue
                if b.lower().startswith(a.lower() + " ") or b.lower().startswith(a.lower() + "-"):
                    after.add(b[len(a):].strip(" -").split(" ")[0])
                if b.lower().endswith(" " + a.lower()):
                    before.add(b[: -len(a)].strip().split(" ")[-1])
            if norm(a).startswith("Miami") and self.lg == "college" and "(OH)" not in a and "OH" not in a.split():
                after |= {"(OH)", "OH", "(Ohio)", "Ohio", "RedHawks"}
            if a == "Texas" or a.startswith("Texas"):
                after |= {"A&M", "Tech", "State", "Southern", "San Antonio", "El Paso"}
            la = "".join("(?<!%s )" % re.escape(w) for w in sorted(before) if w)
            na = ("(?!\\s+(?:%s)(?!\\w))" % "|".join(re.escape(w) for w in sorted(after))) if after else ""
            flags = "" if kind == "abbr" else "(?i:%s)"
            core = re.escape(a) if kind == "abbr" else flags % re.escape(a)
            pats.append(la + r"(?<![\w&(])" + core + r"(?![\w&])" + na)
        return re.compile("|".join(pats)) if pats else re.compile(r"(?!x)x")

    def mentions(self, title, tid):
        return bool(self.rx[tid].search(norm(title)))

# ---------------------------------------------------------------- per-game work
def in_season(d, lg):
    """Football is played Aug-late Jan (college) / Aug-mid Feb (NFL); a 'matchup' clip published in spring is another sport."""
    if lg == "nfl":
        return d.month in (8, 9, 10, 11, 12, 1) or (d.month == 2 and d.day <= 20)
    # college: bowls and the CFP title game end by about Jan 20 (Jan 19 in 2026); later January clips are basketball
    return d.month in (8, 9, 10, 11, 12) or (d.month == 1 and d.day <= 21)

def kickoff(g):
    k = g.get("kickoff_utc")
    return dt.datetime.fromisoformat(k.replace("Z", "+00:00")) if k else None

def is_upcoming(g, now):
    k = kickoff(g)
    return k is not None and k > now + KICKOFF_BUFFER

class Run:
    def __init__(self, today):
        self.today = today
        self.rej = {}
        self.slots = []
        self.lock = threading.Lock()
    def reject(self, why):
        with self.lock: self.rej[why] = self.rej.get(why, 0) + 1

def pick(run, sources, title_ok, date_ok, maxcheck=8, want=1, newest=False):
    """sources: callables returning candidate lists, tried in order until one yields a valid video.
    Candidates are prefiltered (allowlisted channel, title rules, title match) before any validation request;
    validation stops at the first valid video unless newest=True (matchup: compare dates of all valid ones)."""
    seen = set()
    for src in sources:
        pre = []
        for c in src():
            if c["id"] in seen: continue
            seen.add(c["id"]); t = norm(c.get("title", ""))
            if c.get("channel_id") and c["channel_id"] not in OFFICIAL: run.reject("not_allowlisted_channel"); continue
            if OTHER.search(t): run.reject("other_sport_or_format"); continue
            tp = text_problems(c.get("title", ""))
            if tp: run.reject("title_rules: " + tp[0].split(":")[0]); continue
            if not title_ok(t): run.reject("title_mismatch"); continue
            pre.append(c)
        out = []
        for c in pre[:maxcheck]:
            v = video(c["id"])
            if v.get("error"): run.reject("extract_error"); continue
            if v.get("channel_id") not in OFFICIAL: run.reject("not_allowlisted_channel"); continue
            if not v.get("upload_date") or not date_ok(pub(v)): run.reject("date_window"); continue
            if not embed_ok(v): run.reject("not_embeddable"); continue
            if text_problems(v.get("title")) or not title_ok(norm(v.get("title"))): run.reject("title_rules"); continue
            out.append(v)
            if not newest and len(out) >= want: break
        if out:
            out.sort(key=lambda v: v["upload_date"], reverse=True)
            top = pub(out[0])
            near = sorted([v for v in out if (top - pub(v)).days <= 3], key=lambda v: v["channel_id"] not in PRIMARY)
            out = near + [v for v in out if v not in near]
            return out[:want]
    return []

def item(v, kind, label, **kw):
    d = {"youtube_id": v["id"], "title": v["title"], "label": label, "channel": OFFICIAL[v["channel_id"]]["name"],
         "channel_id": v["channel_id"], "published": pub(v).isoformat(), "duration_s": v.get("duration"),
         "thumb": "https://i.ytimg.com/vi/%s/hqdefault.jpg" % v["id"], "kind": kind}
    d.update(kw)
    d.update({"embeddable": True, "oembed": 200, "official": True, "gambling_language": False})
    return d

def short(team_obj, lg):
    return team_obj.get("name") if lg == "nfl" else team_obj.get("location")

def game_videos(run, lg, g, M):
    s = getj("https://site.api.espn.com/apis/site/v2/sports/football/%s/summary?event=%s" % (ESPN_LG[lg], g["game_id"]))
    comps = {c["homeAway"]: c["team"] for c in s["header"]["competitions"][0]["competitors"]}
    away, home = comps["away"], comps["home"]
    for t in (away, home):
        if t["id"] not in M.teams: raise RuntimeError("team %s not in ESPN %s list" % (t.get("displayName"), lg))
    A, H = M.teams[away["id"]], M.teams[home["id"]]
    leaders = {t["team"]["id"]: t for t in s.get("leaders", [])}
    l5 = {t["team"]["id"]: t.get("events", []) for t in s.get("lastFiveGames", [])}
    out_players = set()
    for t in s.get("injuries", []):
        for i in t.get("injuries", []):
            if (i.get("status") or "").lower() in ("out", "injured reserve", "doubtful", "suspension"):
                out_players.add(i.get("athlete", {}).get("displayName"))
    for side in ("away", "home"):   # NFL feed carries the official injury report too
        inj = g.get("injuries") if isinstance(g.get("injuries"), dict) else {}
        for i in inj.get(side) or []:
            if (i.get("game_status") or "").lower() in ("out", "doubtful"): out_players.add(i.get("player"))
    videos, slots, players = [], [], []
    an, hn = A["displayName"], H["displayName"]
    conf = [x.get("conference") for x in (g.get("away") or {}, g.get("home") or {}) if isinstance(x, dict)]
    # 1) matchup: most recent official highlights of the two teams meeting (regular or postseason, <= 3 years)
    q = [lambda: search("%s vs %s highlights" % (an, hn)) + search("%s vs %s football highlights" % (short(H, lg), short(A, lg)))]
    for cid in (TEAM_CH.get(an, [])[:1] + TEAM_CH.get(hn, [])[:1]):
        q.append(lambda cid=cid: chsearch(cid, "%s %s highlights" % (short(A, lg), short(H, lg))))
    m = pick(run, q, newest=True, title_ok=lambda t: M.mentions(t, A["id"]) and M.mentions(t, H["id"]) and HL.search(t)
             and not re.search(r"preseason|spring game|scrimmage", t, re.I),
             date_ok=lambda d: (run.today - d).days <= 3 * 365 + 30 and in_season(d, lg))
    slots.append(("matchup", bool(m)))
    if m: videos.append(item(m[0], "matchup", "Last meeting: %s vs. %s" % (short(A, lg), short(H, lg))))
    # 2) each team's most recent completed game
    for T in (A, H):
        ev = sorted([e for e in l5.get(T["id"], []) if e.get("gameDate")], key=lambda e: e["gameDate"])
        if not ev:
            slots.append(("team_recent", False)); continue
        last = ev[-1]; opp_id = str(last.get("opponent", {}).get("id"))
        gd = dt.date.fromisoformat(last["gameDate"][:10])
        O = M.teams.get(opp_id) or {"displayName": last.get("opponent", {}).get("displayName", ""), "location": last.get("opponent", {}).get("displayName", ""), "name": ""}
        def opp_ok(t, O=O, opp_id=opp_id):
            if opp_id in M.teams: return M.mentions(t, opp_id)
            return norm(O.get("location", "")).lower() in t.lower()
        q = [lambda T=T, O=O: search("%s vs %s highlights" % (T["displayName"], O["displayName"]))]
        for cid in TEAM_CH.get(T["displayName"], [])[:1]:
            q.append(lambda cid=cid, O=O: chsearch(cid, "%s highlights" % (short(O, lg) or O["displayName"])))
        if lg == "nfl":
            q.append(lambda T=T, O=O: chsearch("UCDVYQ4Zhbm3S2dlz7P1GBDg", "%s vs %s" % (short(T, lg), short(O, lg) or O["displayName"])))
        # ESPN gameDate is UTC; YouTube upload dates are Pacific, so a night game can be uploaded the "day before"
        r = pick(run, q, lambda t, T=T: M.mentions(t, T["id"]) and opp_ok(t) and HL.search(t),
                 lambda d, gd=gd: gd - dt.timedelta(days=1) <= d <= gd + dt.timedelta(days=4))
        slots.append(("team_recent", bool(r)))
        if r: videos.append(item(r[0], "team_recent", "%s's last game: vs. %s" % (short(T, lg), short(O, lg) or O["displayName"]),
                                 team=T["displayName"], opponent=O["displayName"], game_date=gd.isoformat()))
    # 3) best players: per team, the ESPN season passing-yards leader plus the bigger of the rushing/receiving
    #    yards leaders; anyone Out, Doubtful, on IR or suspended is skipped. Never invented: ESPN names only.
    for T in (A, H):
        tl = leaders.get(T["id"])
        if not tl: continue
        cat = {c["name"]: c["leaders"][0] for c in tl.get("leaders", []) if c.get("leaders")}
        def yds(l):
            mm = re.search(r"([\d,]+) YDS", (l or {}).get("displayValue", ""))
            return int(mm.group(1).replace(",", "")) if mm else -1
        picks = [("passingYards", cat.get("passingYards"))]
        picks.append(max([("rushingYards", cat.get("rushingYards")), ("receivingYards", cat.get("receivingYards"))], key=lambda x: yds(x[1])))
        for stat, l in picks:
            if not l: continue
            nm = l["athlete"]["displayName"]
            if nm in out_players or any(p["player"] == nm for p in players): continue
            players.append({"team": T["displayName"], "player": nm, "position": (l["athlete"].get("position") or {}).get("abbreviation"),
                            "basis": "ESPN season leader, " + {"passingYards": "passing yards", "rushingYards": "rushing yards", "receivingYards": "receiving yards"}[stat],
                            "source": "ESPN game summary, event %s" % g["game_id"]})
    for p in players:
        nm = norm(p["player"]); last = re.sub(r"\s+(Jr\.?|Sr\.?|II|III|IV|V)$", "", nm).split()[-1]; first = nm.split()[0]
        T = A if p["team"] == an else H
        def subj(t, nm=nm, last=last, first=first, T=T):
            mm = re.search(re.escape(first) + r".{0,3}" + re.escape(last), t, re.I) or re.search(r"\b" + re.escape(last) + r"\b", t)
            if not mm or mm.start() > 45: return False
            if not re.search(re.escape(first), t, re.I) and not M.mentions(t, T["id"]): return False
            if re.search(r"\b(of|on|by|vs\.?|against|sacks?|intercepts?|picks off|stops|stuffs|hits|forces)\s*$", t[:mm.start()], re.I): return False
            if t.strip().startswith(QUESTION): return False
            return bool(ACTION.search(t))
        q = [lambda p=p, T=T: search("%s highlights %s" % (p["player"], T["displayName"]))]
        chans = LEAGUE_CH[lg] + TEAM_CH.get(T["displayName"], [])[:2]
        if lg == "college":
            c = (g.get("away") if T is A else g.get("home")) or {}
            chans = CONF_CH.get(c.get("conference"), []) + TEAM_CH.get(T["displayName"], [])[:2] + LEAGUE_CH[lg]
        for cid in chans:
            q.append(lambda cid=cid, p=p: chsearch(cid, p["player"]))
        r = pick(run, q, subj, lambda d: (run.today - d).days <= 45, maxcheck=5, newest=True)
        slots.append(("player", bool(r)))
        if r: videos.append(item(r[0], "player", "%s highlights" % p["player"], player=p["player"], team=p["team"], player_basis=p["basis"]))
    # dedupe by youtube_id (a matchup clip can also be a team's last game), cap
    seen, vids = set(), []
    for v in videos:
        if v["youtube_id"] in seen: continue
        seen.add(v["youtube_id"]); vids.append(v)
    return players, vids[:MAX_VIDEOS], slots

# ---------------------------------------------------------------- checks on the output
VIDEO_KEYS = {"youtube_id", "title", "label", "channel", "channel_id", "published", "duration_s", "thumb", "kind",
              "embeddable", "oembed", "official", "gambling_language"}
def check_videos(doc, frozen, final_oembed=True):
    errs = []
    for g in doc["games"]:
        gid = str(g["game_id"])
        if gid in frozen: continue
        if "videos" not in g: continue
        if not isinstance(g["videos"], list) or not isinstance(g.get("key_players"), list):
            errs.append("%s: videos/key_players not lists" % gid); continue
        for p in g["key_players"]:
            errs += ["%s: key_player %s" % (gid, x) for x in text_problems(p.get("player", "") + " " + p.get("basis", ""))]
        for v in g["videos"]:
            if not VIDEO_KEYS <= set(v): errs.append("%s: video keys missing %s" % (gid, sorted(VIDEO_KEYS - set(v))))
            if not re.fullmatch(r"[\w-]{11}", v.get("youtube_id", "")): errs.append("%s: bad youtube_id %r" % (gid, v.get("youtube_id")))
            if v.get("channel_id") not in OFFICIAL: errs.append("%s: %s channel not allowlisted" % (gid, v.get("youtube_id")))
            if v.get("kind") not in ("matchup", "team_recent", "player"): errs.append("%s: bad kind" % gid)
            for f in ("title", "label", "channel"):
                errs += ["%s: %s %s %s" % (gid, v.get("youtube_id"), f, x) for x in text_problems(v.get(f))]
            if final_oembed:
                st = oembed(v["youtube_id"])
                if st != 200: errs.append("%s: %s oEmbed %s" % (gid, v["youtube_id"], st))
    return errs

def dump(doc, raw):
    s = json.dumps(doc, indent=1, ensure_ascii=False)
    return s + ("\n" if raw.endswith("\n") else "")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", choices=["nfl", "college", "both"], default="both")
    ap.add_argument("--file", help="enrich this file instead of the live one (single league)")
    ap.add_argument("--games", help="comma-separated game_ids (default: every upcoming game)")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--report", default=os.path.join(CACHE_DIR, "last_report.json"))
    a = ap.parse_args()
    t0 = time.time()
    now = dt.datetime.now(dt.timezone.utc)
    run = Run(dt.datetime.now().date())
    leagues = ["nfl", "college"] if a.league == "both" else [a.league]
    report = {"started": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "leagues": {}}
    failed = False
    for lg in leagues:
        path = a.file or os.path.join(REPO, FILES[lg])
        raw = open(path, encoding="utf-8").read(); doc = json.loads(raw)
        before = json.loads(raw)
        M = Matcher(lg)
        want = set(a.games.split(",")) if a.games else None
        todo = [g for g in doc["games"] if is_upcoming(g, now) and (want is None or str(g["game_id"]) in want)]
        frozen = {str(g["game_id"]) for g in doc["games"] if not is_upcoming(g, now)}
        results, errors = {}, {}
        def work(g):
            t = time.time()
            try:
                kp, vids, slots = game_videos(run, lg, g, M)
                return str(g["game_id"]), (kp, vids, slots, round(time.time() - t, 1)), None
            except Exception as e:
                return str(g["game_id"]), None, "%s: %s" % (type(e).__name__, e)
        with cf.ThreadPoolExecutor(max(1, min(a.workers, 3))) as ex:
            for gid, res, err in ex.map(work, todo):
                if err: errors[gid] = err; print("ERR", lg, gid, err, file=sys.stderr)
                else:
                    results[gid] = res
                    print("%s %s %s %ss %s" % (lg, gid, next(g["matchup"] for g in todo if str(g["game_id"]) == gid), res[3],
                          "".join("+" if s[1] else "." for s in res[2])), file=sys.stderr)
        save_cache()
        # write-time forward-only guard: re-read the clock, never touch a game at/after kickoff
        now2 = dt.datetime.now(dt.timezone.utc)
        for g in doc["games"]:
            gid = str(g["game_id"])
            if gid in results and is_upcoming(g, now2):
                kp, vids, _, _ = results[gid]
                g["key_players"] = kp; g["videos"] = vids
        frozen2 = {str(g["game_id"]) for g in doc["games"] if not is_upcoming(g, now2)}
        # checks: house week checks must not get worse; own video rules must be clean; frozen games byte-identical
        lgk = "nfl" if lg == "nfl" else "college"
        e_before = set(W.check_week(lgk, before, frozen_ids=frozen2, now_utc=now2))
        e_after = set(W.check_week(lgk, doc, frozen_ids=frozen2, now_utc=now2))
        new_house = sorted(e_after - e_before)
        own = check_videos(doc, frozen2)
        fz = [gid for gid in frozen2 if json.dumps(next(g for g in doc["games"] if str(g["game_id"]) == gid), sort_keys=True)
              != json.dumps(next(g for g in before["games"] if str(g["game_id"]) == gid), sort_keys=True)]
        allslots = [s for r in results.values() for s in r[2]]
        def rate(kind): 
            s = [x for x in allslots if x[0] == kind]; return "%d/%d" % (sum(1 for x in s if x[1]), len(s))
        games_with = sum(1 for r in results.values() if r[1])
        rep = {"file": os.path.relpath(path, REPO) if path.startswith(REPO) else path, "upcoming_games": len(todo), "frozen_games": len(frozen2),
               "games_with_video": "%d/%d" % (games_with, len(results)), "slots": {k: rate(k) for k in ("matchup", "team_recent", "player")},
               "videos_total": sum(len(r[1]) for r in results.values()), "errors": errors,
               "house_checks_before": len(e_before), "house_checks_after": len(e_after), "new_house_check_errors": new_house,
               "video_rule_errors": own, "frozen_changed": fz,
               "per_game": {gid: {"matchup": next(g["matchup"] for g in doc["games"] if str(g["game_id"]) == gid), "videos": len(r[1]),
                                  "slots": ["%s%s" % (k, "" if ok else " (none)") for k, ok in r[2]], "seconds": r[3],
                                  "picked": ["%s %s %s | %s | %s" % (v["kind"], v["youtube_id"], v["published"], v["channel"], v["title"]) for v in r[1]],
                                  "key_players": ["%s (%s, %s)" % (p["player"], p["team"], p["basis"]) for p in r[0]]} for gid, r in results.items()}}
        report["leagues"][lg] = rep
        ok = not errors and not new_house and not own and not fz
        rep["pass"] = ok
        if not ok: failed = True
        if a.write and ok:
            out = dump(doc, raw)
            json.loads(out)
            tmp = path + ".tmp"; open(tmp, "w", encoding="utf-8").write(out); os.replace(tmp, path)
            rep["written"] = True
        elif a.write:
            rep["written"] = False
    report["rejections"] = run.rej
    report["yt_requests"] = STATS["yt_requests"]
    report["run_seconds"] = round(time.time() - t0, 1)
    json.dump(report, open(a.report, "w"), indent=1)
    summ = {lg: {k: v for k, v in r.items() if k != "per_game"} for lg, r in report["leagues"].items()}
    print(json.dumps({"leagues": summ, "rejections": run.rej, "yt_requests": STATS["yt_requests"], "run_seconds": report["run_seconds"]}, indent=1))
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
