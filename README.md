# TikTok Gap — Pixel Checker

Finds brands running Meta or Google ads with **no TikTok pixel anywhere**.
Results land in a Google Sheet.

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
