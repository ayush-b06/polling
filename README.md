# jobwatch

Polls companies' **own** ATS feeds on independent source-specific schedules and
pushes an alert the moment a matching role appears. A slow browser or Workday
source no longer delays lightweight Greenhouse, Lever, or Ashby checks.

## What it actually does

Official-source adapters cover **Greenhouse, Lever, Ashby, SmartRecruiters,
Workday, Workable, Recruitee, Rippling, Eightfold, Oracle HCM, Microsoft,
Amazon, IBM, Vanguard**, and
several custom/browser-only career systems.

Simplify is a discovery and safety fallback, not the preferred alert source.
When one of its matching URLs reveals a supported official ATS board, jobwatch
persists and polls that board directly. The first official snapshot is seeded
without duplicate alerts; Simplify is suppressed for that company only after
the official source is healthy, non-empty, and reproduces a qualifying match.

Dedupe is by a stable hash of `(platform, board, job_id)`. Runtime state lives
in `jobwatch.db` (SQLite), including per-source health and a durable Discord
outbox. A temporary ATS failure does not remove its jobs, and a Discord failure
is retried until the webhook confirms delivery.

The dashboard deliberately shows two clocks: when the company says it posted
the role and when this poller first detected it. Detection time is the number to
optimize when measuring alert latency.

## Setup (5 minutes)

```bash
pip install -r requirements.txt

# 1. Verify every configured board resolves. Fix anything marked BROKEN.
python check.py

# 2. Record what's already open so you don't get 400 alerts on first run.
python main.py --seed

# 3. Go.
python main.py
```

On the first run, the existing `state.json` is imported automatically. Keep the
SQLite database on persistent storage in production. `state.json` remains a
compatibility export for the ephemeral GitHub Actions fallback.

### Telegram alerts

1. Message `@BotFather` → `/newbot` → copy the token.
2. Message your new bot once, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.
3. Export both:

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
```

Discord instead: put `DISCORD_WEBHOOK=https://discord.com/api/webhooks/...` in
the git-ignored `.env.local` file. Process environment variables take priority.
Set both Discord and Telegram credentials and you get both.

## Deploying it so it actually runs 24/7

Running on your laptop means it stops when the lid closes. Pick one:

| Option | Real latency | Notes |
|---|---|---|
| **Always-on container / small VM** | poll interval + fetch time | Use a persistent volume for `jobwatch.db`. |
| **Raspberry Pi / spare machine** | ~90s | Free, fully under your control. |
| **GitHub Actions** (`--once` on cron) | **5–20 min** | Included as a fallback, but cron there is throttled and unreliable under load. Don't rely on it as primary. |

The included workflow is in `.github/workflows/poll.yml` — commit `seen.json`
back so state survives between runs.

## Tuning

Everything lives in `config.yaml`:

- **`sources`** — add a company by adding one line. For Greenhouse, the token is
  the slug in `job-boards.greenhouse.io/<token>/jobs`. Same idea for Lever and
  Ashby. Run `check.py` after every edit.
- **Classification allows exactly four categories:** SWE internship, quant
  developer internship, SWE new-grad full-time, and quant developer new-grad
  full-time. Generic titles require trusted cohort or description evidence;
  data, ML research, SRE, DevOps, and quant research/trading roles are rejected.
- **Location is still strict:** a posting needs positive US evidence, and
  explicit foreign evidence always loses.
- **`poll_intervals`** controls independent cadences: direct APIs default to
  60s, Workday/custom sources to 120s, browser scrapers to 180s, and Simplify's
  delayed fallback to 900s. Any source can set its own `interval_seconds`.
- **Automatic direct promotion** recognizes Greenhouse, Lever, Ashby, Workable,
  Workday, SmartRecruiters, Rippling, and Eightfold application URLs. Generated
  source configurations are persisted in SQLite and resume after restarts.
- The self-contained dashboard reloads every 60 seconds. Its file is rewritten
  within roughly five seconds of a completed source poll.

Filters are regex and are tested — see the cases in `check.py` output. If a
posting you wanted got dropped, add its title to your own test case first, then
adjust.

## Limitations — read these

- **Coverage is your configured list plus promoted fallback discoveries.** A
  company with no configured source, no recognizable official ATS URL, and no
  Simplify listing is still invisible.
- **Workday is the weak link.** Its search is fuzzy, it doesn't return real posting
  dates ("Posted Today"), and each tenant needs its `tenant`/`site`/`host` triple
  found by hand from the careers URL. Verify with `check.py`.
- **Some companies have no stable public feed.** Those use targeted browser
  checks where available and otherwise remain on fallback. They cannot carry
  the same latency guarantee as official APIs.
- **Adding a Workday company** (most large non-tech employers — CVS, Walgreens,
  Boeing, Target): open their careers page, copy the URL, and run
  `python discover.py --workday <url>`. It prints the config line.
- **Rate limits are real.** More sources at a shorter interval means more failed
  fetches. The poller retries 3× with backoff, records failures in SQLite, and
  exposes degraded sources on the dashboard. Jobs from a failed source remain
  visible until a successful snapshot proves they closed.
- **Boards can go stale.** A company occasionally posts to their site before the
  API reflects it. Rare, but it means "zero latency" isn't a promise anyone can
  make — including this.
