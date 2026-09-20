#!/usr/bin/env python3
"""ONE-TIME script: work out each spreadsheet-era player's NFL team (per season) and college.

The old spreadsheet only has names and prices. Sleeper's player database has each player's college but
only his *current* team, so for 2012-2020 the team comes from the public nflverse season rosters
(https://github.com/nflverse/nflverse-data, roster_<season>.csv). From 2021 on the team is stored on
every Sleeper pick, so nothing is needed here for those seasons.

    pip install pandas
    python scripts/build_player_meta.py          # needs internet; takes about a minute

Writes
  data/history_teams.csv   season, player_key, team (NFL abbreviation on that season's roster)
  data/player_meta.csv     player_key, college  (spreadsheet era and 2021+ alike; update_data.py
                                                 adds new players itself as seasons arrive)
Hand fixes live in config/player_extras.json (retired players missing from both sources, nicknames).
Rosters are season-level: a player traded mid-season can show either of his teams.
"""
import csv
import json
import re
import sys
import unicodedata
import urllib.request
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download/rosters/roster_{}.csv"


def norm(name):
    s = "".join(c for c in unicodedata.normalize("NFKD", str(name)) if not unicodedata.combining(c)).lower()
    s = re.sub(r"[.'’`\-]", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(t for t in s.split() if t not in SUFFIXES)


def load_players():
    cache = ROOT / "data" / ".players_cache.json"
    if cache.exists():
        return json.loads(cache.read_text())
    print("downloading Sleeper player database ...")
    with urllib.request.urlopen("https://api.sleeper.app/v1/players/nfl", timeout=120) as r:
        return json.load(r)


def pick_row(cands, pos):
    """Choose the roster row for a name: same position first, and for players on two teams the earlier stint."""
    if len(cands) and pos == "RB":
        c2 = cands[cands.position.isin(["RB", "FB"])]
        cands = c2 if len(c2) else cands
    elif len(cands):
        c2 = cands[cands.position == pos]
        cands = c2 if len(c2) else cands
    if not len(cands):
        return None
    return cands.sort_values("week", kind="stable").iloc[0]


def main():
    extras = json.loads((ROOT / "config" / "player_extras.json").read_text(encoding="utf-8"))
    players = load_players()
    hist = list(csv.DictReader(open(ROOT / "data" / "history_2012_2020.csv", encoding="utf-8")))
    sleeper = list(csv.DictReader(open(ROOT / "data" / "sleeper_picks.csv", encoding="utf-8")))

    rosters = {}
    for y in sorted({int(r["season"]) for r in hist}):
        print(f"nflverse roster {y} ...")
        d = pd.read_csv(NFLVERSE.format(y), low_memory=False)
        d["n"] = d.full_name.map(norm)
        d["last"] = d.last_name.map(norm)
        d["init"] = d.first_name.map(lambda s: norm(s)[:1])
        rosters[y] = d

    teams, college = {}, {}
    for r in hist:
        if r["position"] == "DEF":
            continue
        y, key, pos, name = int(r["season"]), r["player_key"], r["position"], r["player"]
        ex = extras.get(name) or extras.get(key) or {}
        d = rosters[y]
        row = pick_row(d[d.n == norm(name)], pos)
        if row is None:                                    # e.g. Robbie Chosen (Sleeper) vs Robby Anderson (nflverse)
            parts = norm(name).split()
            if len(parts) >= 2:
                c = d[(d["last"] == parts[-1]) & (d.init == parts[0][:1]) & (d.position.isin([pos, "FB"] if pos == "RB" else [pos]))]
                row = pick_row(c, pos) if c.team.nunique() == 1 and len(c) else None
        team = (ex.get("teams") or {}).get(str(y)) or (row.team if row is not None and isinstance(row.team, str) else "")
        teams[(y, key)] = team
        col = ""
        if key.startswith("id:"):
            col = (players.get(key[3:]) or {}).get("college") or ""
        if not col and ex.get("college"):
            col = ex["college"]
        if not col and row is not None and isinstance(row.college, str):
            col = row.college
        if col or key not in college:
            college[key] = col
    for r in sleeper:
        if r["position"] == "DEF":
            continue
        key = f"id:{r['player_id']}"
        if not college.get(key):
            college[key] = (players.get(r["player_id"]) or {}).get("college") or (extras.get(key) or {}).get("college", "")

    with open(ROOT / "data" / "history_teams.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["season", "player_key", "team"])
        for (y, k), t in sorted(teams.items()):
            w.writerow([y, k, t])
    with open(ROOT / "data" / "player_meta.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["player_key", "college"])
        for k, c in sorted(college.items()):
            w.writerow([k, c])
    print(f"teams known {sum(1 for t in teams.values() if t)}/{len(teams)}; colleges known {sum(1 for c in college.values() if c)}/{len(college)}")
    for (y, k), t in sorted(teams.items()):
        if not t:
            print("  no team:", y, k, next(r['player'] for r in hist if int(r['season']) == y and r['player_key'] == k))
    for k, c in sorted(college.items()):
        if not c:
            print("  no college:", k)


if __name__ == "__main__":
    sys.exit(main())
