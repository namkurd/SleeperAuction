#!/usr/bin/env python3
"""Season totals for EVERY fantasy-relevant NFL player (not just the ones drafted in DTF), straight from
Sleeper's public weekly stat feed, scored with the same rules build_season_ranks.py uses.

Why it exists: the over/underrated analysis (scripts/build_player_context.py) needs to know how a player
finished the PREVIOUS season -- games played, position rank, points -- even when nobody in the league drafted
him that year, and season_ranks.csv only keeps player-seasons that were drafted in DTF.

    python scripts/build_nfl_seasons.py             # fetch anything missing, needs internet
    python scripts/build_nfl_seasons.py --refresh   # redo every season already cached
    python scripts/build_nfl_seasons.py --offline   # nothing fetched

Writes data/nfl_seasons.csv: season, sleeper_id, position, points, rank, n, games, started, snaps, team_snaps
  rank/n: rank among every NFL player at that position that season (same definition as season_ranks.csv)
  games: weeks played (Sleeper "gp"); started: weeks started ("gs"); snaps/team_snaps: offensive snaps
  played / his team's total offensive snaps (Sleeper "snp" / "tm_off_snp", summed) -- a snap share.
Only QB/RB/WR/TE rows with at least one game are kept. Seasons that haven't finished are skipped, so a
season shows up here once its league is complete, exactly like season_ranks.csv.
"""
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update_data as U  # noqa: E402
import build_season_ranks as R  # noqa: E402

ROOT = U.ROOT
COLS = ["season", "sleeper_id", "position", "points", "rank", "n", "games", "started", "snaps", "team_snaps"]
POS = {"QB", "RB", "WR", "TE"}
FIRST_SEASON = 2011                     # one year before the first DTF draft, so 2012 picks have a "last season"
LAST_COMPLETE_FALLBACK = 2025


def fetch_season(season, scoring, players):
    weeks = R.WEEKS.get(season, 17)
    def one(wk):
        return U.get(f"/stats/nfl/regular/{season}/{wk}", timeout=60)
    with ThreadPoolExecutor(max_workers=6) as ex:
        data = list(ex.map(one, range(1, weeks + 1)))
    totals = defaultdict(lambda: defaultdict(float))
    for wk_stats in data:
        for pid, cats in wk_stats.items():
            t = totals[pid]
            for k, v in cats.items():
                if isinstance(v, (int, float)):
                    t[k] += v
    rows = []
    for pid, t in totals.items():
        if len(pid) <= 3 and pid.isalpha():
            continue
        pos = (players.get(pid) or {}).get("position")
        if pos not in POS or round(t.get("gp", 0)) < 1:
            continue
        pts = sum(v * scoring.get(k, 0) for k, v in t.items())
        rows.append({"season": season, "sleeper_id": pid, "position": pos, "points": round(pts, 1),
                     "games": round(t.get("gp", 0)), "started": round(t.get("gs", 0)),
                     "snaps": round(t.get("snp", 0)), "team_snaps": round(t.get("tm_off_snp", 0))})
    by_pos = defaultdict(list)
    for r in rows:
        by_pos[r["position"]].append(r)
    for lst in by_pos.values():
        lst.sort(key=lambda r: -r["points"])
        for i, r in enumerate(lst, 1):
            r["rank"] = i
            r["n"] = len(lst)
    return rows


def main(argv):
    out = ROOT / "data" / "nfl_seasons.csv"
    cached = U.read_csv(out) if out.exists() else []
    have = {int(r["season"]) for r in cached}
    if "--offline" in argv:
        print(f"nfl_seasons.csv has {len(cached)} rows for seasons {sorted(have)}; --offline, nothing fetched")
        return 0
    cfg = U.load_json("managers.json")
    leagues = U.discover_leagues(cfg)
    players = None
    new = []
    for season in range(FIRST_SEASON, max(leagues) + 1):
        if season in have and "--refresh" not in argv:
            continue
        if season in leagues and leagues[season].get("status") != "complete":
            print(f"  {season}: league not complete yet, skipping")
            continue
        scoring = R.season_scoring(season, leagues) if season >= 2021 else R.season_scoring(season, {})
        if scoring is None:
            print(f"  {season}: no scoring settings, skipped")
            continue
        if players is None:
            players = R.load_players()
        print(f"  {season}: fetching {R.WEEKS.get(season, 17)} weeks ...")
        new.extend(fetch_season(season, scoring, players))
    if new:
        got = {r["season"] for r in new}
        cached = [r for r in cached if int(r["season"]) not in got] + new
        cached.sort(key=lambda r: (int(r["season"]), r["position"], int(r["rank"])))
        U.write_csv(out, cached, COLS)
        print(f"wrote {len(cached)} rows to data/nfl_seasons.csv")
    else:
        print("nothing new to fetch")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
