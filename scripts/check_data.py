#!/usr/bin/env python3
"""Sanity checks on data/picks.csv. The Action runs this before committing, so a bad
Sleeper response can never overwrite good data or reach the site. Exit code 1 = problem."""
import csv
import sys
from collections import defaultdict, Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
rows = list(csv.DictReader(open(ROOT / "data" / "picks.csv", encoding="utf-8")))
problems = []

spend = defaultdict(int)
keys = defaultdict(int)
for r in rows:
    spend[(int(r["season"]), r["manager"])] += int(r["price"])
    if r["player_key"].startswith("id:"):
        keys[(int(r["season"]), r["player_key"])] += 1
    if r["position"] not in {"QB", "RB", "WR", "TE", "K", "DEF"}:
        problems.append(f"unknown position {r['position']!r} for {r['player']} ({r['season']})")
    if int(r["price"]) < 0:
        problems.append(f"negative price: {r['player']} ({r['season']})")

seasons = sorted({s for s, _ in spend})
if seasons != list(range(seasons[0], seasons[-1] + 1)):
    problems.append(f"missing season(s) between {seasons[0]} and {seasons[-1]}: have {seasons}")
for (s, m), v in sorted(spend.items()):
    if v > 200:
        problems.append(f"{s} {m} spent ${v}, over the $200 budget")
for s in seasons:
    n = sum(1 for (ss, _) in spend if ss == s)
    if n < 8:
        problems.append(f"{s} has only {n} teams")
lines = Counter((r["season"], r["manager"], r["row"]) for r in rows)
for (s, m, line), n in lines.items():
    if n > 1:
        problems.append(f"{s} {m}: {n} players share board line {line}")
for (s, k), n in keys.items():
    if n > 1:
        problems.append(f"{s}: player {k} bought {n} times")

# standings (drawn above each year on the Manager charts): every team of a season has a rank, at most one per podium spot
import json
site = json.loads((ROOT / "site" / "data.json").read_text(encoding="utf-8")) if (ROOT / "site" / "data.json").exists() else {}
teams_in = {}
for r in rows:
    teams_in.setdefault(int(r["season"]), set()).add(r["manager"])
for season, block in (site.get("standings") or {}).items():
    n = len(block)
    if set(block) - teams_in.get(int(season), set()):
        problems.append(f"{season} standings name(s) not in that season's draft: {sorted(set(block) - teams_in.get(int(season), set()))}")
    if any(not 1 <= v[0] <= n for v in block.values()):
        problems.append(f"{season} standings: a rank is outside 1..{n}")
    for spot in (1, 2, 3):
        if sum(1 for v in block.values() if v[1] == spot) > 1:
            problems.append(f"{season} standings: more than one team in podium spot {spot}")

if problems:
    print("DATA CHECK FAILED:")
    print("\n".join(" - " + p for p in problems))
    sys.exit(1)
print(f"data check passed: {len(rows)} picks, seasons {seasons[0]}-{seasons[-1]}")
