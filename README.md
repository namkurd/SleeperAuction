# DTF Club Auction Archive

An auto-updating dashboard of every DTF Club auction draft since 2012: league-wide market trends, a profile for each manager, every draft board laid out like the old spreadsheet, and a price history for any player.

**Live site:** https://namkurd.github.io/SleeperAuction/

The page title uses the league's current name on Sleeper ("DTF Club" today). The update run saves it, and the open page also asks Sleeper for it, so a rename shows up without any edit.

When a new season's Sleeper auction draft is marked complete, a GitHub Action picks it up, rebuilds the data and republishes the site. Nobody has to touch anything.

## Setup (about 5 minutes, no command line)

1. **Create the repository.** On github.com click **New repository**, name it `SleeperAuction`, choose **Public**, leave everything else empty, and click **Create repository**.
2. **Upload the files.** Unzip the download. In the new repo click **uploading an existing file** (or **Add file > Upload files**), drag in everything from the unzipped folder, and click **Commit changes**.
3. **Turn on GitHub Pages.** In the repo open **Settings > Pages**. Under **Build and deployment**, set **Source** to **GitHub Actions**.
4. **Add the automation (copy and paste).** Open the **Actions** tab and click **set up a workflow yourself**. At the top, change the file name from `main.yml` to `update.yml`. Select everything in the editor and delete it. Open `workflow-to-paste.txt` from the unzipped folder, copy all of it, paste it into the editor, then click **Commit changes** and confirm.
5. **Watch it build.** Stay on the **Actions** tab. A run called "Update auction data and publish site" starts by itself and takes a minute or two. When it turns green the site is live at the address at the top of this page. If it fails on a Pages error, you skipped step 3: do it, then open the failed run and click **Re-run failed jobs**.

Why step 4 is a paste: GitHub only runs automations that live in a hidden folder (`.github/workflows`), and dragging hidden folders into the browser upload is unreliable. Pasting into GitHub's own editor creates the folder correctly. After it works you can delete `workflow-to-paste.txt` from the repo.

Prefer a terminal? `mkdir -p .github/workflows && cp workflow-to-paste.txt .github/workflows/update.yml`, then `git init -b main && git add . && git commit -m "Initial import"`, `gh repo create namkurd/SleeperAuction --public --source=. --remote=origin`, `gh api -X POST repos/namkurd/SleeperAuction/pages -f build_type=workflow`, `git push -u origin main`.

## What's in the dashboard

| Tab | What it shows |
|---|---|
| League | Average dollars per team per position per season, average dollars per team per depth slot (QB1-3, RB1-3, WR1-3, TE1-2, K1), and a dot for every pick ever made. Dashed gold lines mark scoring-rule changes. Below the charts, two tables count the players each manager has bought from every NFL team and every college (defenses excluded); hover or tap a number for the players and years. |
| Managers | Pick any current or former manager: the same two charts for just that person, real year gaps when they sat out, optional league-average overlay. Hover a chart point to see which players make up that value. Above each year on both charts is the team's regular-season finish in the league standings, with a gold, silver or bronze medal above it for the champion, 2nd place and 3rd place; hover it for the record, PF+ (points for indexed to that season's league average, 100 = average) and the playoff finish. Two tables list the NFL teams and the colleges their drafted players came from (defenses excluded); hover a count to see the players and years. |
| Drafts | Every draft board, newest first, in the style of the old `Auctions` sheet: a column per manager, a row per lineup slot (QB, RB, RB, WR, WR, TE, FLEX, SFLEX, D/ST, K, bench), player and price in each cell. Every board starts expanded, with each year's top QB, RB, WR and TE bids in its banner. Jump to a season, search a player or manager to highlight every match, or switch to full player names. |
| Players | Type a name and pick from the drop-down (position, number of auctions and best price are shown for each match) to see what that player cost each year, who bought them, and their depth slot. |

Every chart has a data table under it. The League charts divide each season's league total by that season's number of teams (10 through 2025, 12 in 2026), so seasons of different sizes compare fairly; the Managers charts and the price-per-pick dots are raw dollars. The site is built to fit a phone held sideways without side-to-side scrolling.

## How it works

```
Sleeper API ─┐
             ├─> scripts/update_data.py ─> data/picks.csv ─> site/data.json + data.js ─> GitHub Pages
spreadsheet ─┘   (2012-2020 frozen in data/history_2012_2020.csv)
```

- **2012-2020** come from the old spreadsheet, cleaned once and frozen in `data/history_2012_2020.csv` (co-owners folded into one manager, misspelled and nicknamed players matched to real players).
- **2021 onward** come from Sleeper. Its prices match the spreadsheet apart from two $1 discrepancies in 2021, and its positions are right where the spreadsheet has a few mislabels (a TE typed as WR, a kicker typed as DEF). Sleeper also has real player ids and cleaner names, so it wins wherever the two differ. `data/reconcile_report.md` lists every difference so you can look.
- **Depth is strictly by price.** Within a manager, season and position the highest-priced player is 1, the next is 2, and so on (ties go to the earlier pick). The old charts used the spreadsheet's lineup order, so a few depth numbers moved slightly.
- **Draft boards** are laid out by price, the same rule for every team and year: the priciest QB takes QB, the 2nd QB takes SFLEX (if a team has no 2nd QB, SFLEX takes its priciest RB, WR or TE), the two priciest RBs and two priciest WRs take those slots, the next-priciest RB or WR takes FLEX, then the priciest TE, K and D/ST, and everyone else sits on the bench, priciest first. Which slots exist each year is in `config/lineups.json`. Managers appear in the spreadsheet's left-to-right order (`data/board_order.json`); new seasons reuse last season's order with newcomers at the end.
- **NFL team and college** for each pick: 2021 onward the team is stored on the Sleeper pick and the college comes from Sleeper's player database. For 2012-2020 the team is that season's roster from the public [nflverse](https://github.com/nflverse/nflverse-data) data (a player traded mid-season can show either team). About 25 retired players missing from both sources were filled in by hand in `config/player_extras.json`, from general knowledge, so check those if a college looks off.
- **Standings** are the league's own Rumbles ranking: each regular-season week a team earns 1 point per team it outscored plus 9 for winning its matchup (a perfect week is teams + 8), ranked by total, then points for. 2013-2020 are copied from the workbook's `Lifetime` tab (`data/history_standings.csv`). From 2021 the site works them out from Sleeper's weekly scores, which reproduces the sheet's Rumble R for every team in 2021-2025, and caches them in `data/sleeper_standings.csv` once a season is complete (so the current season appears when its regular season ends). The podium (champion, runner-up, third place) through 2025 is the league's own record, entered in `config/playoffs.json` (Sleeper's brackets are deliberately not used for those years); from 2026 it comes from Sleeper's playoff bracket, and a `config/playoffs.json` entry would still win over it. 2012 has no standings (the workbook has none).
- Finished Sleeper seasons are cached in `data/sleeper_picks.csv`, so the archive survives even if Sleeper someday deletes an old league.

### Files

| Path | Purpose |
|---|---|
| `scripts/update_data.py` | Fetches Sleeper, computes depth, writes every data file. Standard library only. |
| `scripts/check_data.py` | Sanity checks (budgets, gaps, duplicates). The Action stops before publishing if these fail. |
| `scripts/build_history.py` | One-time importer for the spreadsheet (names, board order). You will almost never need it again. |
| `scripts/build_player_meta.py` | One-time: NFL team per season and college for 2012-2020 players (downloads nflverse rosters). Writes `data/history_teams.csv` and `data/player_meta.csv`; `update_data.py` adds new players to the latter itself. |
| `scripts/build_standings.py` | One-time: imports the 2013-2020 finishes from the `Lifetime` tab of the old workbook into `data/history_standings.csv`. |
| `scripts/export_excel.py` | Optional: writes `DTF Auction - Updated.xlsx` (Drafting, Auctions, Player origins, Name fixes, Differences tabs) from the current data. Needs `pip install openpyxl`; not part of the Action. |
| `config/playoffs.json` | Champion, runner-up and third place for every season through 2025 (the league's own record). 2026 on is read from Sleeper's bracket. |
| `config/player_extras.json` | Hand-filled colleges and teams for players the data sources lack. |
| `config/managers.json` | Sleeper `user_id` to manager name, plus roster overrides. |
| `config/eras.json` | The dashed scoring-era lines. |
| `config/lineups.json` | The starting lineup of each era (which slots the Drafts boards show). |
| `config/aliases.json` | Co-owner folding and player-name fixes for the spreadsheet years. |
| `site/` | The static website (`index.html`, Plotly bundled in `vendor/`). |
| `.github/workflows/update.yml` | The automation: runs every 6 hours, on demand, and when you push. You create it in setup step 4 from `workflow-to-paste.txt`. |

## Everyday tasks

**A new manager joins.** Nothing breaks: they show up under their Sleeper display name and the run prints a warning. To give them a proper name, add their Sleeper `user_id` to `config/managers.json` (find it at `https://api.sleeper.app/v1/user/<username>`), then run the workflow.

**The starting lineup changes.** Add an entry to `config/lineups.json`, for example one more FLEX: `{ "from": 2027, "starters": ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "SFLEX", "D/ST", "K"] }`. FLEX takes RB/WR only (the `eligible` list in the same file). Until you add an entry, new seasons use the latest lineup listed and any extra players just show on the bench.

**Scoring rules change.** Add a line to `config/eras.json`, for example `{ "label": "Full PPR", "from": 2027 }`, and push. `from` is the first season the rule applies; the line is drawn between the previous season and that one. Use `<br>` in a label for a line break.

**A playoff result looks wrong or is missing.** Set the champion, runner-up and third place (names as shown on the site) for that year in `config/playoffs.json` and push; the Managers charts show the medals on the next run. Until a season's playoffs finish, 2026 onward shows the regular-season finish only.

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

- **2026 has 12 teams** ($2,398 spent vs about $2,000 before). The League charts and tables average per team so they stay comparable; the Managers charts show what one person actually paid, so their dollars are unaffected. Very narrow phones round the League tables to whole dollars; hover a chart point for one decimal.
- **The spreadsheet starts in 2012.** There is no 2010 or 2011 data in it, so the Drafts tab begins with 2012.
- **Sleeper records a player's position as of today.** If someone changes position later (a QB turned TE, say), Sleeper would show the new one. In the 2021-2026 seasons compared against the spreadsheet, every position difference turned out to be a spreadsheet mislabel, not a position change, and the reconcile report would flag a real one.
- **A few old spreadsheet names could not be matched** to a real player (retired players missing from Sleeper's database, joke nicknames). They keep their spreadsheet name. See `data/history_name_map.csv` for every match decision and fix any by adding to `config/aliases.json`, then re-running `scripts/build_history.py`.
- **Manager position charts include every player a manager bought**, bench depth included, so each season adds up to their budget. The older manager screenshots only counted their top three RBs and WRs.
- Spreadsheet-era player identity relies on name and era, so two different players who share a name and position in nearby years could be merged. The name map shows every guess.

Plotly.js (bundled in `site/vendor`) is MIT licensed.
