# TikTok Gap

Finds small-to-midsized marketing agencies that run Meta and/or YouTube for their
clients but have **not** moved into TikTok — then verifies the gap is real by
checking the agencies' actual clients for a TikTok pixel.

The pitch it's built to support: those agencies are already producing vertical
video for Reels and YouTube, so the creative is repurposable. *You already made
the assets.*

## Just want to run it?

Green **`< > Code`** button above → **Download ZIP** → unzip it → double-click
**`Start TikTok Gap.command`** (Mac) or **`Start TikTok Gap.bat`** (Windows) →
press **Start**.

Full walkthrough, including what to do if your Mac refuses to open it:
**[START HERE.md](START%20HERE.md)**.

This file is the reference for how it works. [RUNBOOK.md](RUNBOOK.md) is the
command-line version, if you'd rather drive it that way.

---

## The pipeline

`app.py` runs all of this for you in one long job (see
[START HERE.md](START%20HERE.md)). Each step is also its own script, runnable
against a single domain or a CSV, so you can spot-check one agency without
running the whole chain.

```
find_agencies.py       →  Agencies tab      candidate agencies, by vertical + metro
check_agency_tiktok.py →  Agencies tab      does the agency sell TikTok? with evidence
parse_clients.py       →  Clients tab       who are the agency's clients?
pixel_check.py         →  Ad Tags tab       does each client run a TikTok pixel?
score_agencies.py      →  Agencies tab      how many clients came back TikTok-free
feedback.py            →  Feedback tab      your corrections, fed back into the parser

app.py + pipeline.py   →  all of the above, as one resumable job with a UI
```

A full pass, from nothing to a ranked list:

```bash
# 0. Build the TikTok Marketing Partner suppression list (once, then cached)
python tiktok_partners.py --refresh

# 1. Find agencies
python find_agencies.py --vertical home_services --geo "Phoenix AZ" -o agencies.csv

# 2. Which of them already sell TikTok?
python check_agency_tiktok.py agencies.csv -o checked.csv --sheet

# 3. Pull the client lists off the ones that don't
python parse_clients.py checked.csv -o clients.csv --sheet

# 4. Pixel-check every client domain
python pixel_check.py clients.csv --column client_domain -o results.csv --sheet

# 5. Rank agencies by how many clients came back TikTok-free
python score_agencies.py --clients clients.csv --tags results.csv --agencies checked.csv
```

Step 5 is the point of the whole thing. It prints, and writes to
`agency_scorecard.csv`:

```
agency                             vert           qual free  tt  chk found  cov  mentions
--------------------------------------------------------------------------------------
Alpine Digital                     home_services     2    2   1    3     5  60%  no
Northbeam Media                    local_health      1    2   0    2     3  67%  no
```

`qual` is the number that matters: clients that are reachable, run Meta or
Google, and have no TikTok pixel anywhere. `cov` is what share of the found
clients we actually got a look at — a big number on thin coverage is visible as
thin rather than laundered into a score.

---

## Reading the three tabs

**Agencies** — one row per agency. `mentions_tiktok` is yes/no; `tiktok_evidence`
is the URL and the surrounding sentence for every hit, tagged by kind:

| kind | what it means |
|---|---|
| `pixel` | a TikTok pixel on the agency's own site. Strongest signal — they run TikTok whatever the copy says |
| `service_copy` | prose on a services page. The real signal |
| `social_link` | a link to their own tiktok.com profile. Close to meaningless on its own |
| `markup_only` | a class name or icon. Usually a theme shipping every social network |

A hit supported only by `social_link` or `markup_only` gets `WEAK:` in notes. An
agency with no TikTok mention that *does* mention Meta, YouTube or vertical video
gets `TARGET:` in notes. Those are the calls to make.

**Clients** — one row per client, with `confidence` set by extraction method:
`alt_text` and `outbound_link` are high, `image_filename` is medium,
`case_study_title` is low. Review anything below high.

**Ad Tags** — `pixel_check.py` output, one row per client domain. See below.

**Feedback** — where your corrections go. See below.

---

## The feedback loop

The extractors are heuristics, and heuristics are wrong in specific, repeatable
ways. Rather than re-notice the same bad row every week, you correct it once:

```
Clients tab  →  you add a verdict in the Feedback tab  →  feedback.py --learn
             →  data/learned_rules.json  →  the next parse_clients.py run
```

Five verdicts: `bad`, `good`, `rename`, `wrong_domain`, `missed`. A `bad` label
is never recorded again for that agency. A `rename` is applied everywhere. A
domain you supply is marked high confidence, because a fact you supplied beats
any heuristic.

```bash
python feedback.py --template --clients clients.csv -o review.csv   # rows to review
python feedback.py --learn --from-sheet                             # verdicts -> rules
python feedback.py --show-rules                                     # what it knows
python feedback.py --report --from-sheet                            # what's broken
```

**It tunes itself where it has evidence.** Once a method has 20+ reviewed
examples, its confidence rating is set from measured precision — a method you
keep marking wrong is demoted, one that keeps being right is promoted. Twenty is
a deliberate floor so one afternoon of review can't swing the pipeline.

Nothing here guesses: every rule traces to a row you wrote, and rules are exact
matches, never fuzzy.

### It reports on itself

`.github/workflows/health-check.yml` runs every Monday, folds in new verdicts,
and files a health report as a GitHub issue labelled `health-report` — updated in
place. It tells you which directories have gone quiet, and distinguishes *blocked
by Cloudflare* from *the page layout changed*, because those need different
fixes. If an extractor regresses, the issue leads with the failing test output.

Paste that issue's URL into a Claude Code session and it has what it needs to fix
the problem without going and looking first.

### Regression gate

`.github/workflows/tests.yml` runs on every push — no secrets, no network:

```bash
python tests/test_extractors.py     # the parse must not regress
python tests/test_feedback.py       # verdicts must change the parse
python tests/test_sheets_upsert.py  # upserts must not duplicate
```

---

## Honest limits on the client parse

Logo walls are images, many are lazy-loaded, and some are carousels a browser
builds at runtime. **Expect 50–60% recall on a static fetch.** Where a page is
clearly JavaScript-rendered and yields nothing, the agency is marked
`needs_manual_review` rather than recorded with zero clients — a false zero
silently removes a live prospect, which is worse than an admitted gap.

Client domains are resolved from evidence on the page, never invented. A client
whose domain can't be established keeps a blank domain. `--verify-guess` will try
`<slug>.com` and keep it **only** if the fetched page actually names the brand.

Check the parse before you scale it:

```bash
python parse_clients.py --selftest            # extractors vs. bundled fixtures
python parse_clients.py oneagency.com --show  # every hit, per page, per method
python parse_clients.py oneagency.com --dry-run   # parse, write nothing
```

---

## Scraping conduct

These are small businesses. Every fetch in the new scripts goes through one
`Fetcher` in `common.py`, which:

- reads and honours `robots.txt` (`--ignore-robots` exists and warns loudly)
- rate-limits to **one request per second per host**, adjustable with `--delay`
- sends a descriptive User-Agent naming the tool and a contact address
  (set `CRAWLER_CONTACT`, or pass `--user-agent`)
- **caches every fetched page to `.cache/`**, so a re-run re-hits nobody
- retries broken TLS unverified — we only read markup — and falls back to
  http once when https won't connect at all

A 403 is recorded as a failure, never as a zero. `find_agencies.py` writes
blocked and empty sources to `agency_source_failures.csv` for the same reason:
"no HVAC agencies in Tulsa" and "Clutch blocked us" are different facts.

---

## First, the thing that's confusing: where does this actually run?

GitHub **stores** code. It doesn't run it. Three separate things can run it:

| Where | What it is | When to use |
|---|---|---|
| **Your laptop** | You type a command in Terminal | Big sweeps, best data quality |
| **GitHub Actions** | A temporary computer GitHub rents you, for free | Click a button, no setup on your machine |
| **A schedule** | Same rented computer, fires itself weekly | Set once, forget |

They run the *same code* on the *same list* and write to the *same Sheet*.
The only difference is which computer does the work and how you kick it off.

**Everything writes to one place:** [your Google Sheet](https://docs.google.com/spreadsheets/d/1TpvjeWyDt4oGmc7bqtEaq8zf9pqWiPmzVULYxcwO3ow/edit).
Whichever way you run it, results show up in the **Ad Tags** tab.

---

## Setup — do this once

### 1. Push this folder to GitHub

Create a new **private** repo on github.com, then:

```bash
cd path/to/this/folder
git init
git add .
git commit -m "initial"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPO.git
git push -u origin main
```

Private matters — the workflow references your Sheet.

### 2. Make the Google service account

The script writes to Sheets as its own robot identity. It can't use your browser login.

1. [console.cloud.google.com](https://console.cloud.google.com) → create a project
2. **APIs & Services → Library** → enable **Google Sheets API**
3. **Credentials → Create Credentials → Service account** → name it `pixel-checker` → skip the optional steps
4. Click it → **Keys → Add Key → Create new key → JSON** → downloads a file

### 3. Share the Sheet with the robot

Open that JSON file. Find `client_email` — looks like
`pixel-checker@your-project.iam.gserviceaccount.com`.

Open [the Sheet](https://docs.google.com/spreadsheets/d/1TpvjeWyDt4oGmc7bqtEaq8zf9pqWiPmzVULYxcwO3ow/edit)
→ **Share** → paste that address → **Editor** → Send.

> Skipping this is the #1 cause of failures. You'll get a 403 and the script will
> print the exact address to share with.

### 4. Give GitHub the credentials

In your repo on github.com:

**Settings → Secrets and variables → Actions**

On the **Secrets** tab → **New repository secret**:
- Name: `GCP_SERVICE_ACCOUNT_JSON`
- Value: open the JSON file in a text editor, copy **the entire contents**, paste

On the **Variables** tab → **New repository variable**:
- Name: `SHEET_ID`
- Value: `1TpvjeWyDt4oGmc7bqtEaq8zf9pqWiPmzVULYxcwO3ow`

Setup done.

---

## Running it — Option A: the button (easiest)

1. Go to your repo on github.com
2. Click the **Actions** tab (top of the page)
3. Click **Pixel Check** in the left sidebar
4. Click the **Run workflow** dropdown on the right
5. Paste your domains into the box, one per line:
   ```
   blockrenovation.com
   mesagaragedoors.com
   ```
6. Optionally type the agency name
7. Click the green **Run workflow** button

A yellow dot appears, turns green in a minute or two. Click into the run to watch
the logs live. Results are in your Sheet when it goes green.

**That's it.** Nothing installed on your machine.

If you leave the domains box blank, it uses `data/clients.csv` from the repo instead —
useful once you have a long standing list. Edit that file on github.com directly
(click the file → pencil icon → commit).

---

## Running it — Option B: your laptop

Better data quality: home internet doesn't get blocked by Cloudflare the way
datacenter IPs do. Use this for full sweeps.

```bash
pip install -r requirements.txt
```

Put the JSON key from step 2 next to the scripts, named `service_account.json`.
(It's gitignored, so it won't get committed.)

```bash
python pixel_check.py data/clients.csv -o results.csv --sheet
```

With an agency tag:

```bash
python pixel_check.py data/clients.csv -o results.csv --sheet --agency "Blue Corona"
```

Check your connection works first:

```bash
python sheets.py
```

Everything else runs the same way, and every script writes its CSV **before** it
touches Sheets — so a Sheets failure never costs you the run:

```bash
python find_agencies.py --vertical local_health --geo "Denver CO" -o agencies.csv
python check_agency_tiktok.py agencies.csv -o checked.csv --sheet
python parse_clients.py checked.csv -o clients.csv --sheet
python score_agencies.py --clients clients.csv --tags results.csv
```

Spot-check a single agency at any stage — every script takes a bare domain:

```bash
python check_agency_tiktok.py bluecorona.com --show
python parse_clients.py bluecorona.com --show --dry-run
```

---

## Running it — Option C: on a schedule

Already configured. It fires Mondays at 09:00 UTC against `data/clients.csv`.

To change the timing, edit the `cron:` line in `.github/workflows/pixel-check.yml`.
To turn it off, delete those two lines. GitHub cron is UTC only.

---

## Reading results

**Ad Tags** tab. The column that matters is `qualifies`:

- **yes** — reachable, no TikTok anywhere, Meta or Google present. Your target.
- **no** — has TikTok, or unreachable, or no ad platforms found

Check `status` before trusting a `no`. `unreachable` means the site blocked us,
not that the brand is disqualified.

The `*_evidence` columns name which signature fired. Before building outreach on a
brand that reads clean, look at the evidence.

Re-running a domain **updates** its row rather than duplicating. Safe to re-run.

---

## Known limits

**Server-side tracking is invisible.** TikTok Events API fires from the brand's
backend. Those brands read as clean here and no client-side tool can see them.
MediaRadar is the arbiter.

**Datacenter IPs get blocked.** GitHub Actions runs from cloud ranges that
Cloudflare challenges. Expect more `unreachable` than running locally. The script
never turns a blocked fetch into a false `qualifies=yes` — but you do lose coverage.

**Pixel presence is not spend.** A pixel proves setup, not an active campaign.
Absence is the higher-confidence direction, which is what we're using.

**The TikTok Partner suppression list is a weak filter.** The directory lists
roughly 500 companies globally and almost no small agency is badged. It removes a
few obvious wrong answers and nothing else — treat it as suppression, never as
signal. An agency absent from it has told you nothing. `tiktok_partners.py`
refuses to write an empty cache for exactly this reason: a suppression list that
silently matches nothing is worse than none at all.

**Directory scraping gets challenged.** Clutch, DesignRush and friends sit behind
Cloudflare and will turn away a scripted request, especially from a datacenter
IP. When that happens it is recorded as `bot_challenge` in
`agency_source_failures.csv`, not as an empty result. Save the listing page from
your own browser and parse it instead:

```bash
python find_agencies.py --vertical home_services --from-html 'saved/clutch-*.html'
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Run fails instantly, "Secret not set" | Step 4 — add `GCP_SERVICE_ACCOUNT_JSON` |
| `403 PERMISSION_DENIED` | Step 3 — share the Sheet with the robot email |
| `Credentials file not found` (local) | `service_account.json` isn't beside the script |
| `APIError: 429` | Sheets quota. Lower `--workers` |
| Lots of `unreachable` | Run locally instead, or add `--delay 1` |
| No **Actions** tab | Repo Settings → Actions → General → enable |
| `suppression list ... is empty or missing` | `python tiktok_partners.py --refresh`, or `--no-suppression` |
| `bot_challenge` in the failures CSV | The directory blocked us. Save the page and use `--from-html` |
| Agencies come back `needs_manual_review` | The client page is JS-rendered. Open it by hand — this is an admitted gap, not a zero |
| Clients look wrong | `--show` prints every hit by method; raise `--min-confidence high` |
| Re-runs are slow | They shouldn't be — check `.cache/` exists and you didn't pass `--no-cache` |
