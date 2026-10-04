#!/usr/bin/env python3
"""Write a clean, labeled, offline backup of the whole archive: standings, every auction pick (with
that player's real-NFL season finish and this project's pick-quality grade), a season-by-season player
score reference, and the manager roster -- everything the live site is built from, in one workbook with
one clear row per record, meant for safekeeping and for spotting/hand-fixing anything mislabeled.

    pip install openpyxl
    python scripts/export_backup.py                 # writes "DTF Club Archive - Backup.xlsx" in the repo root
    python scripts/export_backup.py some/path.xlsx

Reads site/data.json (the same merged data the live dashboard renders -- standings, boards, manager
roster) and data/picks.csv (the full per-pick archive). Run scripts/update_data.py first for the
newest season. Not part of the automatic update.

Sheets
  Read me                 what is here, what each sheet's columns mean, and -- importantly -- which
                           source file to hand-edit if you spot something mislabeled
  Standings                season-by-season finish, record, points, PF+, and playoff result for every
                           manager-season (same numbers the site's Standings view renders)
  Auction Draft Picks      one row per pick ever made: price, roster slot, that season's real-NFL
                           finish, and this project's pick-quality grade
  Player Season Scores     one row per drafted player per season: fantasy points and position rank
                           under the league's own scoring rules
  Managers                 every manager who has played, and which seasons
"""
import csv
import json
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parent.parent
FONT = "Arial"

MEDAL_LABEL = {1: "Champion", 2: "2nd Place", 3: "3rd Place", 9: "Sacko"}
GRADE_META = {
    "banger": "\U0001F525 Banger",
    "hit": "✅ Hit",
    "meh": "\U0001F610 Meh",
    "bust": "\U0001F4C9 Bust",
    "fail": "\U0001F480 Fail",
}
GRADE_GRID = {
    "A": {"top5": "hit",    "top10": "meh",    "great": "meh",    "good": "bust", "replacement": "fail", "deep": "fail"},
    "B": {"top5": "banger", "top10": "hit",    "great": "meh",    "good": "meh",  "replacement": "bust", "deep": "fail"},
    "C": {"top5": "banger", "top10": "banger", "great": "meh",    "good": "meh",  "replacement": "bust", "deep": "fail"},
    "D": {"top5": "banger", "top10": "banger", "great": "hit",    "good": "hit",  "replacement": "meh",  "deep": "meh"},
    "E": {"top5": "banger", "top10": "banger", "great": "banger", "good": "hit",  "replacement": "meh",  "deep": "meh"},
}


def outcome_band(rank):
    if rank <= 5: return "top5"
    if rank <= 10: return "top10"
    if rank <= 20: return "great"
    if rank <= 30: return "good"
    if rank <= 50: return "replacement"
    return "deep"


def price_tier(rank, pool):
    if rank <= max(5, round(pool * .10)): return "A"
    if rank <= round(pool * .25): return "B"
    if rank <= round(pool * .50): return "C"
    if rank <= round(pool * .75): return "D"
    return "E"


def read_picks():
    with open(ROOT / "data" / "picks.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["season"] = int(r["season"])
        r["price"] = int(r["price"])
        r["depth"] = int(r["depth"])
        r["pick_no"] = int(r["pick_no"]) if r["pick_no"] else None
        r["pts"] = float(r["pts"]) if r["pts"] else None
        r["pos_rank"] = int(r["pos_rank"]) if r["pos_rank"] else None
        r["pos_n"] = int(r["pos_n"]) if r["pos_n"] else None
    return rows


def price_ranks(picks):
    """season+position -> each pick's {rank, end, pool, poolMin}, same competition-ranking-with-tie-ranges
    the site uses: priciest pick at the position that draft is rank 1, ties share a rank range. poolMin is
    the cheapest price paid for that position in that draft -- it feeds the "was this really a bargain
    price" check in pick_grade below."""
    groups = {}
    for r in picks:
        groups.setdefault((r["season"], r["position"]), []).append(r)
    out = {}
    for rows in groups.values():
        rows = sorted(rows, key=lambda r: -r["price"])
        pool = len(rows)
        pool_min = min(r["price"] for r in rows)
        i = 0
        while i < len(rows):
            j = i
            while j < len(rows) and rows[j]["price"] == rows[i]["price"]:
                j += 1
            start, end = i + 1, j
            for k in range(i, j):
                out[id(rows[k])] = (start, end, pool, pool_min)
            i = j
    return out


def price_rank_text(pos, rank, end):
    return f"{pos}{rank}" if rank == end else f"{pos}{rank}-{end}"


PQ_NEAR_FLOOR_MARGIN = 1    # a tier C/D "banger" needs a price within this many dollars of the position's floor that year
PQ_NEVER_FAIL_MAX = 5       # a pick priced at or under this never grades worse than Bust


def pick_grade(r, pr):
    """Same grading as the live site's pickGradeKey (site/index.html) -- see the comment there for the
    reasoning behind the two guardrails on top of the tier/band grid."""
    if r["position"] in ("K", "DEF") or r["pos_rank"] is None:
        return ""
    if r["pos_rank"] == 1:
        return GRADE_META["banger"]
    if pr is None:
        return ""
    rank, end, pool, pool_min = pr
    tier, band = price_tier(rank, pool), outcome_band(r["pos_rank"])
    grade = GRADE_GRID[tier][band]
    if grade == "banger" and tier in ("C", "D") and band in ("top5", "top10") and r["price"] > pool_min + PQ_NEAR_FLOOR_MARGIN:
        grade = "hit"
    if grade == "fail" and r["price"] <= PQ_NEVER_FAIL_MAX:
        grade = "bust"
    return GRADE_META[grade]


def roster_label(slot):
    return "D/ST" if slot in ("D/ST", "DEF") else slot


def style_header(ws, ncols):
    fill = PatternFill("solid", fgColor="1F3A5F")
    font = Font(name=FONT, bold=True, color="FFFFFF")
    for c in range(1, ncols + 1):
        cell = ws.cell(row=1, column=c)
        cell.font, cell.fill = font, fill
        cell.alignment = Alignment(horizontal="left", vertical="center")


def style_body(ws):
    base = Font(name=FONT, size=10)
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.font = base


def finish_sheet(ws, widths, freeze="A2", filter_to=None):
    for col, w in zip("ABCDEFGHIJKLMNOPQ", widths):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = freeze
    ws.auto_filter.ref = filter_to or f"A1:{get_column_letter(len(widths))}{ws.max_row}"


def main(out_path):
    picks = read_picks()
    pr = price_ranks(picks)
    site = json.loads((ROOT / "site" / "data.json").read_text(encoding="utf-8"))
    standings, managers = site["standings"], site["managers"]
    generated = site.get("generated", "")

    wb = Workbook()

    # ------------------------------------------------------------------ Read me
    ws = wb.active
    ws.title = "Read me"
    lines = [
        ("DTF Club auction archive - full backup", "title"),
        (f"Generated from the live site data as of {generated}." if generated else "", None),
        ("", None),
        ("What is here", "h"),
        ("Standings: every manager's finish, record, points and playoff result for every season -- the same numbers the site's Standings view shows.", None),
        ("Auction Draft Picks: one row per pick ever made in a DTF auction (2,492 total) -- price, roster slot, how that player finished the real NFL season, and this project's pick-quality grade for the pick.", None),
        ("Player Season Scores: one row per drafted player per season -- fantasy points and position rank (e.g. RB7) under the league's own scoring rules, the same numbers behind the Finish and Pick Quality columns on the Picks sheet.", None),
        ("Managers: every manager who has played in the league and which seasons.", None),
        ("", None),
        ("If something here looks mislabeled", "h"),
        ("This workbook is a read-only snapshot -- editing it does not change the site. To fix something at the source, edit the matching file and re-run scripts/update_data.py:", None),
        ("  - A pick's player, position, price, or team/college: data/picks.csv (player/position/price) or config/player_extras.json (team/college for older players the stat feed misses).", None),
        ("  - A name that's wrong for a 2012-2020 pick (nickname, misspelling): config/aliases.json's sheet_name_overrides, then re-run scripts/build_history.py.", None),
        ("  - A standings number or playoff result: config/playoffs.json (champion/2nd/3rd/Sacko) -- regular-season records come from Sleeper (2021+) or data/history_standings.csv (2012-2020) and aren't hand-edited.", None),
        ("  - A manager's display name: config/managers.json.", None),
        ("", None),
        ("Column notes", "h"),
        ("PF+ : points-for indexed to that season's league average (100 = average team), like baseball's OPS+ -- comparable across seasons even when scoring rules changed.", None),
        ("Price Rank: this pick's price rank among every pick at the same position in that season's draft (1 = priciest); a range (e.g. WR34-37) means tied picks share that rank.", None),
        ("Season Finish / Season Rank: the player's rank among every NFL player at his position that real NFL season (e.g. RB7 = the 7th-highest-scoring RB in the league that year), not just among DTF picks. Blank for the season still in progress and for K/DEF (not ranked).", None),
        ("Pick Quality: this project's grade for how the price matched the outcome -- a cheap pick that massively outperformed grades higher than an expensive pick that merely met expectations. Blank for K/DEF and ungraded picks.", None),
    ]
    for i, (text, kind) in enumerate(lines, 1):
        c = ws.cell(row=i, column=1, value=text)
        c.font = Font(name=FONT, size=15 if kind == "title" else 11, bold=kind in ("title", "h"))
        c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 130

    # ------------------------------------------------------------------ Standings
    ws = wb.create_sheet("Standings")
    cols = ["Season", "Manager", "Regular Season Finish", "Wins", "Losses", "League Size",
            "Points For", "PF+", "Rumbles", "Playoff Finish"]
    ws.append(cols)
    season_avg = {}
    for s, block in standings.items():
        pfs = [v[4] for v in block.values()]
        season_avg[s] = sum(pfs) / len(pfs) if pfs else None
    n_rows = 0
    for s in sorted(standings.keys(), key=int):
        avg = season_avg[s]
        for mgr, v in sorted(standings[s].items(), key=lambda kv: kv[1][0]):
            rank, place, wins, losses, pf, rumbles, teams = v
            pfplus = round(pf / avg * 100) if avg else None
            ws.append([int(s), mgr, rank, wins, losses, teams, pf, pfplus, rumbles, MEDAL_LABEL.get(place, "")])
            n_rows += 1
    style_header(ws, len(cols))
    style_body(ws)
    finish_sheet(ws, (8, 14, 18, 7, 8, 11, 10, 7, 9, 15))

    # ------------------------------------------------------------------ Auction Draft Picks
    ws = wb.create_sheet("Auction Draft Picks")
    cols = ["Season", "Manager", "Player", "Position", "NFL Team", "College", "Price", "Depth Slot",
            "Lineup Slot", "Price Rank", "Season Points", "Season Finish", "Pick Quality",
            "Auction Pick #", "Data Source"]
    ws.append(cols)
    ordered = sorted(picks, key=lambda r: (-r["season"], r["manager"], -r["price"]))
    for r in ordered:
        rk = pr.get(id(r))
        price_rank = price_rank_text(r["position"], rk[0], rk[1]) if rk else ""
        season_finish = f"{r['position']}{r['pos_rank']}" if r["pos_rank"] is not None else ""
        ws.append([
            r["season"], r["manager"], r["player"], r["position"], r["team"] or "",
            "" if r["position"] == "DEF" else (r["college"] or ""), r["price"],
            f"{r['position']}{r['depth']}", roster_label(r["slot"]), price_rank,
            r["pts"] if r["pts"] is not None else "", season_finish, pick_grade(r, rk),
            r["pick_no"] if r["pick_no"] is not None else "", r["source"],
        ])
    style_header(ws, len(cols))
    style_body(ws)
    for row in ws.iter_rows(min_row=2):
        row[6].number_format = '"$"#,##0'          # Price
        if row[10].value != "":
            row[10].number_format = '0.0'          # Season Points
    finish_sheet(ws, (8, 14, 24, 10, 22, 20, 8, 11, 11, 12, 13, 13, 12, 13, 11))

    # ------------------------------------------------------------------ Player Season Scores
    ws = wb.create_sheet("Player Season Scores")
    cols = ["Season", "Player", "Position", "Drafted By", "Season Points", "Season Rank"]
    ws.append(cols)
    scored = [r for r in ordered if r["pts"] is not None and r["position"] not in ("K", "DEF")]
    scored.sort(key=lambda r: (-r["season"], r["position"], r["pos_rank"]))
    for r in scored:
        ws.append([r["season"], r["player"], r["position"], r["manager"], r["pts"],
                    f"{r['position']}{r['pos_rank']}"])
    style_header(ws, len(cols))
    style_body(ws)
    for row in ws.iter_rows(min_row=2):
        row[4].number_format = '0.0'
    finish_sheet(ws, (8, 24, 10, 14, 13, 12))

    # ------------------------------------------------------------------ Managers
    ws = wb.create_sheet("Managers")
    cols = ["Manager", "First Season", "Last Season", "Seasons Played", "Currently Active"]
    ws.append(cols)
    for m in sorted(managers, key=lambda m: m["name"]):
        seasons = m["seasons"]
        ws.append([m["name"], min(seasons), max(seasons), len(seasons), "Yes" if m.get("current") else "No"])
    style_header(ws, len(cols))
    style_body(ws)
    finish_sheet(ws, (14, 12, 12, 14, 14))

    wb.save(out_path)
    print(f"wrote {out_path}: {n_rows} standings rows, {len(ordered)} picks, {len(scored)} season scores, {len(managers)} managers")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "DTF Club Archive - Backup.xlsx"))
