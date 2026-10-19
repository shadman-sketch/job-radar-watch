# Job Radar Watch

Checks about 1,500 employers' career pages every morning for new IT support, help desk, service desk, desktop support, IT technician and security technician jobs in Dallas–Fort Worth or fully remote in the US.

## How it works

1. `checker/discover.py` works out which job system each company uses (Greenhouse, Lever, Ashby, Workday, Oracle, iCIMS, SmartRecruiters, Workable, Jobvite, UKG, ADP, Phenom, Eightfold…). Companies with no detectable system fall back to a page check in a headless browser.
2. `checker/run.py` pulls each company's openings, keeps titles and locations that fit, and reports only jobs it hasn't reported before (and only those posted in the last 3 days when the post date is known).
3. The GitHub Actions workflow runs both at 10:15 UTC daily and commits the results.
4. A Claude scheduled task reads `results/latest.json` each weekday morning, scores the jobs against the resume, and adds them to the Morning Job Radar dashboard.

## Files

| Path | What it is |
|---|---|
| `data/companies.csv` | The watchlist. Add a row to watch another company. |
| `data/resolved.json` | Which job system each company uses. |
| `results/latest.json` | New matches from the last run. |
| `results/summary.md` | Readable summary of the last run. |
| `results/status.json` | Per-company health: system, jobs seen, errors. |
| `results/seen.json` | Everything already reported, so nothing repeats. |

## Run by hand

Actions tab → **Daily watchlist check** → **Run workflow**. Tick "Re-detect the job system for every company" after a big change to the list.
