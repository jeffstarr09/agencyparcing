# Start here

## Run it

**On a Mac** — double-click **`Start TikTok Gap.command`**

**On Windows** — double-click **`Start TikTok Gap.bat`**

That's it. A black window opens, then your browser opens. Press **Start**.

The first time takes a minute while it sets itself up. After that it's a few
seconds. Leave the black window open while you use it — closing it quits the app.

> If your Mac says *"cannot be opened because it is from an unidentified
> developer"*: right-click the file → **Open** → **Open**. You only have to do
> that once.
>
> If nothing happens at all, you probably need Python. Get it from
> [python.org/downloads](https://www.python.org/downloads/), install it, and
> double-click again. On Windows, tick **"Add Python to PATH"** during install.

---

## What you'll see

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
