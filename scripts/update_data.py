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
import re
import sys
import unicodedata
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
              "pick_no", "depth", "slot", "row", "source"]

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
                     "_order": int(r["row_order"]), "source": "sheet",
                     "slot": r.get("slot", "")})
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


def _last_name(name):
    """Lower-case last word of a player name, ignoring accents, punctuation and Jr./III."""
    t = "".join(c for c in unicodedata.normalize("NFKD", str(name)) if not unicodedata.combining(c))
    t = re.sub(r"[.'\u2019`]", "", t.lower())
    words = [w for w in re.split(r"[^a-z0-9$-]+", t) if w and w not in ("jr", "sr", "ii", "iii", "iv", "v")]
    return words[-1] if words else ""


def _sheet_slots(picks, ref):
    """Give Sleeper picks the lineup slot the old spreadsheet had for the same team and season.

    Rows are paired by price (strong), position, and last name; each side is used at most once.
    Picks that find no partner keep no slot and are placed by the rule in add_slots()."""
    pairs = []
    for i, p in enumerate(picks):
        pl = _last_name(p["player"])
        for j, q in enumerate(ref):
            same_price = p["price"] == int(q["price"])
            same_name = bool(pl) and pl == _last_name(q["name_raw"])
            if not (same_price or (same_name and p["position"] == q["position"])):
                continue
            score = 4 * same_price + 3 * same_name + 2 * (p["position"] == q["position"])
            pairs.append((-score, p["_order"], j, i))
    pairs.sort()
    used_p, used_q = set(), set()
    for _, _, j, i in pairs:
        if i in used_p or j in used_q:
            continue
        used_p.add(i)
        used_q.add(j)
        picks[i]["slot"] = ref[j]["slot"]
    return len(picks) - len(used_p)


def add_slots(rows, reference=()):
    """Lay each team out like a lineup card, for the Drafts view.

    Slot labels come from the old spreadsheet wherever it has them (2012-2020 rows carry them;
    2021+ Sleeper picks inherit them by matching price/position/name). Anything without a slot,
    which is every season added after the spreadsheet, is placed by rule: fixed slots (QB, RB, WR,
    TE, K, D/ST) take that position's priciest players, then the flex slots (fewest eligible
    positions first) take the priciest player left, and the rest go to the bench. Ties go to the
    earlier pick. The starting lineup itself is config/lineups.json. Sets r["slot"] and r["row"]
    (line on the board) and returns ({season: [slot label for each board line]}, {season: placed mostly by rule?}).
    """
    cfg = load_json("lineups.json")
    eligible, lineups = cfg["eligible"], sorted(cfg["lineups"], key=lambda e: e["from"])
    ref_by = defaultdict(list)
    for q in reference:
        ref_by[(int(q["season"]), q["manager"])].append(q)
    teams = defaultdict(list)
    for r in rows:
        teams[(r["season"], r["manager"])].append(r)
    starters_of, bench_len, unmatched = {}, defaultdict(int), 0
    n_loose, n_all = defaultdict(int), defaultdict(int)
    price_first = lambda r: (-r["price"], r["_order"])
    for (season, mgr), rs in teams.items():
        starters = next((e["starters"] for e in reversed(lineups) if e["from"] <= season),
                        lineups[0]["starters"])
        starters_of[season] = starters
        sleeper_rows = [r for r in rs if r["source"] == "sleeper"]
        if sleeper_rows and ref_by.get((season, mgr)):
            unmatched += _sheet_slots(sleeper_rows, ref_by[(season, mgr)])
        filled, bench, loose = [None] * len(starters), [], []
        # 1) picks that arrive with a slot: put them in the matching starting line, else on the bench
        for r in sorted(rs, key=price_first):
            label = r.get("slot") or ""
            if label == "BE":
                bench.append(r)
            elif label:
                line = next((i for i, sl in enumerate(starters) if sl == label and filled[i] is None), None)
                if line is None:
                    bench.append(r)
                else:
                    filled[line] = r
            else:
                loose.append(r)
        # 2) picks with no slot: fill any empty starting lines by the price rule
        for i in sorted(range(len(starters)), key=lambda i: (len(eligible[starters[i]]), i)):
            if filled[i] is None:
                ok = eligible[starters[i]]
                pick = next((r for r in loose if r["position"] in ok), None)
                if pick:
                    loose.remove(pick)
                    filled[i] = pick
        n_loose[season] += len(loose)
        n_all[season] += len(rs)
        bench = sorted(bench + loose, key=price_first)
        for i, r in enumerate(filled):
            if r:
                r["slot"], r["row"] = starters[i], i
        for k, r in enumerate(bench):
            r["slot"], r["row"] = "BE", len(starters) + k
        bench_len[season] = max(bench_len[season], len(bench))
    if unmatched:
        print(f"note: {unmatched} Sleeper picks had no partner row in the old spreadsheet; placed by the price rule")
    by_rule = {s: n_loose[s] > n_all[s] / 2 for s in starters_of}     # True: mostly placed by the price rule
    return {s: starters_of[s] + ["BE"] * bench_len[s] for s in starters_of}, by_rule


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
def build_site_json(canon, sources_note, boards):
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
    cols = ["season", "manager", "player", "player_key", "position", "price", "depth", "pick_no", "row"]
    return {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "latest_season": latest, "seasons": seasons, "teams": teams, "spent": spent,
        "source": source, "eras": eras, "managers": managers, "sources_note": sources_note,
        "boards": boards, "columns": cols,
        "picks": [[r["season"], r["manager"], r["player"], r["player_key"], r["position"],
                   r["price"], r["depth"], r["pick_no"] if r["pick_no"] != "" else None, r["row"]]
                  for r in canon],
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
    ref_path = ROOT / "data" / "sheet_reference_2021_plus.csv"
    reference = read_csv(ref_path) if ref_path.exists() else []
    (slots, by_rule), order = add_slots(canon, reference), add_order(canon)
    boards = {str(s): {"slots": slots[s], "order": order[s], "by_rule": by_rule[s]} for s in sorted(slots)}
    write_csv(ROOT / "data" / "picks.csv", canon, CANON_COLS)

    report = reconcile(canon, reference)
    (ROOT / "data" / "reconcile_report.md").write_text(report, encoding="utf-8")

    first_sleeper = min((int(r["season"]) for r in sleeper), default=None)
    note = (f"2012-{SHEET_LAST_SEASON}: spreadsheet. {first_sleeper}-{max(r['season'] for r in canon)}: Sleeper."
            if first_sleeper else "Spreadsheet only.")
    site = build_site_json(canon, note, boards)
    out = ROOT / "site" / "data.json"
    out.parent.mkdir(exist_ok=True)
    if out.exists():  # keep the old timestamp when nothing changed, so runs don't create empty commits
        try:
            old = json.loads(out.read_text(encoding="utf-8"))
            if all(old.get(k) == site[k] for k in ("picks", "eras", "boards")):
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
