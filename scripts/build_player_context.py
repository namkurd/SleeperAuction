#!/usr/bin/env python3
"""Situation facts for every QB/RB/WR/TE pick, known (or knowable) when the auction happened, so the site's
"Overrated & underrated" analysis can ask which kinds of player the league keeps overpaying for or
underpaying for.

    python scripts/build_player_context.py             # compute seasons missing from the cache, needs internet
    python scripts/build_player_context.py --refresh   # redo every season (also redoes the newest season)
    python scripts/build_player_context.py --offline   # nothing fetched

Writes data/player_context.csv, one row per (season, player_key) for QB/RB/WR/TE picks:
  age          age on Sept 1 of the season (years, one decimal)
  rookie       1 if this is his first NFL season
  exp          NFL seasons played before this one
  team         NFL team he finished that season on (nflverse roster, newest week listed)
  prev_team    NFL team he finished the previous season on
  new_team     1 if team != prev_team (a veteran who changed teams); blank for rookies / unknown
  prev_games   games played last season (0 = did not play), prev_started games started
  prev_rank    his position rank last season (blank if he didn't play), prev_pts his points
  team_prev_rank   his team's rank (1 = best of 32) last season by regular-season point differential
  team_prev_wpct   his team's win percentage last season
  new_qb       1 if the team's Week 1 starting QB is not last season's primary starter (not set for QBs)
  new_hc       1 if the team's head coach in Week 1 is not last season's main head coach -- the closest
               public stand-in for "new offensive coordinator", which no free dataset records
  ol_prev_rank the team's offensive line rank (1 = best of 32) LAST season, from team sack rate and
               rushing EPA per carry (see ol_scores). A proxy: it measures what the line produced, with no
               pass-block / run-block grades behind it. Only last season's line is kept on purpose: this
               season's is partly caused by the very player being judged (a great RB lifts his team's rushing
               EPA), so it would be hindsight, not something knowable at the auction.

Sources (all public, no key): nflverse rosters / team stats / schedules+coaches+starting QBs / players,
the DynastyProcess id table (Sleeper id -> NFL id), Sleeper's player database, and data/nfl_seasons.csv
(scripts/build_nfl_seasons.py) for last season's games and rank.
"""
import csv
import io
import statistics as st
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update_data as U  # noqa: E402
import build_season_ranks as R  # noqa: E402

ROOT = U.ROOT
OUT = ROOT / "data" / "player_context.csv"
COLS = ["season", "player_key", "age", "rookie", "exp", "team", "prev_team", "new_team", "prev_games",
        "prev_started", "prev_rank", "prev_pts", "team_prev_rank", "team_prev_wpct", "new_qb", "new_hc",
        "ol_prev_rank"]
POS = {"QB", "RB", "WR", "TE"}
REL = "https://github.com/nflverse/nflverse-data/releases/download"
RAW = "https://raw.githubusercontent.com"
TEAM_NORM = {"ARZ": "ARI", "BLT": "BAL", "HST": "HOU", "CLV": "CLE", "SL": "LA", "STL": "LA", "SD": "LAC",
             "OAK": "LV", "LAR": "LA", "JAC": "JAX", "WSH": "WAS"}
nt = lambda t: TEAM_NORM.get(t, t)
_cache = {}


def fetch_csv(url):
    if url in _cache:
        return _cache[url]
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SleeperAuction-dashboard/1.0"})
            with urllib.request.urlopen(req, timeout=120) as r:
                text = r.read().decode("utf-8", "replace")
            rows = list(csv.DictReader(io.StringIO(text)))
            _cache[url] = rows
            return rows
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"could not download {url}: {last}")


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def id_maps():
    """Sleeper id -> NFL (gsis) id, plus birth date / rookie season by gsis id."""
    dp = fetch_csv(f"{RAW}/dynastyprocess/data/master/files/db_playerids.csv")
    s2g, born, draft = {}, {}, {}
    for r in dp:
        sid = r.get("sleeper_id") or ""
        g = r.get("gsis_id") or ""
        if sid and g and g != "NA":
            try:
                s2g[str(int(float(sid)))] = g
            except ValueError:
                pass
        if g and g != "NA":
            born[g] = r.get("birthdate") if r.get("birthdate") not in (None, "NA") else ""
            draft[g] = r.get("draft_year") if r.get("draft_year") not in (None, "NA") else ""
    for sid, p in R.load_players().items():
        if p.get("gsis_id") and sid not in s2g:
            s2g[sid] = p["gsis_id"]
        if p.get("gsis_id") and not born.get(p["gsis_id"]):
            born[p["gsis_id"]] = p.get("birth_date") or ""
    rookie = {}
    for r in fetch_csv(f"{REL}/players/players.csv"):
        g = r.get("gsis_id")
        if not g:
            continue
        if r.get("birth_date"):
            born[g] = r["birth_date"]
        rookie[g] = r.get("rookie_season") or r.get("draft_year") or ""
    for g, d in draft.items():
        rookie.setdefault(g, d)
    return s2g, born, rookie


def roster_team(season):
    """gsis id -> team he was on in the newest week (regular season or playoffs) he appears in that roster file."""
    rows = list(fetch_csv(f"{REL}/rosters/roster_{season}.csv"))
    rows.sort(key=lambda r: num(r.get("week")) or 0)
    return {r["gsis_id"]: nt(r["team"]) for r in rows if r.get("gsis_id")}


def team_season_facts(games):
    """{(season, team): dict} from the schedule file: record, point differential, week-1 and primary QB, coach."""
    facts = defaultdict(lambda: {"w": 0, "l": 0, "t": 0, "pd": 0.0, "g": 0, "qb": Counter(), "wk1_qb": None,
                                 "coach": Counter(), "wk1_coach": None})
    for r in games:
        if r.get("game_type") != "REG" or not r.get("home_score") or not r.get("away_score"):
            if not (r.get("game_type") == "REG"):
                continue
        season, wk = int(r["season"]), int(r["week"])
        for side, other in (("home", "away"), ("away", "home")):
            team = nt(r[f"{side}_team"])
            f = facts[(season, team)]
            if r.get(f"{side}_qb_id"):
                f["qb"][r[f"{side}_qb_id"]] += 1
                if f["wk1_qb"] is None or wk < f["wk1_week"]:
                    f["wk1_qb"], f["wk1_week"] = r[f"{side}_qb_id"], wk
            if r.get(f"{side}_coach"):
                f["coach"][r[f"{side}_coach"]] += 1
                if f["wk1_coach"] is None or wk < f.get("wk1c_week", 99):
                    f["wk1_coach"], f["wk1c_week"] = r[f"{side}_coach"], wk
            if r.get(f"{side}_score") and r.get(f"{other}_score"):
                mine, theirs = float(r[f"{side}_score"]), float(r[f"{other}_score"])
                f["g"] += 1
                f["pd"] += mine - theirs
                if mine > theirs:
                    f["w"] += 1
                elif mine < theirs:
                    f["l"] += 1
                else:
                    f["t"] += 1
    return facts


def ol_scores(season):
    """team -> offensive-line rank (1 = best) for one season from sack rate (lower is better) and rushing EPA
    per carry (higher is better), each standardised across the league and summed."""
    rows = [r for r in fetch_csv(f"{REL}/stats_team/stats_team_week_{season}.csv") if r.get("season_type") == "REG"]
    tot = defaultdict(lambda: Counter())
    for r in rows:
        t = tot[nt(r["team"])]
        for k in ("sacks_suffered", "attempts", "carries", "rushing_epa"):
            t[k] += num(r.get(k)) or 0
    sr = {t: c["sacks_suffered"] / max(1, c["attempts"] + c["sacks_suffered"]) for t, c in tot.items()}
    re_ = {t: c["rushing_epa"] / max(1, c["carries"]) for t, c in tot.items()}
    z = lambda d: {t: (v - st.mean(d.values())) / st.pstdev(d.values()) for t, v in d.items()}
    zs, zr = z(sr), z(re_)
    score = {t: -zs[t] + zr[t] for t in tot}
    order = sorted(score, key=lambda t: -score[t])
    return {t: i + 1 for i, t in enumerate(order)}


def main(argv):
    cached = U.read_csv(OUT) if OUT.exists() else []
    picks = [r for r in U.read_csv(ROOT / "data" / "picks.csv") if r["position"] in POS]
    wanted = defaultdict(set)
    for r in picks:
        wanted[int(r["season"])].add(r["player_key"])
    have = defaultdict(set)
    for r in cached:
        have[int(r["season"])].add(r["player_key"])
    if "--offline" in argv:
        print(f"player_context.csv has {len(cached)} rows; --offline, nothing fetched")
        return 0
    todo = [s for s in sorted(wanted) if "--refresh" in argv or wanted[s] - have[s]]
    if not todo:
        print("player_context.csv is up to date")
        return 0
    print(f"  building player context for {todo} ...")
    s2g, born, rookie = id_maps()
    seasons_tbl = {}
    for r in U.read_csv(ROOT / "data" / "nfl_seasons.csv"):
        seasons_tbl[(int(r["season"]), r["sleeper_id"])] = r
    facts = team_season_facts(fetch_csv(f"{RAW}/nflverse/nfldata/master/data/games.csv"))
    # team strength rank per season by point differential
    tier = {}
    for s in {k[0] for k in facts}:
        teams = [(t, f["pd"]) for (ss, t), f in facts.items() if ss == s and f["g"]]
        for i, (t, _) in enumerate(sorted(teams, key=lambda x: -x[1]), 1):
            tier[(s, t)] = i
    rosters, ol = {}, {}
    new_rows = []
    for s in todo:
        for y in (s - 1, s):
            if y not in rosters:
                rosters[y] = roster_team(y)
        if (s - 1) not in ol:
            ol[s - 1] = ol_scores(s - 1)
        sep1 = date(s, 9, 1)
        for r in picks:
            if int(r["season"]) != s:
                continue
            sid, key = r["player_id"], r["player_key"]
            g = s2g.get(sid)
            if not g:        # unmapped: keep a blank row so the cache knows this pick was looked at
                new_rows.append({"season": s, "player_key": key})
                continue
            row = {"season": s, "player_key": key}
            bd = born.get(g)
            try:
                y, m, d = (int(x) for x in bd[:10].split("-"))
                row["age"] = round((sep1 - date(y, m, d)).days / 365.25, 1)
            except Exception:  # noqa: BLE001
                row["age"] = ""
            rk = rookie.get(g)
            try:
                rk = int(float(rk))
            except (TypeError, ValueError):
                rk = None
            prev = seasons_tbl.get((s - 1, sid))
            team, prev_team = rosters[s].get(g, ""), rosters[s - 1].get(g, "")
            is_rookie = (rk == s) if rk else (not prev and not prev_team)
            row["rookie"] = 1 if is_rookie else 0
            row["exp"] = max(0, s - rk) if rk else ""
            row["team"], row["prev_team"] = team, prev_team
            row["new_team"] = (1 if team and prev_team and team != prev_team else 0) if (team and prev_team and not is_rookie) else ""
            if not is_rookie and (s - 1) in {k[0] for k in seasons_tbl}:
                row["prev_games"] = int(prev["games"]) if prev else 0
                row["prev_started"] = int(prev["started"]) if prev else 0
                row["prev_rank"] = int(prev["rank"]) if prev else ""
                row["prev_pts"] = prev["points"] if prev else ""
            else:
                row["prev_games"] = row["prev_started"] = row["prev_rank"] = row["prev_pts"] = ""
            tf, pf = facts.get((s, team)), facts.get((s - 1, team))
            row["team_prev_rank"] = tier.get((s - 1, team), "")
            row["team_prev_wpct"] = round((pf["w"] + .5 * pf["t"]) / pf["g"], 3) if pf and pf["g"] else ""
            if tf and pf and tf["wk1_qb"] and pf["qb"]:
                row["new_qb"] = "" if r["position"] == "QB" else int(tf["wk1_qb"] != pf["qb"].most_common(1)[0][0])
            else:
                row["new_qb"] = ""
            row["new_hc"] = int(tf["wk1_coach"] != pf["coach"].most_common(1)[0][0]) if tf and pf and tf["wk1_coach"] and pf["coach"] else ""
            row["ol_prev_rank"] = ol[s - 1].get(team, "")
            new_rows.append(row)
        print(f"    {s}: {sum(1 for r in new_rows if r['season'] == s)} rows")
    done = {r["season"] for r in new_rows}
    cached = [r for r in cached if int(r["season"]) not in done] + new_rows
    cached.sort(key=lambda r: (int(r["season"]), r["player_key"]))
    U.write_csv(OUT, cached, COLS)
    print(f"wrote {len(cached)} rows to data/player_context.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
