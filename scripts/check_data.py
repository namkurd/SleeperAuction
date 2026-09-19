#!/usr/bin/env python3
"""Sanity checks on data/picks.csv. The Action runs this before committing, so a bad
Sleeper response can never overwrite good data or reach the site. Exit code 1 = problem."""
import csv
import sys
from collections import defaultdict
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
for (s, k), n in keys.items():
    if n > 1:
        problems.append(f"{s}: player {k} bought {n} times")

if problems:
    print("DATA CHECK FAILED:")
    print("\n".join(" - " + p for p in problems))
    sys.exit(1)
print(f"data check passed: {len(rows)} picks, seasons {seasons[0]}-{seasons[-1]}")
