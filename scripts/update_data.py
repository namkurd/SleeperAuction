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
              "pick_no", "depth", "source"]

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


class SchemaError(RuntimeError):
    """Sleeper returned something we do not recognise."""


# ----------------------------------------------------------------------------- http
def get(path, retries=4):
    url = API + path
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SleeperAuction-dashboard/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
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
    return cached, cache_path


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
                     "_order": int(r["pick_no"]), "source": "sleeper"})
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
def build_site_json(canon, sources_note):
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
    cols = ["season", "manager", "player", "player_key", "position", "price", "depth", "pick_no"]
    return {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "latest_season": latest, "seasons": seasons, "teams": teams, "spent": spent,
        "source": source, "eras": eras, "managers": managers, "sources_note": sources_note,
        "columns": cols,
        "picks": [[r["season"], r["manager"], r["player"], r["player_key"], r["position"],
                   r["price"], r["depth"], r["pick_no"] if r["pick_no"] != "" else None] for r in canon],
    }


# ----------------------------------------------------------------------------- main
def main(argv):
    refresh = "--refresh" in argv
    offline = "--offline" in argv
    cfg = load_json("managers.json")

    if offline:
        cache_path = ROOT / "data" / "sleeper_picks.csv"
        sleeper = read_csv(cache_path) if cache_path.exists() else []
    else:
        sleeper, cache_path = sync_sleeper(cfg, refresh)
        write_csv(cache_path, sleeper, PICK_COLS)

    history = read_csv(ROOT / "data" / "history_2012_2020.csv")
    canon = canonical_rows(history, sleeper)
    add_depth(canon)
    write_csv(ROOT / "data" / "picks.csv", canon, CANON_COLS)

    ref_path = ROOT / "data" / "sheet_reference_2021_plus.csv"
    report = reconcile(canon, read_csv(ref_path) if ref_path.exists() else [])
    (ROOT / "data" / "reconcile_report.md").write_text(report, encoding="utf-8")

    first_sleeper = min((int(r["season"]) for r in sleeper), default=None)
    note = (f"2012-{SHEET_LAST_SEASON}: spreadsheet. {first_sleeper}-{max(r['season'] for r in canon)}: Sleeper."
            if first_sleeper else "Spreadsheet only.")
    site = build_site_json(canon, note)
    out = ROOT / "site" / "data.json"
    out.parent.mkdir(exist_ok=True)
    if out.exists():  # keep the old timestamp when nothing changed, so runs don't create empty commits
        try:
            old = json.loads(out.read_text(encoding="utf-8"))
            if old.get("picks") == site["picks"] and old.get("eras") == site["eras"]:
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
