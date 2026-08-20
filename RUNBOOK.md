# Running it from the command line

**You probably don't need this page.** Double-click
`Start TikTok Gap.command` (Mac) or `Start TikTok Gap.bat` (Windows) and press
Start — see [START HERE.md](START%20HERE.md).

This page is for driving the steps individually: spot-checking one agency,
scripting a run, or working out why a step behaved the way it did.

---

## Once, before anything

### 1. Get the code and the dependencies

```bash
git clone https://github.com/jeffstarr09/agencyparcing.git
cd agencyparcing
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

From here on, every `python` command assumes that venv is active. If you close
the terminal, run `source .venv/bin/activate` again.

### 2. Google credentials

Follow README steps 2 and 3 — make the service account, download the JSON, save
it beside the scripts as `service_account.json`, and **share the Sheet with the
robot's email address as an Editor**. Skipping the share is the number one cause
of failures.

Then check it:

```bash
python sheets.py
```

You want to see your sheet's name, its URL, and a line per tab. First run also
creates the two new tabs (`Feedback`) and adds the scorecard columns to
`Agencies`. If you get `403 PERMISSION_DENIED`, you didn't share the Sheet — the
error prints the exact address to share with.

### 3. Build the suppression list

```bash
python tiktok_partners.py --refresh
```

This scrapes the TikTok Marketing Partner directory so badged agencies get
dropped from your candidate list.

**It will probably fail**, and that's expected — the directory is a JavaScript
app and TikTok moves its endpoints. When it does, it tells you exactly what to do:
open <https://partners.tiktok.com/directory> in your browser, save the page
(Cmd/Ctrl+S) or copy the JSON from the Network tab, then:

```bash
python tiktok_partners.py --from-file ~/Downloads/directory.html
```

It refuses to save an empty list, on purpose. A suppression list that silently
matches nothing is worse than no list at all.

Verify it took:

```bash
python tiktok_partners.py                    # shows what's cached
python tiktok_partners.py --check acme.com   # test one domain
```

> Remember what this filter is worth: ~500 companies globally, and almost no
> small agency is badged. It removes a few obvious wrong answers. An agency
> *absent* from this list has told you nothing.

---

## Step 1 — Find agencies

```bash
python find_agencies.py --vertical home_services --geo "Phoenix AZ" -o agencies.csv
```

Verticals are `home_services`, `local_health`, `dtc`. Sweep more ground with
`--all-metros` (25 metros) or `--geo-file data/metros.txt`, and add `--sheet` to
push to the Agencies tab.

**Two things will happen and both are normal:**

Clutch, DesignRush and the rest sit behind Cloudflare and will refuse a scripted
request. That gets recorded in `agency_source_failures.csv` as `bot_challenge` —
*not* as "found nothing". Check that file. When a directory is blocking you, save
the listing page from your own browser and parse it instead:

```bash
python find_agencies.py --vertical home_services --from-html 'saved/clutch-*.html' -o agencies.csv
```

Search discovery uses DuckDuckGo (no API key needed) and is the most reliable
source from a laptop:

```bash
python find_agencies.py --vertical home_services --geo "Phoenix AZ" --source search
```

Already have a list? Skip discovery:

```bash
python find_agencies.py --seed my_agencies.csv --vertical home_services -o agencies.csv
```

---

## Step 2 — Which agencies already sell TikTok?

```bash
python check_agency_tiktok.py agencies.csv -o checked.csv --sheet
```

Reads each agency's services pages and records **where** it found TikTok, not just
whether. Check one agency and read the evidence yourself:

```bash
python check_agency_tiktok.py bluecorona.com --show
```

In the `notes` column, two markers matter:

- **`TARGET:`** — no TikTok mention, but they do sell Meta / YouTube / vertical
  video. This is your list.
- **`WEAK:`** — the only TikTok "evidence" is a footer social icon or a stray CSS
  class. Reads as `mentions_tiktok=yes`, but it's noise. Treat these as targets too.

A blank `mentions_tiktok` means the site blocked us. That is **not** a clean no.

---

## Step 3 — Who are their clients?

**Spot-check one agency before running the batch.** This is the step most likely
to need tuning for the sites you're actually hitting:

```bash
python parse_clients.py bluecorona.com --show --dry-run
```

`--show` prints every hit, per page, per method. `--dry-run` writes nothing. Look
at the output and decide whether you believe it. When you do:

```bash
python parse_clients.py checked.csv -o clients.csv --sheet
```

Expect 50–60% recall. Logo walls are images, many lazy-loaded, some drawn by
JavaScript. Agencies whose client page is JS-rendered come back as
`needs_manual_review` with a blank `clients_found` — those are prospects worth
opening by hand, not agencies with no clients.

Options worth knowing:

```bash
--min-confidence high    # drop everything below high
--verify-guess           # try <name>.com and keep it only if the page names the brand
```

---

## Step 4 — Check the clients for a TikTok pixel

```bash
python pixel_check.py clients.csv --column client_domain -o results.csv --sheet
```

Only clients with a resolved domain can be checked; the rest show up as
`unchecked` in the scorecard. Run from your laptop rather than GitHub Actions —
home IPs get challenged far less than datacenter ranges.

---

## Step 5 — Rank the agencies

```bash
python score_agencies.py --clients clients.csv --tags results.csv --agencies checked.csv --sheet
```

This is the answer:

```
agency                             vert           qual free  tt  chk found  cov  mentions
--------------------------------------------------------------------------------------
Alpine Digital                     home_services     8    8   0    8    11  73%  no
Northbeam Media                    local_health      1    2   0    2     3  67%  no
```

- **qual** — clients that are reachable, run Meta or Google, and have **no TikTok
  pixel**. Sort on this. Alpine, with eight, is the call.
- **cov** — how many found clients we actually got a look at. A big `qual` on low
  `cov` is a thin read, and you can see that it's thin.
- **tt** — clients that *do* have TikTok. Disqualifies the client, not the agency.

Sort the Agencies tab on `clients_qualifying`. Full detail, including which
client domains qualified, is in `agency_scorecard.csv`.

---

## Making it better: the feedback loop

The extractors are heuristics, and heuristics are wrong in specific, repeatable
ways. This is how you fix one permanently instead of re-noticing it every week.

### Review

Open the **Clients** tab. You don't have to review everything — only the rows you
disagree with carry information. For each, add a line to the **Feedback** tab:

| entity_type | entity_key | agency_domain | verdict | correct_value |
|---|---|---|---|---|
| client | Some Web Shop | alpinedigital.com | `bad` | |
| client | Canyon Plumbing Co | alpinedigital.com | `rename` | Canyon Plumbing Co. |
| client | Bright Smile Dental | alpinedigital.com | `wrong_domain` | brightsmileaz.com |
| client | Summit Roofing | alpinedigital.com | `good` | |
| client | | alpinedigital.com | `missed` | Ironwood Electric |

| verdict | means |
|---|---|
| `bad` | not a client. Never recorded again for that agency |
| `good` | confirmed. Counts toward that method's precision score |
| `rename` | right client, wrong name. Put the right one in `correct_value` |
| `wrong_domain` | right client, wrong domain. Right domain in `correct_value` |
| `missed` | a client we never found. Name in `correct_value` |

Prefer a spreadsheet-free start? Generate a pre-filled review file, sorted so the
least trustworthy rows are first:

```bash
python feedback.py --template --clients clients.csv -o review.csv --limit 50
```

Fill in the `verdict` column and use `--feedback review.csv` below.

### Teach it

```bash
python feedback.py --learn --from-sheet          # or: --feedback review.csv
```

It reports what it applied and what it rejected. A malformed verdict is refused
with a reason rather than silently ignored.

### Watch it take effect

The next parse applies your corrections automatically:

```bash
python parse_clients.py checked.csv -o clients.csv --sheet
#   applying your corrections: 3 junk labels, 1 junk domains, 12 per-agency rules, ...
```

Rows you called junk are gone. Names you fixed are fixed everywhere. Domains you
supplied are filled in and marked high confidence, because a fact you supplied
beats any heuristic.

**It also tunes itself.** Once a method has 20+ reviewed examples, its confidence
rating is set from its measured precision — a method you keep marking wrong gets
demoted, one that keeps being right gets promoted. Twenty is a deliberate floor;
below it, one bad afternoon of review would swing the whole pipeline.

See what it knows:

```bash
python feedback.py --show-rules
```

### What's broken right now

```bash
python feedback.py --report --from-sheet
```

Writes `feedback_report.md`: precision per method, which directories have gone
quiet, which agencies the static parse can't read, and the clients you said we
missed. It distinguishes *blocked* from *layout changed* — different problems,
different fixes.

**This runs itself.** `.github/workflows/health-check.yml` fires every Monday at
10:00 UTC, folds in any new verdicts, and files the report as a GitHub issue
labelled `health-report` — updated in place, so it reads as a running log. If an
extractor regresses, the issue leads with the failing test output.

That issue is the handoff. Paste its URL into a Claude Code session and it has
everything needed to fix the thing without going and looking first.

---

## The weekly loop

Once you're set up, this is the whole job:

```bash
source .venv/bin/activate

python find_agencies.py --vertical home_services --all-metros -o agencies.csv --sheet
python check_agency_tiktok.py agencies.csv -o checked.csv --sheet
python parse_clients.py checked.csv -o clients.csv --sheet
python pixel_check.py clients.csv --column client_domain -o results.csv --sheet
python score_agencies.py --clients clients.csv --tags results.csv --agencies checked.csv --sheet
```

Then sort the Agencies tab on `clients_qualifying`, work the top of the list, and
drop verdicts in the Feedback tab as you notice bad rows. Next Monday's run picks
them up on its own.

---

## When something goes wrong

| What you see | What it means |
|---|---|
| `Credentials file not found` | `service_account.json` isn't beside the scripts |
| `403 PERMISSION_DENIED` | Share the Sheet with the robot email in the error |
| `suppression list ... is empty` | Run `tiktok_partners.py --refresh`, or `--from-file` |
| `bot_challenge` in the failures CSV | A directory blocked us. Save the page, use `--from-html` |
| Lots of `unreachable` | Run locally, not in Actions. Try `--delay 2` |
| `needs_manual_review` | JS-rendered client page. Open it by hand — a real prospect |
| `Tab 'X' has an unexpected header row` | Someone edited the header. It says what it expected |
| Re-runs are slow | They shouldn't be. Check `.cache/` exists and you didn't pass `--no-cache` |
| A client keeps coming back wrong | Mark it `bad` in Feedback and run `feedback.py --learn` |

Every script takes `--help`, and every one runs against a single bare domain for
a quick look:

```bash
python parse_clients.py oneagency.com --show --dry-run
```

### Being a good citizen

Every fetch honours `robots.txt`, waits a second between requests to the same
host, identifies itself with a real User-Agent and a contact address, and caches
to `.cache/` so a re-run hits nobody. Set your own contact address:

```bash
export CRAWLER_CONTACT="you@yourdomain.com"
```

Please don't run with `--delay 0` against real sites, and treat `--ignore-robots`
as something you'd have to justify.
