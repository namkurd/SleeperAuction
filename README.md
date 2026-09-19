# DTF Club Auction Archive

An auto-updating dashboard of every DTF Club auction draft since 2012: league-wide market trends, a profile for each manager, and a price history for any player.

**Live site:** https://namkurd.github.io/SleeperAuction/

When a new season's Sleeper auction draft is marked complete, a GitHub Action picks it up, rebuilds the data and republishes the site. Nobody has to touch anything.

## One-time setup (about 5 minutes, no command line)

1. On github.com click **New repository**, name it `SleeperAuction`, keep it **Public**, and create it (no README or other files).
2. In the new repo open **Settings > Pages** and set **Source** to **GitHub Actions**.
3. Unzip the download. Back in the repo click **Add file > Upload files** and drag in everything from the unzipped folder: `README.md`, `config`, `data`, `scripts`, `site` **and the hidden `.github` and `.gitignore`** (on a Mac press Cmd+Shift+. in Finder to show hidden files first; Windows shows them already). Click **Commit changes**.
4. Open the **Actions** tab. The run that starts is the first build; when it is green the site is live at the address above. If the run failed because step 2 was skipped, do step 2 and click **Re-run failed jobs**.

If the Actions tab is empty, `.github/workflows/update.yml` didn't upload. Click **Add file > Create new file**, type `.github/workflows/update.yml` as the name, paste in that file's contents from the unzipped folder, and commit.

Prefer a terminal? `git init -b main && git add . && git commit -m "Initial import"`, then `gh repo create namkurd/SleeperAuction --public --source=. --remote=origin`, `gh api -X POST repos/namkurd/SleeperAuction/pages -f build_type=workflow`, `git push -u origin main`.

## What's in the dashboard

| Tab | What it shows |
|---|---|
| League | Dollars per position per season, dollars per depth slot (QB1-3, RB1-3, WR1-3, TE1-2, K1), and a dot for every pick ever made. Dashed gold lines mark scoring-rule changes. |
| Managers | Pick any current or former manager: the same two charts for just that person, real year gaps when they sat out, optional league-average overlay. |
| Players | Search any player: what they cost each year they were bought, who bought them, and their depth slot on that team. |

Every chart has a data table under it. All amounts are raw dollars.

## How it works

```
Sleeper API ─┐
             ├─> scripts/update_data.py ─> data/picks.csv ─> site/data.json + data.js ─> GitHub Pages
spreadsheet ─┘   (2012-2020 frozen in data/history_2012_2020.csv)
```

- **2012-2020** come from the old spreadsheet, cleaned once and frozen in `data/history_2012_2020.csv` (co-owners folded into one manager, misspelled and nicknamed players matched to real players).
- **2021 onward** come from Sleeper. Its prices match the spreadsheet apart from two $1 discrepancies in 2021, and its positions are right where the spreadsheet has a few mislabels (a TE typed as WR, a kicker typed as DEF). Sleeper also has real player ids and cleaner names, so it wins wherever the two differ. `data/reconcile_report.md` lists every difference so you can look.
- **Depth is strictly by price.** Within a manager, season and position the highest-priced player is 1, the next is 2, and so on (ties go to the earlier pick). The old charts used the spreadsheet's lineup order, so a few depth numbers moved slightly.
- Finished Sleeper seasons are cached in `data/sleeper_picks.csv`, so the archive survives even if Sleeper someday deletes an old league.

### Files

| Path | Purpose |
|---|---|
| `scripts/update_data.py` | Fetches Sleeper, computes depth, writes every data file. Standard library only. |
| `scripts/check_data.py` | Sanity checks (budgets, gaps, duplicates). The Action stops before publishing if these fail. |
| `scripts/build_history.py` | One-time importer for the spreadsheet. You will almost never need it again. |
| `config/managers.json` | Sleeper `user_id` to manager name, plus roster overrides. |
| `config/eras.json` | The dashed scoring-era lines. |
| `config/aliases.json` | Co-owner folding and player-name fixes for the spreadsheet years. |
| `site/` | The static website (`index.html`, Plotly bundled in `vendor/`). |
| `.github/workflows/update.yml` | Runs every 6 hours, on demand, and when you push. |

## Everyday tasks

**A new manager joins.** Nothing breaks: they show up under their Sleeper display name and the run prints a warning. To give them a proper name, add their Sleeper `user_id` to `config/managers.json` (find it at `https://api.sleeper.app/v1/user/<username>`), then run the workflow.

**Scoring rules change.** Add a line to `config/eras.json`, for example `{ "label": "Full PPR", "from": 2027 }`, and push. `from` is the first season the rule applies; the line is drawn between the previous season and that one. Use `<br>` in a label for a line break.

**A season came out wrong.** Actions tab, "Update auction data and publish site", "Run workflow", tick "Re-download every Sleeper season".

**A roster has no owner in Sleeper** (this happened to Tommy in 2023). Add it under `roster_overrides` in `config/managers.json`: `{"2023": {"9": "Tommy"}}`.

**Preview locally.**

```bash
python scripts/update_data.py          # or add --offline to skip the network
python -m http.server --directory site 8000
# open http://localhost:8000
```

(`site/index.html` also works if you just double-click it.)

## Keeping the schedule alive

GitHub turns scheduled workflows off after 60 days without repository activity, and the off-season is longer than that. The workflow guards against it twice: every scheduled run re-enables itself (`gh workflow enable`), and if the repo has had no commit for 45 days it writes a tiny `data/heartbeat.txt` commit. If you ever see a "scheduled workflows disabled" banner in the Actions tab, click **Enable workflow** once.

## Known quirks

- **2026 has 12 teams** ($2,398 spent vs about $2,000 before), so 2026 totals are not comparable with earlier years. The site says so in a footnote.
- **Sleeper records a player's position as of today.** If someone changes position later (a QB turned TE, say), Sleeper would show the new one. In the 2021-2026 seasons compared against the spreadsheet, every position difference turned out to be a spreadsheet mislabel, not a position change, and the reconcile report would flag a real one.
- **A few old spreadsheet names could not be matched** to a real player (retired players missing from Sleeper's database, joke nicknames). They keep their spreadsheet name. See `data/history_name_map.csv` for every match decision and fix any by adding to `config/aliases.json`, then re-running `scripts/build_history.py`.
- **Manager position charts include every player a manager bought**, bench depth included, so each season adds up to their budget. The older manager screenshots only counted their top three RBs and WRs.
- Spreadsheet-era player identity relies on name and era, so two different players who share a name and position in nearby years could be merged. The name map shows every guess.

Plotly.js (bundled in `site/vendor`) is MIT licensed.
