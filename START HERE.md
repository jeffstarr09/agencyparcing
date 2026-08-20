# Start here

## 1. Get the files onto your computer

1. Go to **<https://github.com/jeffstarr09/agencyparcing>**
2. Click the green **`< > Code`** button near the top right
3. Click **Download ZIP**
4. Open your Downloads folder and **double-click the ZIP** to unpack it

You'll get a folder called something like
`agencyparcing-claude-agency-discovery-tiktok-gap-st3gx4`. Drag it somewhere
you'll find again — your Desktop is fine. Everything the app makes gets saved
inside that folder.

> Prefer a direct link? [Download the ZIP](https://github.com/jeffstarr09/agencyparcing/archive/refs/heads/claude/agency-discovery-tiktok-gap-st3gx4.zip)
> — you'll need to be signed in to GitHub if the repo is private.

## 2. Run it

**On a Mac** — double-click **`Start TikTok Gap.command`**

**On Windows** — double-click **`Start TikTok Gap.bat`**

That's it. A black window opens, then your browser opens. Press **Start**.

The first time takes a minute while it sets itself up. After that it's a few
seconds. Leave the black window open while you use it — closing it quits the app.

### Mac: "Apple could not verify it is free of malware"

**Expected, and it happens to every Mac.** It is not a virus warning. macOS
blocks *any* program downloaded from the internet that hasn't been through
Apple's paid developer signing, and this is a plain text file you can read.

Click **Done** on that box, then:

1. Open **System Settings**
2. Go to **Privacy & Security**
3. Scroll down to the **Security** section. You'll see
   *"Start TikTok Gap.command" was blocked to protect your Mac.*
4. Click **Open Anyway**
5. Enter your Mac password or Touch ID
6. Double-click **`Start TikTok Gap.command`** again — this time click **Open**

You only do this once. After that it starts normally every time.

> On older Macs (macOS 14 and earlier) right-clicking the file → **Open** →
> **Open** does the same thing in one step. Apple removed that shortcut in
> macOS 15 Sequoia, which is why the setting above is now the way in.

**If "Open Anyway" isn't there**, use Terminal instead — it sidesteps the check
entirely, and you don't have to type any file paths:

1. Open **Terminal** (press ⌘+Space, type `Terminal`, press Return)
2. Type `bash ` — the word bash, then a space. Don't press Return yet.
3. **Drag `Start TikTok Gap.command` from your folder into the Terminal window.**
   It fills in the path for you.
4. Press **Return**

That starts the app the same way. To run it again later, press the Up arrow in
Terminal and hit Return.

### Other things that can go wrong

**Windows: "Windows protected your PC"** — same idea as the Mac warning. Click
**More info** → **Run anyway**.

**Mac: "you do not have permission to execute"** — the download stripped the
file's permission. Use the Terminal method above; `bash` doesn't need it.

**Nothing happens at all** — you probably need Python. Get it from
[python.org/downloads](https://www.python.org/downloads/), install it, then
try again. On Windows, tick **"Add Python to PATH"** during the install.

---

## 3. What you'll see

Five tabs across the top.

### Run

Press **Start** and leave it. It works through 25 metro areas looking for
agencies, and for each one it finds it will:

1. read their services pages to see if they already sell TikTok
2. pull their client list off their website
3. check each of those clients for a TikTok pixel

The log shows what it's doing in plain language. Press **Stop** whenever —
everything found so far is already saved, and pressing Start again picks up
where it left off rather than starting over.

Tick **"Keep going until I press Stop"** for a long run. It'll work through
everything, then go round again looking for more.

**It is deliberately slow.** It waits a second between requests to any one
website. These are small businesses and hammering them would be rude, and would
get us blocked. A long run is the intended way to use this.

### Results

The list, sorted by the number that matters.

**No TikTok** is the column to read: how many of that agency's clients are
running Meta or Google ads and have no TikTok pixel at all. An agency with eight
is a much better call than one with one.

**Coverage** is how many of the clients we found we could actually check. A big
number on low coverage is a thin read — worth knowing before you pick up the
phone.

**Open results folder** gives you the raw spreadsheets, which open in Excel,
Numbers, or Google Sheets.

### Review

This is where it gets better over time.

It shows the client names it's least sure about. Four buttons: **Correct**,
**Not a client**, **Wrong name**, **Wrong website**. Click one and it takes
effect immediately and permanently — it will not make that mistake again, for
that agency or any other.

You don't have to get through them all. Only the ones it got wrong actually
teach it anything. Five minutes here is worth more than an hour anywhere else.

### Problems

The honest column. Sites that blocked us, client lists built with JavaScript
that we couldn't read, and search sources that have stopped working.

**Nothing in here is counted as a zero.** A site we couldn't read is recorded as
unread, not as "no TikTok" and not as "no clients". A blank means unknown. That
distinction is the whole point — a false zero would quietly drop a live prospect
off your list.

### Setup

Everything here is optional. You can press Start without touching any of it.

---

## Do I need Google Sheets?

**No.** Results are always saved as `.csv` spreadsheet files right next to the
app. Double-click one and it opens in Excel or Numbers.

Set up Google Sheets only if you want rows written into a shared Sheet
automatically — the Setup tab has the steps if you do.

---

## What it's actually looking for

Agencies that run Meta and YouTube ads for their clients but have never moved
into TikTok. They're already making vertical video for Reels and YouTube, so the
creative is repurposable. The pitch is *you already made the assets*.

It's after small and midsized shops — roughly 5 to 100 people, one or a few
offices — in home services, local health, and DTC ecommerce. Not holding
companies, not global networks, not one-person freelancers.

---

## Honest limits

**It'll find roughly half to two-thirds of an agency's clients, not all of
them.** Logo walls are images, and some are drawn by JavaScript that a plain
read can't see. Where that happens the agency shows up under Problems rather
than with a wrong number next to it.

**A pixel proves setup, not spend.** A client with no TikTok pixel is very
likely not running TikTok. A client *with* one might have installed it and never
launched. Absence is the more reliable direction, which is the direction this
uses.

**Some tracking is invisible to any tool like this.** If a brand fires TikTok
events from their own server rather than the browser, nothing that reads a
webpage can see it. Those will read as clean here.

**Directory sites block us.** Clutch and the rest sit behind bot protection and
will refuse. That shows up under Problems as blocked, never as "no agencies
here". Web search is the source that reliably works from a laptop.

---

## If something goes wrong

| What you see | What to do |
|---|---|
| Browser didn't open | Copy the `http://127.0.0.1:8765/` address from the black window into your browser |
| "Lost contact with the app" | The black window closed. Double-click the launcher again |
| Lots of "couldn't read" | Normal from some networks. It keeps going and records them honestly |
| Nothing found after a while | Check the Problems tab — a search source may be blocked |
| Want to start fresh | Setup tab → **Forget progress**. Results and learning are kept |

Everything the app does can also be done from the command line — see
[RUNBOOK.md](RUNBOOK.md) for that, and [README.md](README.md) for how it all
works. You don't need either to use the app.
