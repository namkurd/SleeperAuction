#!/usr/bin/env python3
"""Fetch DTF Club auction drafts from Sleeper and rebuild every data file the site needs.

Standard library only (no pip install needed, so the GitHub Action is fast).

    python scripts/update_data.py             # normal run: fetch anything new, rebuild
    python scripts/update_data.py --refresh   # re-download every Sleeper season
    python scripts/update_data.py --offline   # no network: rebuild from files already in data/

Inputs   data/history_2012_2020.csv   frozen spreadsheet era (cleaned once, see build_history.py)
         data/sleeper_picks.csv       cache of every completed Sleeper auction (grows by itself)
         config/*.json                managers, aliases, eras
Outputs  data/sleeper_picks.csv       updated cache
         data/picks.csv               one tidy table, every pick 2012 -> now, with depth
         data/reconcile_report.md     spreadsheet vs Sleeper differences for overlap seasons
         site/data.json               what the dashboard loads

Every season from 2021 on comes from Sleeper (real player ids, cleaner names).
Seasons 2012-2020 come from the spreadsheet.
"""
import csv
import datetime as dt
import html
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.sleeper.app/v1"
SHEET_LAST_SEASON = 2020

PICK_COLS = ["season", "draft_id", "pick_no", "roster_id", "picked_by", "manager", "player_id",
             "first_name", "last_name", "position", "nfl_team", "amount"]
CANON_COLS = ["season", "manager", "player", "player_id", "player_key", "position", "price",
              "pick_no", "depth", "slot", "row", "team", "college", "source"]

# team abbreviation -> nickname, so defenses look identical in every era
DEF_NICK = {
    "ARI": "Cardinals", "ATL": "Falcons", "BAL": "Ravens", "BUF": "Bills", "CAR": "Panthers",
    "CHI": "Bears", "CIN": "Bengals", "CLE": "Browns", "DAL": "Cowboys", "DEN": "Broncos",
    "DET": "Lions", "GB": "Packers", "HOU": "Texans", "IND": "Colts", "JAX": "Jaguars",
    "JAC": "Jaguars", "KC": "Chiefs", "LV": "Raiders", "OAK": "Raiders", "LAC": "Chargers",
    "SD": "Chargers", "LAR": "Rams", "STL": "Rams", "MIA": "Dolphins", "MIN": "Vikings",
    "NE": "Patriots", "NO": "Saints", "NYG": "Giants", "NYJ": "Jets", "PHI": "Eagles",
    "PIT": "Steelers", "SF": "49ers", "SEA": "Seahawks", "TB": "Buccaneers", "TEN": "Titans",
    "WAS": "Commanders", "WSH": "Commanders",
}


# NFL team code (current and old spellings, from Sleeper and nflverse) -> franchise name
TEAM_FULL = {
    "ARI": "Arizona Cardinals", "ARZ": "Arizona Cardinals", "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens", "BLT": "Baltimore Ravens", "BUF": "Buffalo Bills", "CAR": "Carolina Panthers",
    "CHI": "Chicago Bears", "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "CLV": "Cleveland Browns",
    "DAL": "Dallas Cowboys", "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "HST": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "JAC": "Jacksonville Jaguars", "KC": "Kansas City Chiefs", "LV": "Las Vegas Raiders", "LVR": "Las Vegas Raiders",
    "OAK": "Las Vegas Raiders", "LAC": "Los Angeles Chargers", "SD": "Los Angeles Chargers",
    "LAR": "Los Angeles Rams", "LA": "Los Angeles Rams", "SL": "Los Angeles Rams", "STL": "Los Angeles Rams",
    "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings", "NE": "New England Patriots", "NO": "New Orleans Saints",
    "NYG": "New York Giants", "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SF": "San Francisco 49ers", "SEA": "Seattle Seahawks", "TB": "Tampa Bay Buccaneers", "TEN": "Tennessee Titans",
    "WAS": "Washington Commanders", "WSH": "Washington Commanders",
}
NICK_FULL = {v.split()[-1]: v for v in TEAM_FULL.values()}          # "Ravens" -> "Baltimore Ravens"

# Sleeper spells some colleges two ways; show one name for each
COLLEGE_CANON = {
    "Brigham Young": "BYU", "Louisiana State": "LSU", "Miami": "Miami (FL)", "North Carolina State": "NC State",
    "Mississippi": "Ole Miss", "Southern California": "USC", "Central Florida": "UCF",
    "Alabama-Birmingham": "UAB", "Louisiana-Lafayette": "Louisiana", "Massachusetts": "UMass",
    "Monmouth, N.J.": "Monmouth", "Wayne State, Mich.": "Wayne State",
}


def clean_college(name):
    name = html.unescape(str(name or "")).strip()
    return COLLEGE_CANON.get(name, name)


class SchemaError(RuntimeError):
    """Sleeper returned something we do not recognise."""


# ----------------------------------------------------------------------------- http
def get(path, retries=4, timeout=40):
    url = API + path
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SleeperAuction-dashboard/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed after {retries} tries: {last}")


def need(obj, keys, where):
    """Fail loudly, with a readable report, if Sleeper's response shape changed."""
    if not isinstance(obj, dict):
        raise SchemaError(f"{where}: expected an object, got {type(obj).__name__}")
    missing = [k for k in keys if k not in obj]
    if missing:
        raise SchemaError(
            f"{where}: Sleeper response is missing field(s) {missing}.\n"
            f"  fields present: {sorted(obj.keys())}\n"
            "  Sleeper probably changed its API. Fix scripts/update_data.py (function named in the traceback).")


# ----------------------------------------------------------------------------- io helpers
def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, cols):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def load_json(name):
    return json.loads((ROOT / "config" / name).read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------- sleeper
def discover_leagues(cfg):
    """season -> league object, from the newest league back to the first one on Sleeper."""
    seed = get(f"/league/{cfg['seed_league_id']}")
    need(seed, ["league_id", "season", "previous_league_id", "status"], "league")
    chain = {int(seed["season"]): seed}
    cur = seed
    while cur.get("previous_league_id"):
        cur = get(f"/league/{cur['previous_league_id']}")
        need(cur, ["league_id", "season", "previous_league_id"], "league")
        chain[int(cur["season"])] = cur
    # a new season's league is a league of ours whose previous_league_id we already know
    this_year = dt.date.today().year
    for season in range(max(chain) + 1, this_year + 2):
        for lg in get(f"/user/{cfg['root_user_id']}/leagues/nfl/{season}") or []:
            if lg.get("previous_league_id") in {str(l["league_id"]) for l in chain.values()}:
                chain[int(lg["season"])] = lg
                print(f"  found new league for {lg['season']}: {lg['league_id']}")
                break
    return dict(sorted(chain.items()))


def roster_managers(cfg, season, league_id, picks):
    """roster_id -> manager name for one season."""
    rosters = get(f"/league/{league_id}/rosters")
    users = {u["user_id"]: u for u in get(f"/league/{league_id}/users")}
    overrides = cfg.get("roster_overrides", {}).get(str(season), {})
    by_picker = defaultdict(Counter)
    for p in picks:
        if p.get("picked_by"):
            by_picker[str(p["roster_id"])][p["picked_by"]] += 1
    out = {}
    for r in rosters:
        need(r, ["roster_id", "owner_id"], "roster")
        rid = str(r["roster_id"])
        uid = r["owner_id"] or (by_picker[rid].most_common(1)[0][0] if by_picker[rid] else None)
        if rid in overrides:
            out[rid] = overrides[rid]
        elif uid and uid in cfg["users"]:
            out[rid] = cfg["users"][uid]
        elif uid:
            name = (users.get(uid) or {}).get("display_name") or uid
            out[rid] = name
            print(f"  WARNING {season}: Sleeper user {uid} ('{name}') is not in config/managers.json; "
                  "showing them under their Sleeper name. Add them to fix the label.")
        else:
            out[rid] = f"Roster {rid}"
            print(f"  WARNING {season}: roster {rid} has no owner. Add it to roster_overrides in config/managers.json.")
    return out


def fetch_season(cfg, season, league):
    """Return (rows, note). rows is [] if there is no completed auction yet."""
    lid = league["league_id"]
    drafts = get(f"/league/{lid}/drafts")
    autions = [d for d in drafts if d.get("type") == "auction"]
    if not autions:
        return [], f"{season}: no auction draft (draft types: {[d.get('type') for d in drafts]})"
    done = [d for d in autions if d.get("status") == "complete"]
    if not done:
        return [], f"{season}: auction draft exists but is '{autions[0].get('status')}' - waiting"
    draft = done[0]
    picks = get(f"/draft/{draft['draft_id']}/picks")
    if not picks:
        return [], f"{season}: draft complete but no picks returned yet"
    mgr = roster_managers(cfg, season, lid, picks)
    rows = []
    for p in picks:
        need(p, ["pick_no", "roster_id", "player_id", "metadata"], f"pick in {season}")
        m = p["metadata"]
        need(m, ["amount", "position"], f"pick metadata in {season}")
        rows.append({
            "season": season, "draft_id": draft["draft_id"], "pick_no": p["pick_no"],
            "roster_id": p["roster_id"], "picked_by": p.get("picked_by") or "",
            "manager": mgr[str(p["roster_id"])], "player_id": p["player_id"],
            "first_name": m.get("first_name", ""), "last_name": m.get("last_name", ""),
            "position": m["position"], "nfl_team": m.get("team", ""), "amount": int(m["amount"]),
        })
    return rows, f"{season}: {len(rows)} picks from draft {draft['draft_id']}"


def sync_sleeper(cfg, refresh):
    cache_path = ROOT / "data" / "sleeper_picks.csv"
    cached = read_csv(cache_path) if cache_path.exists() else []
    have = {int(r["season"]) for r in cached}
    print("Sleeper: discovering league chain ...")
    leagues = discover_leagues(cfg)
    print(f"  seasons on Sleeper: {list(leagues)}; already cached: {sorted(have)}")
    new_rows, notes = [], []
    for season, league in leagues.items():
        if season in have and not refresh:
            continue
        rows, note = fetch_season(cfg, season, league)
        notes.append(note)
        print("  " + note)
        if rows:
            new_rows += rows
            have.add(season)
    if new_rows:
        replaced = {int(r["season"]) for r in new_rows}
        cached = [r for r in cached if int(r["season"]) not in replaced] + [
            {k: str(v) for k, v in r.items()} for r in new_rows]
        cached.sort(key=lambda r: (int(r["season"]), int(r["pick_no"])))
    newest = leagues[max(leagues)]
    latest = {"id": str(newest["league_id"]), "name": newest.get("name") or ""}
    return cached, cache_path, latest, leagues


# ----------------------------------------------------------------------------- standings
STANDING_COLS = ["season", "manager", "rank", "rumbles", "h2h_w", "h2h_l", "pf", "teams", "place"]
SLEEPER_PLAYOFFS_FROM = 2026   # earlier podiums are recorded by hand in config/playoffs.json, not taken from Sleeper
RUMBLE_BONUS = 9      # a head-to-head win is worth 9 on top of 1 per team outscored (a perfect week = teams + 8)


def fetch_standings(cfg, season, league, picks_cache):
    """Finish of every team in a completed Sleeper season, by the league's own 'Rumbles' rule.

    Each regular-season week a team earns 1 rumble per team it outscored plus 9 for winning its matchup
    (so a perfect week is teams + 8: 18 with 10 teams, 20 with 12). Rank is by total rumbles, then points
    for. This reproduces the spreadsheet's Rumble R for every team in 2021-2025. Also finds the playoff
    result (1 champion, 2 runner-up, 3 third place) from Sleeper's bracket, from 2026 on only.
    """
    lid = league["league_id"]
    if league.get("status") != "complete":
        return [], f"{season}: league is '{league.get('status')}', standings not final yet"
    start = int((league.get("settings") or {}).get("playoff_week_start") or 0)
    weeks = start - 1 if start > 1 else 15
    mgr = {}
    for r in picks_cache:
        if int(r["season"]) == season:
            mgr[int(r["roster_id"])] = r["manager"]
    for rid, name in roster_managers(cfg, season, lid, []).items():
        mgr.setdefault(int(rid), name)
    rumbles, pf, hw, hl = Counter(), Counter(), Counter(), Counter()
    for w in range(1, weeks + 1):
        games = get(f"/league/{lid}/matchups/{w}") or []
        pts = {g["roster_id"]: float(g.get("points") or 0) for g in games}
        mid = {g["roster_id"]: g.get("matchup_id") for g in games}
        if not pts or not any(pts.values()):
            continue
        for rid, p in pts.items():
            opp = [k for k in pts if k != rid and mid.get(k) is not None and mid[k] == mid[rid]]
            won = bool(opp) and p > pts[opp[0]]
            rumbles[rid] += sum(1 for k, v in pts.items() if k != rid and p > v) + (RUMBLE_BONUS if won else 0)
            pf[rid] += p
            hw[rid] += 1 if won else 0
            hl[rid] += 1 if opp and p < pts[opp[0]] else 0
    if not rumbles:
        return [], f"{season}: no matchup scores found"
    order = sorted(rumbles, key=lambda k: (-rumbles[k], -pf[k]))
    place = {}
    try:
        bracket = (get(f"/league/{lid}/winners_bracket") or []) if season >= SLEEPER_PLAYOFFS_FROM else []
    except RuntimeError:
        bracket = []
    for g in bracket:
        if g.get("w") is None:
            continue
        if g.get("p") == 1:
            place[g["w"]], place[g["l"]] = 1, 2
        elif g.get("p") == 3:
            place[g["w"]] = 3
    rows = [{"season": season, "manager": mgr.get(rid, f"Roster {rid}"), "rank": i, "rumbles": rumbles[rid],
             "h2h_w": hw[rid], "h2h_l": hl[rid], "pf": round(pf[rid]), "teams": len(order), "place": place.get(rid, 0)}
            for i, rid in enumerate(order, 1)]
    return rows, f"{season}: standings for {len(rows)} teams ({weeks} regular-season weeks)"


def sync_standings(cfg, leagues, picks_cache, refresh):
    path = ROOT / "data" / "sleeper_standings.csv"
    cached = read_csv(path) if path.exists() else []
    have = {int(r["season"]) for r in cached}
    new = []
    for season, league in leagues.items():
        if season in have and not refresh:
            continue
        rows, note = fetch_standings(cfg, season, league, picks_cache)
        print("  " + note)
        new += rows
    if new:
        got = {r["season"] for r in new}
        cached = [r for r in cached if int(r["season"]) not in got] + [{k: str(v) for k, v in r.items()} for r in new]
        cached.sort(key=lambda r: (int(r["season"]), int(r["rank"])))
        write_csv(path, cached, STANDING_COLS)
    return cached


def build_standings(sleeper_rows):
    """{season: {manager: [rank, place, wins, losses, points for, rumbles, teams]}}; place 1/2/3 = champion, runner-up,
    third place, 9 = Sacko (last place after the losers bracket), 0 = none of those.

    2013-2020 come from data/history_standings.csv (the spreadsheet's Lifetime tab), 2021 on from Sleeper.
    The podium through 2025 is in config/playoffs.json; from 2026 it comes from Sleeper's bracket (a
    config entry, if present, wins)."""
    rows = {}
    hist = ROOT / "data" / "history_standings.csv"
    for r in (read_csv(hist) if hist.exists() else []):
        rows.setdefault(int(r["season"]), {})[r["manager"]] = [int(r["rank"]), 0, int(r["h2h_w"]), int(r["h2h_l"]),
                                                              int(r["pf"]), int(float(r["rumbles"])), int(r["teams"])]
    for r in sleeper_rows:
        rows.setdefault(int(r["season"]), {})[r["manager"]] = [int(r["rank"]), int(r.get("place") or 0) if int(r["season"]) >= SLEEPER_PLAYOFFS_FROM else 0, int(r["h2h_w"]),
                                                              int(r["h2h_l"]), int(r["pf"]), int(float(r["rumbles"])), int(r["teams"])]
    p = ROOT / "config" / "playoffs.json"
    manual = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    for season, podium in manual.items():
        if season.startswith("_") or int(season) not in rows:
            continue
        for key, code in (("champion", 1), ("second", 2), ("third", 3), ("sacko", 9)):
            name = podium.get(key)
            if not name:
                continue
            if name not in rows[int(season)]:
                print(f"  WARNING config/playoffs.json: {season} {key} '{name}' is not a manager that season")
                continue
            for v in rows[int(season)].values():        # one team per podium spot
                if v[1] == code:
                    v[1] = 0
            rows[int(season)][name][1] = code
    return {str(s): rows[s] for s in sorted(rows)}


# ----------------------------------------------------------------------------- canonical table
def canonical_rows(history, sleeper):
    rows = []
    for r in history:
        if int(r["season"]) > SHEET_LAST_SEASON:
            continue
        rows.append({"season": int(r["season"]), "manager": r["manager"], "player": r["player"],
                     "player_id": r["player_id"], "player_key": r["player_key"],
                     "position": r["position"], "price": int(r["price"]), "pick_no": "",
                     "_order": int(r["row_order"]), "source": "sheet"})
    for r in sleeper:
        pos = r["position"]
        if pos == "DEF":
            nick = DEF_NICK.get(r["player_id"]) or r["last_name"]
            player, key, pid = nick, f"def:{nick}", ""
        else:
            player = f"{r['first_name']} {r['last_name']}".strip()
            key, pid = f"id:{r['player_id']}", r["player_id"]
        rows.append({"season": int(r["season"]), "manager": r["manager"], "player": player,
                     "player_id": pid, "player_key": key, "position": pos,
                     "price": int(r["amount"]), "pick_no": int(r["pick_no"]),
                     "_order": int(r["pick_no"]), "source": "sleeper",
                     "_team": r["player_id"] if pos == "DEF" else r.get("nfl_team", "")})
    return rows


def add_depth(rows):
    """Depth = rank by price within (season, manager, position). Ties: earlier pick / row first."""
    groups = defaultdict(list)
    for r in rows:
        groups[(r["season"], r["manager"], r["position"])].append(r)
    for g in groups.values():
        g.sort(key=lambda r: (-r["price"], r["_order"]))
        for i, r in enumerate(g, 1):
            r["depth"] = i
    rows.sort(key=lambda r: (r["season"], r["manager"], r["position"], r["depth"]))


def add_slots(rows):
    """Lay each team out like a lineup card, for the Drafts view and the Excel boards.

    Purely by price (ties go to the earlier pick). The lineup is config/lineups.json:
      1. fixed slots take the priciest player at that position (RB1 in the first RB slot, RB2 in the
         second, and so on; QB, TE, K, D/ST the same);
      2. flex slots are then filled in the order given by "fill_order": SFLEX takes the 2nd-priciest
         QB (or, with no second QB, the priciest RB/WR/TE left), FLEX the priciest RB/WR left, W/T the
         priciest WR/TE left;
      3. everyone else goes to the bench, priciest first.
    Sets r["slot"] and r["row"] (line on the board) and returns {season: [slot label per board line]}.
    """
    cfg = load_json("lineups.json")
    eligible, prefer = cfg["eligible"], cfg.get("prefer", {})
    fill_order, lineups = cfg.get("fill_order", []), sorted(cfg["lineups"], key=lambda e: e["from"])
    teams = defaultdict(list)
    for r in rows:
        teams[(r["season"], r["manager"])].append(r)
    starters_of, bench_len = {}, defaultdict(int)
    price_first = lambda r: (-r["price"], r["_order"])
    for (season, _), rs in teams.items():
        starters = next((e["starters"] for e in reversed(lineups) if e["from"] <= season),
                        lineups[0]["starters"])
        starters_of[season] = starters
        free = sorted(rs, key=price_first)
        filled = [None] * len(starters)
        flex_rank = lambda i: fill_order.index(starters[i]) if starters[i] in fill_order else len(fill_order)
        for i in sorted(range(len(starters)), key=lambda i: (len(eligible[starters[i]]) > 1, flex_rank(i), i)):
            label = starters[i]
            pick = (next((r for r in free if r["position"] in prefer.get(label, [])), None)
                    or next((r for r in free if r["position"] in eligible[label]), None))
            if pick:
                free.remove(pick)
                filled[i] = pick
        for i, r in enumerate(filled):
            if r:
                r["slot"], r["row"] = starters[i], i
        for k, r in enumerate(free):                       # free is still priciest-first
            r["slot"], r["row"] = "BE", len(starters) + k
        bench_len[season] = max(bench_len[season], len(free))
    return {s: starters_of[s] + ["BE"] * bench_len[s] for s in starters_of}


def add_order(canon):
    """Left-to-right manager order for each season's board: the old spreadsheet's order
    (data/board_order.json), and for newer seasons last season's order, newcomers at the end."""
    path = ROOT / "data" / "board_order.json"
    known = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    present = defaultdict(set)
    for r in canon:
        present[r["season"]].add(r["manager"])
    order, prev = {}, []
    for s in sorted(present):
        base = known.get(str(s)) or prev
        cur = [m for m in base if m in present[s]]
        cur += sorted(present[s] - set(cur), key=str.lower)
        order[s] = prev = cur
    return order


def sync_player_meta(canon, offline):
    """college by player_key (data/player_meta.csv). New Sleeper players are looked up in Sleeper's
    player database, which is only downloaded when there is somebody new to look up."""
    path = ROOT / "data" / "player_meta.csv"
    meta = {r["player_key"]: r["college"] for r in read_csv(path)} if path.exists() else {}
    missing = {r["player_key"] for r in canon if r["player_key"].startswith("id:") and r["player_key"] not in meta}
    if missing and not offline:
        try:
            players = get("/players/nfl", timeout=180)
            for k in missing:
                meta[k] = (players.get(k[3:]) or {}).get("college") or ""
            write_csv(path, [{"player_key": k, "college": v} for k, v in sorted(meta.items())], ["player_key", "college"])
            print(f"  looked up the college of {len(missing)} new player(s)")
        except (RuntimeError, ValueError) as e:
            print(f"  WARNING: could not fetch Sleeper's player database ({e}); their colleges stay blank for now")
    return meta


def add_team_college(canon, meta):
    """Attach NFL team (franchise name, as of that season) and college to every pick."""
    hteams = {}
    p = ROOT / "data" / "history_teams.csv"
    if p.exists():
        hteams = {(int(r["season"]), r["player_key"]): r["team"] for r in read_csv(p)}
    extras = {k: v for k, v in load_json("player_extras.json").items() if not k.startswith("_")}
    for r in canon:
        ex = extras.get(r["player"], {})
        if r["position"] == "DEF":
            r["team"], r["college"] = NICK_FULL.get(r["player"], ""), ""
            continue
        code = r.pop("_team", None)
        if code is None:
            code = hteams.get((r["season"], r["player_key"]), "")
        code = (ex.get("teams") or {}).get(str(r["season"])) or code
        r["team"] = TEAM_FULL.get(code, "")
        r["college"] = clean_college(meta.get(r["player_key"]) or ex.get("college"))


# ----------------------------------------------------------------------------- reconcile
def reconcile(canon, reference):
    """Compare the old spreadsheet with Sleeper for seasons both cover. Never overrides anything."""
    ref_by = defaultdict(list)
    for r in reference:
        ref_by[(int(r["season"]), r["manager"])].append(r)
    sl_by = defaultdict(list)
    for r in canon:
        if r["source"] == "sleeper":
            sl_by[(r["season"], r["manager"])].append(r)
    seasons = sorted({s for s, _ in ref_by} & {s for s, _ in sl_by})
    lines = ["# Spreadsheet vs Sleeper reconcile report", "",
             "Generated by `scripts/update_data.py`. Sleeper is used for the charts from 2021 on; this file lists where the "
             "old spreadsheet disagrees so a human can look. Nothing here changes the data.", ""]
    if not seasons:
        lines.append("No overlap seasons to compare.")
        return "\n".join(lines) + "\n"
    lines += ["## Position totals ($) - spreadsheet / Sleeper", "",
              "| Season | QB | RB | WR | TE | K | DEF |", "|---|---|---|---|---|---|---|"]
    detail = []
    for s in seasons:
        a, b = Counter(), Counter()
        for (ss, _), rs in ref_by.items():
            if ss == s:
                for r in rs:
                    a[r["position"]] += int(r["price"])
        for (ss, _), rs in sl_by.items():
            if ss == s:
                for r in rs:
                    b[r["position"]] += r["price"]
        cells = []
        for p in ["QB", "RB", "WR", "TE", "K", "DEF"]:
            cells.append(f"{a[p]}" if a[p] == b[p] else f"**{a[p]} / {b[p]}**")
        lines.append(f"| {s} | " + " | ".join(cells) + " |")
    for s in seasons:
        for m in sorted({m for (ss, m) in ref_by if ss == s} | {m for (ss, m) in sl_by if ss == s}):
            sheet = [(int(r["price"]), r["position"], r["name_raw"]) for r in ref_by.get((s, m), [])]
            slp = [(r["price"], r["position"], r["player"]) for r in sl_by.get((s, m), [])]
            unmatched_sheet, unmatched_sl = list(sheet), list(slp)
            for x in list(sheet):  # exact price+position pairs
                for y in unmatched_sl:
                    if x[0] == y[0] and x[1] == y[1] and x in unmatched_sheet:
                        unmatched_sheet.remove(x)
                        unmatched_sl.remove(y)
                        break
            for x in list(unmatched_sheet):  # same price, different position label
                for y in unmatched_sl:
                    if x[0] == y[0]:
                        detail.append(f"- {s} {m}: position differs at ${x[0]}: sheet has {x[1]} '{x[2]}', "
                                      f"Sleeper has {y[1]} '{y[2]}'")
                        unmatched_sheet.remove(x)
                        unmatched_sl.remove(y)
                        break
            for x in unmatched_sheet:
                detail.append(f"- {s} {m}: only in spreadsheet: {x[1]} '{x[2]}' ${x[0]}")
            for y in unmatched_sl:
                detail.append(f"- {s} {m}: only in Sleeper: {y[1]} '{y[2]}' ${y[0]}")
    lines += ["", "## Differences", ""]
    lines += detail or ["None - every pick matches on manager, price and position."]
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------- site data
def build_site_json(canon, sources_note, boards, league, standings):
    eras = load_json("eras.json")["eras"]
    seasons = sorted({r["season"] for r in canon})
    latest = seasons[-1]
    per = defaultdict(set)
    for r in canon:
        per[r["manager"]].add(r["season"])
    managers = [{"name": m, "seasons": sorted(s), "current": latest in s}
                for m, s in sorted(per.items(), key=lambda kv: (latest not in kv[1], kv[0].lower()))]
    teams = {str(s): len({r["manager"] for r in canon if r["season"] == s}) for s in seasons}
    spent = {str(s): sum(r["price"] for r in canon if r["season"] == s) for s in seasons}
    source = {str(s): next(r["source"] for r in canon if r["season"] == s) for s in seasons}
    cols = ["season", "manager", "player", "player_key", "position", "price", "depth", "pick_no", "row", "team", "college"]
    return {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "latest_season": latest, "seasons": seasons, "teams": teams, "spent": spent,
        "source": source, "league": league, "eras": eras, "managers": managers, "sources_note": sources_note,
        "boards": boards, "columns": cols,
        "standings": standings,
        "standings_columns": ["rank", "place", "wins", "losses", "points_for", "rumbles", "teams"],
        "picks": [[r["season"], r["manager"], r["player"], r["player_key"], r["position"],
                   r["price"], r["depth"], r["pick_no"] if r["pick_no"] != "" else None, r["row"],
                   r["team"], r["college"]]
                  for r in canon],
    }


# ----------------------------------------------------------------------------- main
def main(argv):
    refresh = "--refresh" in argv
    offline = "--offline" in argv
    cfg = load_json("managers.json")

    latest = None
    if offline:
        cache_path = ROOT / "data" / "sleeper_picks.csv"
        sleeper = read_csv(cache_path) if cache_path.exists() else []
        sp = ROOT / "data" / "sleeper_standings.csv"
        sleeper_standings = read_csv(sp) if sp.exists() else []
    else:
        sleeper, cache_path, latest, leagues = sync_sleeper(cfg, refresh)
        write_csv(cache_path, sleeper, PICK_COLS)
        sleeper_standings = sync_standings(cfg, leagues, sleeper, refresh)
    # the league's current name on Sleeper (it can be renamed); offline runs keep what the last run saw
    league = {"id": str(cfg["seed_league_id"]), "name": "DTF Club"}
    old_site = ROOT / "site" / "data.json"
    if old_site.exists():
        try:
            league.update({k: v for k, v in json.loads(old_site.read_text(encoding="utf-8")).get("league", {}).items() if v})
        except ValueError:
            pass
    if latest:
        league["id"] = latest["id"]
        if latest["name"]:
            league["name"] = latest["name"]

    history = read_csv(ROOT / "data" / "history_2012_2020.csv")
    canon = canonical_rows(history, sleeper)
    add_depth(canon)
    ref_path = ROOT / "data" / "sheet_reference_2021_plus.csv"
    reference = read_csv(ref_path) if ref_path.exists() else []
    add_team_college(canon, sync_player_meta(canon, offline))
    slots, order = add_slots(canon), add_order(canon)
    boards = {str(s): {"slots": slots[s], "order": order[s]} for s in sorted(slots)}
    write_csv(ROOT / "data" / "picks.csv", canon, CANON_COLS)

    report = reconcile(canon, reference)
    (ROOT / "data" / "reconcile_report.md").write_text(report, encoding="utf-8")

    first_sleeper = min((int(r["season"]) for r in sleeper), default=None)
    note = (f"2012-{SHEET_LAST_SEASON}: spreadsheet. {first_sleeper}-{max(r['season'] for r in canon)}: Sleeper."
            if first_sleeper else "Spreadsheet only.")
    site = build_site_json(canon, note, boards, league, build_standings(sleeper_standings))
    out = ROOT / "site" / "data.json"
    out.parent.mkdir(exist_ok=True)
    if out.exists():  # keep the old timestamp when nothing changed, so runs don't create empty commits
        try:
            old = json.loads(out.read_text(encoding="utf-8"))
            if all(old.get(k) == site[k] for k in ("picks", "eras", "boards", "league", "standings")):
                site["generated"] = old["generated"]
        except (ValueError, KeyError):
            pass
    blob = json.dumps(site, separators=(",", ":"))
    out.write_text(blob, encoding="utf-8")
    # data.js lets the page work when index.html is simply double-clicked (no web server needed)
    (out.parent / "data.js").write_text("window.DTF_DATA=" + blob + ";", encoding="utf-8")
    print(f"wrote {len(canon)} picks, seasons {site['seasons'][0]}-{site['seasons'][-1]}, "
          f"{len(site['managers'])} managers")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except SchemaError as e:
        print("\n=== SLEEPER SCHEMA PROBLEM ===\n" + str(e), file=sys.stderr)
        sys.exit(2)
