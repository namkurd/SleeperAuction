#!/usr/bin/env python3
"""Work out, for every DTF pick, how that player finished the real NFL season: his rank among every
NFL player at his position that year, by total fantasy points under OUR league's own scoring rules
(not a generic PPR/standard preset). Also incremental: update_data.py calls this each run to pick up
a season once it just finished.

    python scripts/build_season_ranks.py             # fetch anything missing, needs internet
    python scripts/build_season_ranks.py --refresh    # redo every season already cached
    python scripts/build_season_ranks.py --offline    # no network, just re-check the existing cache

How the points are computed: Sleeper's public weekly stat feed
(https://api.sleeper.app/v1/stats/nfl/regular/<season>/<week>) gives every NFL player's (and every team
defense's) raw box-score categories for that week, for any season -- it goes back to 2012, well before
our league was on Sleeper. Summing a season's worth of those and multiplying by a league's real
`scoring_settings` reproduces Sleeper's own reported fantasy points exactly (checked against actual
2023 matchup totals: computed 184.8, Sleeper reported 184.8). For 2021 on we use the real
scoring_settings of that season's Sleeper league. For 2012-2020 (before we were on Sleeper) there is no
league object to read settings from, so config/scoring_pre2021.json supplies them by hand; until that
file exists, those seasons are simply skipped (picks from 2012-2020 show no rank until it's filled in).

Ranking uses the real NFL regular season only (17 weeks through 2020, 18 from 2021), not our league's
own shorter regular-season window and not the NFL playoffs -- the usual meaning of "finished RB4" etc.

Writes data/season_ranks.csv: season, player_key, position, points, rank, n, games
  rank/n are computed against EVERY NFL player who played that position that season, but only rows for
  players who were actually drafted in DTF that season are kept (that's all the site needs). games is
  how many of that season's weeks he actually played (Sleeper's "gp" stat) -- it's what lets a pick be
  graded Injury instead of Fail when a real injury, not a bad pick, wrecked the season.
Team defenses are keyed by their Sleeper team-abbreviation stat entry and matched to our def:<Nickname>
player_key with the same DEF_NICK table update_data.py uses, so history stays consistent if a team
changes abbreviation.
"""
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update_data as U  # noqa: E402  (reuse get/read_csv/write_csv/DEF_NICK/load_json)

ROOT = U.ROOT
RANK_COLS = ["season", "player_key", "position", "points", "rank", "n", "games"]
POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}
WEEKS = {**{y: 17 for y in range(2012, 2021)}, **{y: 18 for y in range(2021, 2027)}}


def load_players():
    cache = ROOT / "data" / ".players_cache.json"
    if cache.exists():
        return json.loads(cache.read_text())
    print("  downloading Sleeper player database ...")
    return U.get("/players/nfl", timeout=180)


def season_scoring(season, leagues):
    if season >= 2021:
        lid = leagues[season]["league_id"]
        return U.get(f"/league/{lid}")["scoring_settings"]
    path = ROOT / "config" / "scoring_pre2021.json"
    if not path.exists():
        return None
    cfg = json.loads(path.read_text(encoding="utf-8"))
    return cfg.get(str(season)) or cfg.get("default")


def compute_season(season, scoring, players, weeks):
    """{stat_id (Sleeper player id or team code): fantasy points}, plus how many of those weeks he
    actually played (Sleeper's "gp" stat category, summed the same way as every other stat) -- that
    games-played count is what lets the pick-quality grade (site/index.html) tell a real injury-wrecked
    season apart from a healthy player who just didn't perform."""
    totals = defaultdict(lambda: defaultdict(float))
    for wk in range(1, weeks + 1):
        wk_stats = U.get(f"/stats/nfl/regular/{season}/{wk}", timeout=60)
        for pid, cats in wk_stats.items():
            t = totals[pid]
            for k, v in cats.items():
                if isinstance(v, (int, float)):
                    t[k] += v
    points, position, games = {}, {}, {}
    for pid, t in totals.items():
        pts = sum(v * scoring.get(k, 0) for k, v in t.items())
        gp = round(t.get("gp", 0))
        if len(pid) <= 3 and pid.isalpha():          # team defense entry, e.g. "NYJ"
            key = f"def:{U.DEF_NICK.get(pid, pid)}"
            points[key] = pts
            position[key] = "DEF"
            games[key] = gp
        else:
            pos = (players.get(pid) or {}).get("position")
            if pos in POSITIONS:
                key = f"id:{pid}"
                points[key] = pts
                position[key] = pos
                games[key] = gp
    return points, position, games


def rank_within_position(points, position, games):
    by_pos = defaultdict(list)
    for key, pts in points.items():
        by_pos[position[key]].append((key, pts))
    ranks = {}
    for pos, rows in by_pos.items():
        rows.sort(key=lambda kv: -kv[1])
        n = len(rows)
        for i, (key, pts) in enumerate(rows, 1):
            ranks[key] = (round(pts, 1), i, n, games.get(key, 0))
    return ranks


def main(argv):
    refresh = "--refresh" in argv
    offline = "--offline" in argv
    out_path = ROOT / "data" / "season_ranks.csv"
    cached = U.read_csv(out_path) if out_path.exists() else []
    have = {int(r["season"]) for r in cached}

    needed = defaultdict(set)   # season -> {player_key wanted}
    for r in U.read_csv(ROOT / "data" / "picks.csv"):
        needed[int(r["season"])].add(r["player_key"])
    seasons = sorted(s for s in needed if s in WEEKS)

    if offline:
        print(f"season_ranks.csv has {len(cached)} rows for seasons {sorted(have)}; --offline, nothing fetched")
        return 0

    cfg = U.load_json("managers.json")
    leagues = None
    players = None
    new_rows = []
    for season in seasons:
        if season in have and not refresh:
            continue
        if leagues is None:
            leagues = U.discover_leagues(cfg)
        if season in leagues and leagues[season].get("status") != "complete":
            print(f"  {season}: league not complete yet, skipping")
            continue
        scoring = season_scoring(season, leagues)
        if scoring is None:
            print(f"  {season}: no scoring settings available yet (add config/scoring_pre2021.json for "
                  "seasons before 2021) -- skipped")
            continue
        if players is None:
            players = load_players()
        print(f"  {season}: fetching {WEEKS[season]} weeks of NFL stats ...")
        points, position, games = compute_season(season, scoring, players, WEEKS[season])
        ranks = rank_within_position(points, position, games)
        got, missing = 0, []
        for key in needed[season]:
            if key in ranks:
                pts, rank, n, gp = ranks[key]
                new_rows.append({"season": season, "player_key": key, "position": position[key],
                                 "points": pts, "rank": rank, "n": n, "games": gp})
                got += 1
            else:
                missing.append(key)
        print(f"    {got}/{len(needed[season])} of that season's picks ranked" +
              (f"; not found in the stat feed: {missing[:6]}{'...' if len(missing) > 6 else ''}" if missing else ""))

    if new_rows:
        got_seasons = {r["season"] for r in new_rows}
        cached = [r for r in cached if int(r["season"]) not in got_seasons] + new_rows
        cached.sort(key=lambda r: (int(r["season"]), r["position"], int(r["rank"])))
        U.write_csv(out_path, cached, RANK_COLS)
        print(f"wrote {len(cached)} rows to data/season_ranks.csv")
    else:
        print("nothing new to fetch")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
