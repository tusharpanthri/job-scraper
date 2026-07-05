# Workday Watcher — GitHub Actions Setup

## Files
- `workday_last3days_watcher.py` — the scraper. Keep at repo root.
- `.github/workflows/workday-watcher.yml` — hourly runner.
- `data/` — created automatically. This is your persistence layer AND your output.
  - `jobs_db.json` — the database. Do not delete this or you lose all "NEW!" history.
  - `openings_MMDDYYYY.md` — one file per EST calendar day, regenerated every run.
  - `priority.md` / `priority.csv` — today/yesterday postings only.
  - `last3days.csv` / `raw_scraped_jobs.csv` — full rolling 3-day window.
  - `last_run.json` — run metadata (counts, timestamp).
  - `scrape_errors.jsonl` — present only if a board errored this run.

## Setup
1. Put `workday_last3days_watcher.py` at the root of your repo.
2. Put the workflow file at `.github/workflows/workday-watcher.yml`.
3. Commit and push. Go to the Actions tab, select "workday-watcher", and click
   **Run workflow** to trigger a manual test — don't wait for the hourly cron.
4. Check the run logs. Then check `data/scrape_errors.jsonl` in the resulting commit.

## Before you rely on this: test for blocking

GitHub's shared runners use well-known IP ranges. Some Workday tenants sit behind
bot protection that's more aggressive toward datacenter IPs than toward your home
connection. Run the manual test (step 3) and actually read the error log. If you
see a wall of HTTP 403s across most boards, the hourly schedule won't fix that —
you'd need a self-hosted runner (i.e., your own machine staying on) or a paid
proxy. There's no free way around IP-based blocking if it's happening.

## How "NEW!" and daily files actually work

- `jobs_db.json` is committed back to the repo after every run, so it persists
  across otherwise-stateless runner instances.
- Every run compares freshly scraped postings against `jobs_db.json`. A posting
  not already in the DB is "new" for that run and gets tagged.
- `openings_MMDDYYYY.md` is not appended to — it's fully rebuilt each run from
  the DB, filtered to jobs whose `first_seen` date (converted to America/New_York)
  matches today's date. This means:
  - No duplicate rows, ever.
  - When EST midnight passes, the next hourly run naturally starts writing to a
    new filename — no separate rollover logic needed.
  - A job discovered at 9am shows (NEW!) in the 9am run's file version, and
    still appears (without the tag) in every later-that-day version, since
    it's still part of "today's" postings.

## Known limitations, stated plainly

- **Scheduled workflows are not exact.** GitHub documents delays during high
  load, sometimes 10-20+ minutes. If you need hourly precision, this isn't it.
- **Public repo = unlimited free Actions minutes. Private repo = 2,000 min/month
  free.** Confirm which applies to you.
- **Hourly commits add up.** Not a functional issue, just repo noise. Squash
  history periodically if that bothers you.
- **Timezone bug class this avoids:** all timestamps are stored and compared as
  EST/EDT-aware (`America/New_York`), not runner-local UTC. If you ever add more
  date logic to this script, keep using `now_est()` — don't reintroduce naive
  `datetime.now()`.
