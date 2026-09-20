#!/usr/bin/env python3
"""ONE-TIME script: turn the legacy spreadsheet (2012-2020) into data/history_2012_2020.csv.

The output is frozen and committed. You only need to re-run this if you find a
mistake in the old data. It is NOT part of the automatic update.

    pip install pandas openpyxl rapidfuzz
    python scripts/build_history.py "DTF Auction.xlsx"

What it does
  * reads the `Drafting` sheet, keeps seasons <= LAST_SHEET_SEASON (Sleeper is the
    source of truth from 2021 on)
  * folds co-owner labels into one manager (config/aliases.json -> owner_aliases)
  * cleans DEF names to a team nickname
  * resolves messy player names ("cmc", "Gostowski", "Antionio Brown") to a real
    player using Sleeper's public player database (fuzzy match + manual overrides in
    config/aliases.json -> sheet_name_overrides)
  * writes data/history_2012_2020.csv and data/history_name_map.csv (audit trail)
  * writes data/board_order.json: the left-to-right manager order of each season's board on the
    `Auctions` tab (used by the site's Drafts view)
  * writes data/sheet_reference_2021_plus.csv: the spreadsheet's later rows, kept ONLY so
    update_data.py can print a sheet-vs-Sleeper reconcile report (never used for charts)
"""
import json
import re
import sys
import unicodedata
import urllib.request
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parent.parent
LAST_SHEET_SEASON = 2020
SKILL = {"QB", "RB", "WR", "TE", "K"}
ORDER_ONLY = False
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

# nickname -> canonical DEF label. Keys are matched against words in the sheet cell.
TEAMS = {
    "Cardinals": ["cardinals", "cards", "arizona", "ari"],
    "Falcons": ["falcons", "atlanta", "atl"],
    "Ravens": ["ravens", "baltimore", "bal"],
    "Bills": ["bills", "buffalo", "buf"],
    "Panthers": ["panthers", "carolina", "car"],
    "Bears": ["bears", "chicago", "chi"],
    "Bengals": ["bengals", "cincinnati", "cin"],
    "Browns": ["browns", "cleveland", "cle"],
    "Cowboys": ["cowboys", "dallas", "dal"],
    "Broncos": ["broncos", "denver", "den"],
    "Lions": ["lions", "detroit", "det"],
    "Packers": ["packers", "green bay", "gb"],
    "Texans": ["texans", "houston", "hou"],
    "Colts": ["colts", "indianapolis", "ind"],
    "Jaguars": ["jaguars", "jags", "jacksonville", "jax"],
    "Chiefs": ["chiefs", "kansas city", "kc"],
    "Raiders": ["raiders", "oakland", "las vegas", "lv"],
    "Chargers": ["chargers", "san diego", "lac"],
    "Rams": ["rams", "st louis", "la rams", "lar"],
    "Dolphins": ["dolphins", "fins", "miami", "mia"],
    "Vikings": ["vikings", "minnesota", "min"],
    "Patriots": ["patriots", "pats", "new england", "ne"],
    "Saints": ["saints", "new orleans", "no"],
    "Giants": ["giants", "nyg"],
    "Jets": ["jets", "nyj"],
    "Eagles": ["eagles", "philadelphia", "phi"],
    "Steelers": ["steelers", "pittsburgh", "pit"],
    "49ers": ["49ers", "9ers", "niners", "san francisco", "sf"],
    "Seahawks": ["seahawks", "seattle", "sea"],
    "Buccaneers": ["buccaneers", "bucs", "tampa bay", "tampa", "tb"],
    "Titans": ["titans", "tennessee", "ten"],
    "Commanders": ["commanders", "redskins", "football team", "washington", "was"],
}


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm(name):
    s = strip_accents(str(name)).lower()
    s = re.sub(r"[.'’`\-]", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    toks = [t for t in s.split() if t not in SUFFIXES]
    return " ".join(toks)


def clean_def(raw):
    s = strip_accents(str(raw)).lower()
    s = re.sub(r"d/?st\b|\bd\b|\bdefense\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = " ".join(s.split())
    for nick, keys in TEAMS.items():
        for k in keys:
            if s == k or re.search(rf"\b{re.escape(k)}\b", s):
                return nick
    return None


def load_players():
    cache = ROOT / "data" / ".players_cache.json"
    if cache.exists():
        return json.loads(cache.read_text())
    with urllib.request.urlopen("https://api.sleeper.app/v1/players/nfl", timeout=90) as r:
        data = json.load(r)
    return data


def build_index(players):
    idx = []
    for pid, p in players.items():
        pos = p.get("position")
        if pos not in SKILL:
            continue
        full = p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip()
        if not full:
            continue
        by = None
        if p.get("birth_date"):
            try:
                by = int(p["birth_date"][:4])
            except ValueError:
                pass
        idx.append({
            "pid": pid, "full": full, "pos": pos, "n": norm(full),
            "first": norm(p.get("first_name") or ""), "last": norm(p.get("last_name") or ""),
            "by": by, "rank": p.get("search_rank") or 9999999,
            "fp": set(p.get("fantasy_positions") or [pos]),
        })
    return idx


def plausible(c, year):
    if c["by"] is None:
        return True
    return 20 <= year - c["by"] <= 44


def resolve(raw, pos, year, idx, overrides):
    """Return (full_name, player_id, method, score, note)."""
    n = norm(raw)
    target = overrides.get(f"{n}|{pos}|{year}") or overrides.get(f"{n}|{pos}") or overrides.get(n)
    if target and target.startswith("="):       # "=Name": keep unmatched, but show this name
        return target[1:], "", "override-unmatched", 100, ""
    query = norm(target) if target else n
    pool = [c for c in idx if (c["pos"] == pos or pos in c["fp"]) and plausible(c, year)]
    res = _match(query, pool, year, target)
    if res is None and pos != "K":
        # the sheet sometimes lists a player under the wrong position (e.g. a RB as WR)
        pool = [c for c in idx if c["pos"] != "K" and plausible(c, year)]
        res = _match(query, pool, year, target, min_score=92)
        if res and len(query.split()) == 1 and res[4]:
            res = None  # last-name-only + wrong position + several candidates: don't guess
        if res:
            res = (res[0], res[1], res[2] + "+posmismatch", res[3], res[4])
    if res is None and target:                  # a nickname we fixed by hand, but the real player is not in Sleeper's database
        return target, "", "override-unmatched", 100, "not in Sleeper's player database"
    return res


def _match(query, pool, year, target, min_score=85):
    if not pool:
        return None
    toks = query.split()
    scored = []
    for c in pool:
        s_full = fuzz.ratio(query, c["n"])
        s = s_full
        if len(toks) >= 2:
            last_sim = fuzz.ratio(toks[-1], c["last"])
            f = toks[0]
            if len(f) <= 2:
                first_sim = 90 if c["first"][:1] == f[0] else 0          # "P Perkins", "D. Bryant"
            elif c["first"] == f:
                first_sim = 100
            elif len(f) >= 3 and (c["first"].startswith(f) or f.startswith(c["first"])):
                first_sim = 95                                            # Matt/Matthew, Pat/Patrick
            else:
                first_sim = fuzz.ratio(f, c["first"])
            if last_sim >= 88 and first_sim >= 80:
                s = max(s, 0.7 * last_sim + 0.3 * first_sim)
        else:
            last_sim = fuzz.ratio(toks[0], c["last"]) if toks else 0
            s = last_sim if last_sim >= 88 else 0                         # last name only
        scored.append((s, c, s_full))
    scored = [(s, c, sf) for s, c, sf in scored if s >= min_score]
    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], t[1]["rank"], -(t[1]["by"] or 0)))
    best = scored[0][0]
    exact = [c for s, c, sf in scored if sf >= 99]
    top = exact if exact else [c for s, c, sf in scored if s >= best - 3]
    method = "override" if target else "fuzzy"
    if len(top) == 1:
        c = top[0]
        return c["full"], c["pid"], method, best, ""
    # several plausible players: prefer the fantasy-relevant one, else flag
    ranked = sorted(top, key=lambda c: c["rank"])
    if not exact and ranked[0]["rank"] < 9999999:
        c = ranked[0]
        return c["full"], c["pid"], method + "+rank", best, "ambiguous: " + "; ".join(x["full"] for x in top)
    # same name twice (e.g. two "Mike Williams"): pick the one whose age best fits the year
    c = sorted(top, key=lambda c: abs((year - (c["by"] or year - 26)) - 26))[0]
    return c["full"], c["pid"], method + "+age", best, "ambiguous: " + "; ".join(f"{x['full']} ({x['by']})" for x in top)


def write_board_order(xlsx, owner_alias):
    """Read the manager column order of every board on the `Auctions` tab -> data/board_order.json."""
    a = pd.read_excel(xlsx, sheet_name="Auctions", header=None)
    order = {}
    for i, row in a.iterrows():
        cell = row[0]
        if isinstance(cell, str) and cell.strip().endswith("Auction Draft") and cell.strip()[:4].isdigit():
            names = [v for v in a.iloc[i + 1, 1:] if isinstance(v, str)]
            order[cell.strip()[:4]] = [owner_alias.get(n, n) for n in names]
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "board_order.json").write_text(
        json.dumps(dict(sorted(order.items())), indent=1), encoding="utf-8")
    print(f"wrote board order for {len(order)} seasons")


def main(xlsx):
    aliases = json.loads((ROOT / "config" / "aliases.json").read_text())
    owner_alias = aliases["owner_aliases"]
    write_board_order(xlsx, owner_alias)
    if ORDER_ONLY:
        return
    overrides = {"|".join([norm(k.split("|")[0])] + k.split("|")[1:]): v
                 for k, v in aliases["sheet_name_overrides"].items()}

    full = pd.read_excel(xlsx, sheet_name="Drafting", usecols=range(7))
    ref = full[full.Year > LAST_SHEET_SEASON].copy()
    ref["manager"] = ref.Owner.replace(owner_alias)
    ref = ref.rename(columns={"Year": "season", "Position": "position", "Player": "name_raw", "Price": "price"})
    (ROOT / "data").mkdir(exist_ok=True)
    ref[["season", "manager", "position", "name_raw", "price"]].to_csv(
        ROOT / "data" / "sheet_reference_2021_plus.csv", index=False)   # only used by the reconcile report
    df = full[full.Year <= LAST_SHEET_SEASON].reset_index(drop=True)
    df["row_order"] = range(len(df))
    df["manager"] = df.Owner.replace(owner_alias)

    print("loading Sleeper player database ...")
    players = load_players()
    idx = build_index(players)

    out, namemap = [], {}
    for r in df.itertuples():
        raw, pos, year = str(r.Player).strip(), r.Position, int(r.Year)
        pid, method, score, note = "", "", "", ""
        if pos == "DEF":
            nick = clean_def(raw)
            if nick is None:
                sys.exit(f"Unrecognised DEF name: {raw!r} ({year})")
            player, key, method = nick, f"def:{nick}", "team"
        else:
            res = resolve(raw, pos, year, idx, overrides)
            if res:
                player, pid, method, score, note = res
                key = f"id:{pid}" if pid else f"name:{norm(player)}|{pos}"
            else:
                player = re.sub(r"\s+", " ", raw).strip().title()
                key, method = f"name:{norm(raw)}|{pos}", "unresolved"
        out.append({
            "season": year, "manager": r.manager, "player": player, "player_id": pid,
            "player_key": key, "position": pos, "price": int(r.Price),
            "pick_no": "", "row_order": r.row_order, "name_raw": raw, "source": "sheet",
        })
        namemap[(raw, pos, year)] = (player, pid, method, score, note)

    hist = pd.DataFrame(out)
    (ROOT / "data").mkdir(exist_ok=True)
    hist.to_csv(ROOT / "data" / "history_2012_2020.csv", index=False)

    rows = [
        {"name_raw": k[0], "position": k[1], "season": k[2], "resolved": v[0], "player_id": v[1],
         "method": v[2], "score": v[3], "note": v[4],
         "price": int(df[(df.Player.str.strip() == k[0]) & (df.Position == k[1]) & (df.Year == k[2])].Price.max())}
        for k, v in namemap.items()
    ]
    nm = pd.DataFrame(rows).sort_values(["method", "note", "name_raw"])
    nm.to_csv(ROOT / "data" / "history_name_map.csv", index=False)
    print(nm.method.value_counts().to_string())
    print(f"wrote {len(hist)} rows")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    ORDER_ONLY = "--order-only" in sys.argv       # refresh only data/board_order.json
    main(args[0] if args else "DTF Auction.xlsx")
