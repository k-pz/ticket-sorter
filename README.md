# ticket-sorter

> Written with AI assistance (Claude Code), reviewed by a human. It runs
> against my own ticket PDFs and Google account, and the test suite passes —
> but read it before you point it at yours.

You bought a pile of festival tickets on [biletomat.pl](https://biletomat.pl),
for yourself and for other people, and now you have a folder of PDFs called
`bilety(3).pdf`. This sorts them out.

Point it at those PDFs and it reads each one (showtime, film, venue, whose name
it was issued to, reservation code) and hands back:

- one folder of tickets per person, files named after the film and the person;
- the ones you bought twice for the same showing, pulled aside as duplicates;
- `tickets.csv` and `duplicates.csv` with everything it parsed.

Optionally, it can also put all of that in Google Drive with a linked
spreadsheet, and give each person a Google Calendar of their showings with
their ticket attached to every event. It can just as well *read* the PDFs from
that same Drive folder, which is what lets the whole thing run from a GitHub
Action: you drop tickets into a folder, and everything else updates itself.
Those parts need a bit of Google setup and are described further down — the
basic sorting needs none of it.

Film runtimes (used for the end times and the calendar events) come from a
lookup table in `src/ticket_sorter/main.py`, taken off each film's page on
<https://octopusfilmfestival.com/program/> and keyed by title.

---

## 1. What you need

**uv**, a Python package manager. That's the only thing to install — it takes
care of Python itself (this needs 3.14, and uv will fetch it) and of the
libraries.

```sh
brew install uv                              # macOS
curl -LsSf https://astral.sh/uv/install.sh | sh   # macOS/Linux, without Homebrew
```

**Your ticket PDFs**, downloaded from biletomat.pl. Anything else that lands in
the folder (a receipt, say) is skipped with a note rather than mangled.

That's it for sorting. The Google features additionally need a Google account
and a free OAuth client — see [Google setup](#4-google-setup-once).

## 2. First run

```sh
git clone <this repo>
cd ticket-sorter

mkdir input                 # not in the repo: it holds real names and codes
cp ~/Downloads/bilety*.pdf input/

uv run ticket-sorter
```

The first `uv run` takes a minute while uv downloads Python and the libraries;
after that it starts instantly. You never activate a virtualenv or run `pip` —
`uv run` does that for you.

You should see something like:

```
wrote 104 tickets and 3 duplicates to /path/to/ticket-sorter/output
```

## 3. What you get

`output/` is wiped and rebuilt on every run, so don't put anything of your own
in it. `input/` is never touched (unless you ask for `--rename-input` below).

```
output/
├── tickets/
│   ├── jan-kowalski/
│   │   ├── motor-city-ABC123__jan-kowalski.pdf
│   │   └── zoolander-DEF456__jan-kowalski.pdf
│   └── anna-nowak/
│       └── vampyres-GHI789__anna-nowak.pdf
├── duplicates/
│   └── motor-city-JKL012.pdf
├── tickets.csv
└── duplicates.csv
```

A **duplicate** is a second ticket for a showing one person is already going
to — same person, film, time and venue. The first ticket is the one that
counts; the rest go to `duplicates/`. (You can also mark a ticket as one by
hand; see [Handing a showing back](#handing-a-showing-back).)

Both CSVs carry the same columns: `start_time`, `end_time`, `length_min`,
`movie`, `issued_to`, `address`, `section`, `reservation_code`, `ticket_code`,
`file`.

## 4. Google setup (once)

Only needed for the flags that talk to Google — `--upload-drive`,
`--create-calendar`, `--input-drive`, `--config-drive`. Skip this section if
you just want the sorted folders.

The script acts as you against your own Drive and Calendar, which Google
requires an OAuth client for. It's free and takes a couple of minutes:

1. Go to [console.cloud.google.com](https://console.cloud.google.com/) and
   create a new project (or pick an existing one).
2. In **APIs & Services → Library**, enable the **Google Drive API**, the
   **Google Sheets API**, and (if you'll use `--create-calendar`) the
   **Google Calendar API**.
3. In **APIs & Services → OAuth consent screen**, choose **External**, fill in
   the required fields (app name, your email), and add yourself as a test user.
   You don't need to submit it for verification for personal use.
4. In **APIs & Services → Credentials**, click **Create Credentials → OAuth
   client ID**, choose **Desktop app**, and create it.
5. Download the resulting JSON and save it as `credentials.json` in the project
   root (next to `pyproject.toml`). It's gitignored, so it stays local.

The first run with `--upload-drive` opens a browser tab for you to authorize,
then caches a `token.json` (also gitignored) so later runs don't ask again.
Delete `token.json` to re-authorize — needed after revoking access, and the
first time you add `--create-calendar` to a setup whose cached token was
granted without calendar access.

Custom paths for either file: `--credentials PATH` / `--token PATH`.

## 5. The options

| Flag | What it does |
|---|---|
| `--merge-names` | Ask which name spellings are the same person, before sorting |
| `--rename-input` | Also rename the input PDFs in place |
| `--upload-drive` | Upload to Google Drive and build a linked Sheet |
| `--create-calendar` | One Google Calendar per person (needs `--upload-drive`) |
| `--input-drive` | Read the input PDFs from `unsorted/` in the Drive folder, not `input/` |
| `--config-drive` | Keep `config.toml` in the Drive folder (needs `--upload-drive`) |
| `--name NAME` | Name the Drive folder/Sheet/calendars, instead of `config.toml` doing it |
| `--config PATH` | Use a different `config.toml` |
| `--credentials PATH`, `--token PATH` | Use different Google credential files |

A full run, once set up, is usually:

```sh
uv run ticket-sorter --merge-names --upload-drive --create-calendar
```

`--upload-drive`, `--create-calendar` and `--input-drive` can also be turned on
permanently in `config.toml`, so you don't have to type them every time:

```toml
upload_drive = true
create_calendar = true
```

A flag is on if either the CLI flag or the config value is true; passing the
CLI flag can't turn off one the config enabled. `--config-drive` is the one
flag with no config equivalent: it decides which `config.toml` a run reads, so
by the time that file is read it has already had its say.

---

## Renaming input files

`--rename-input` renames each PDF in `input/` in place to the name it gets as a
sorted ticket — `bilety(3).pdf` becomes `matrix-GHI789__jan-nowak.pdf`. It's a
rename, not a copy: the file stays in `input/`, and this is independent of
`--upload-drive`/`--create-calendar`.

```sh
uv run ticket-sorter --rename-input
```

With `--input-drive` it renames the files in the Drive folder the input came
from instead — renaming the downloaded copies would be renaming what gets
thrown away at the end of the run.

## Collapsing one person's several names

The same person's tickets don't always carry the same name: they get booked as
`PAWEŁ ŻAK` and `PAWEŁ ZAK`, as `Joe` and `Joe "Rocket" Doe`, as `Anna Nowak`
and `Ania Anna Nowak`. Left alone, each spelling becomes its own recipient
folder, its own calendar, and its own set of rows.

Add `--merge-names` to be asked, before anything is sorted, which of the names
are the same person. It lists every name with its ticket count, guesses which
ones go together, and takes a group at a time until you're done:

```sh
uv run ticket-sorter --merge-names
```

```
12 names across 107 tickets:
    1  Jane                          2
    2  Jane Doe                      1
  ...
   11  Janina Kowalska              13
   12  Janina Kowalska-WIŚNIEWSKA    2

  maybe the same person:  1+2   4+5   7+8   6+9+10   11+12

Merge which names? (e.g. "1 2 5", blank when done): 11 12
    1  Janina Kowalska
    2  Janina Kowalska-WIŚNIEWSKA
Keep which name? (number, or type a different one; blank to cancel): 1
  collapsed 2 names into "Janina Kowalska".
```

The `maybe the same person` line is only a suggestion — it groups names sharing
a part, so it will also pair up relatives who share a surname. You decide by
picking the numbers; anything you don't pick is left alone. Instead of a number
you can type a name of your own, which collapses the whole group into a
spelling that appears on no ticket.

Each answer is saved to `config.toml` in the project root (the same file that
holds the name and flags) and applied on every run from then on, so you only
merge a name once:

```toml
[aliases]
"Jane" = "Jane Doe"
"PAWEŁ ZAK" = "PAWEŁ ŻAK"
"Janina Kowalska-WIŚNIEWSKA" = "Janina Kowalska"
```

The table is a plain variant → name mapping you can also edit by hand; delete a
line to split that name back out. Keep it among the tables at the end of the
file, since a run rewrites everything from the first table header down.

Merging happens before tickets are sorted, so the collapsed name is the one
used everywhere downstream: the recipient folder, the CSVs, the Sheet, and the
calendars. It also means a show one person booked twice under two spellings is
finally recognized as a repeat booking and moved to `duplicates/`.

Runs without `--merge-names` still apply the saved aliases, and print a note
when two names look like one person — but only when one name's parts are
contained in the other's (`Jane` vs `Jane Doe`), which a shared surname alone
isn't. Anything subtler than that only shows up in the `--merge-names` list.

## Uploading to Google Drive + Sheets

Needs the [Google setup](#4-google-setup-once) above.

Add `--upload-drive` to also upload everything to a Google Drive folder
(mirroring the `tickets/`/`duplicates/` layout, plus the two CSVs) and build a
Google Sheet with a `Tickets` tab and a `Duplicates` tab. Each row gets a
"ticket_pdf" column that links straight to the uploaded PDF, and both tabs get
a bold frozen header row with a filter applied so you can sort/filter them like
a normal table.

```sh
uv run ticket-sorter --upload-drive
```

The Drive folder and Sheet are both named after the `name` in `config.toml`
(see [Naming](#naming-the-drive-folder-sheet-and-calendar)).

Two things are shared by link, so what you hand out actually opens:

- **The Sheet: anyone with its link can edit it.** Drive, unlike Calendar, does
  allow write access on a link share.
- **Each ticket PDF: anyone with its link can view it.** That's what makes the
  Sheet's `ticket_pdf` column and the `Ticket` link in every calendar event work
  for the person you sent them to, not just for you.

The folders themselves stay private, so the PDFs are reachable only through a
link someone was given — there's no folder to browse and no way to enumerate
tickets that weren't shared. Sharing is checked against what's already set (for
the PDFs, off the folder listing that's read anyway), so a re-run doesn't
re-share anything: it's ~one extra write per newly uploaded PDF and none after.

Re-running syncs rather than re-uploads: a PDF already in Drive under the name
this run would give it is left alone (its existing link is reused), and only
files that this run wouldn't upload — tickets that changed name, recipients who
dropped out — get moved to the trash. The two CSVs and the Sheet are always
rewritten, since their contents change every run.

## Creating the Google Calendars

Add `--create-calendar` (alongside `--upload-drive`, since events link to the
uploaded tickets) to also build one calendar per ticket holder, named
`<name> – <person>` after the Drive folder/Sheet name, holding only that
person's showings: title is the movie, location is the venue address, and the
description links to their ticket PDF. It doesn't try to match names to Google
accounts or invite anyone as an attendee.

```sh
uv run ticket-sorter --upload-drive --create-calendar
```

Notes:

- Each person's calendar gets its own color, spread evenly around the color
  wheel, so overlapping showings are told apart by whose calendar they're on.
  Events themselves are uncolored (they inherit the calendar's color).
- Calendars are synced, not just added to, the same way the Drive folder is: a
  `<name> – <person>` calendar this run wouldn't create is deleted. That covers
  someone who dropped out of the festival, a name spelling since collapsed into
  another by `--merge-names`, and the single shared calendar named just
  `<name>` that older versions created. Only calendars you own and that carry
  the `<name> – ` prefix are touched — ones you're merely subscribed to, and
  anything named otherwise, are left alone.
- Only rows from `tickets.csv` get an event — a repeat booking recorded in
  `duplicates.csv` is for a show that already has one.
- Events are scheduled in the festival's local timezone (Europe/Warsaw), not
  whatever timezone your Google account or machine uses.
- If a movie's runtime isn't in the `FILM_LENGTHS_MIN` lookup table in
  `src/ticket_sorter/main.py`, its event falls back to a 90-minute length.
- Each printed `Calendar (<person>):` link opens that person's calendar and, as
  described below, is all anyone needs to add it to their own Google Calendar.

### Sharing the calendars

Each calendar is shared so that anyone with its link can see it, which is what
the printed `Calendar (<person>):` links are for — send someone theirs and they
can add it to their own Google Calendar, with no invite to accept and nothing
for you to approve.

```
Creating calendar 'Octopus 2026 Tickets – Jan Kowalski'...
  shared: anyone with the link can view
```

**A link can only ever carry read access.** Google Calendar has no "anyone with
the link" tier the way Drive does; its only link-based sharing is *public*, and
it refuses to pair that with a role that can write:

```
400 The default scope cannot be granted a write or owners access role to a calendar.
```

So a calendar can be link-shared, or editable by named people, but not editable
by whoever holds the link. Editing has to be granted per person, by Google
account address — see below.

What public sharing does mean, for a calendar holding film times: the calendar
is world-readable, and its id (the long string in the link) is the only thing
gating access — it isn't restricted to the people you send it to. Nobody can
change anything. Each event's description links to that showing's ticket PDF,
which is itself shared by link so it opens for whoever has the calendar, so a
public calendar does put those tickets within reach of anyone holding its id.

Re-running doesn't re-share a calendar that's already shared; the rule is read
first, so this costs nothing once set. To take it back, remove "Make available
to public" in the calendar's Google Calendar settings — but note the next run
will set it again.

To let someone edit their calendar, share it with their Google account from
Google Calendar's own settings ("Share with specific people" → their address →
"Make changes to events"). Runs leave those rules alone, so a person you've
added stays added.

### Handing a showing back

A ticket you've decided not to use — a genuine double booking, or one you're
giving away — can be marked without going back to the PDFs, from either the
Sheet or the calendar.

**In the Sheet**, tick the `duplicate` checkbox, the column immediately before
the film title:

| start_time | end_time | length_min | duplicate | movie | … |
|---|---|---|---|---|---|
| 2026-08-11 21:30 | 2026-08-11 23:13 | 103 | ☑ | Motor City | … |

**In the calendar**, rename the event to put `DUPLICATE` in front of its title:

```
Motor City            ->   DUPLICATE Motor City
```

Either way the next run picks it up, records it in `config.toml`, and treats
that ticket as a duplicate from then on: the PDF moves to `output/duplicates/`,
the row moves to the `Duplicates` tab, and the showing loses its calendar event.

```toml
[duplicates]
"ABC123" = "Joe “Rocket” Doe – Motor City, 2026-08-11 21:30"
```

The key is the ticket's reservation code, which is what actually matters; the
value is a label so the file says what you marked, refreshed on each run.

**To take a marking back**, untick the row's checkbox on the `Duplicates` tab —
or delete its line from `config.toml`. The calendar has no way back of its own:
a marked showing has no event left to edit.

Marks are read before the Sheet and the calendars are rewritten, so they aren't
lost to the rebuild, and because they're saved they keep applying afterwards.
Only the two meaningful directions are read — a tick on `Tickets` and an untick
on `Duplicates`. A ticked row on `Duplicates` and an unticked one on `Tickets`
are just their normal state, so leaving them alone is not an instruction.

Notes:

- Only manual marks can be unticked back. A ticket that's a duplicate because
  you hold two for the same showing is detected fresh each run, so unticking it
  lasts until the next run.
- The calendar prefix is matched case-insensitively, as a whole word, and may be
  followed by a separator — `DUPLICATE Motor City`, `duplicate: Motor City`, and
  `DUPLICATE – Motor City` all work. A film whose real title starts with
  "Duplicates" is not affected.
- Each event carries its reservation code as a private property, so the mark
  survives you rewriting the rest of the title. Events from before this existed
  are matched on person + film + start time instead; if neither works, the run
  says which event it couldn't place and leaves it alone.
- Marking a ticket doesn't make its showing unattended. If you hold two tickets
  for one showing and mark the one that was counting, the other is promoted to
  take its place rather than both landing in duplicates.

### Staying under Google's rate limits

Calendars are where a run does the most work, and Google throttles that. Two
things keep it under the limit.

**Events are synced, not rebuilt.** Each event carries its reservation code, so
a re-run matches events to tickets and writes only what actually differs. A
first run creates every event; a re-run with nothing new makes no writes at all,
just the reads to check. Changing one thing costs one write:

```
Reusing calendar 'Octopus 2026 Tickets – Anna Nowak'...
  23 showings: 0 added, 1 updated, 0 removed
```

Because a mismatched event is rewritten rather than left alone, an event whose
title you edited is put back the way it was — that's how a `DUPLICATE` mark you
made but that couldn't be traced to a ticket gets undone.

**Rejected calls are retried.** Anything the API turns down for pace — a 429, a
403 that says `rateLimitExceeded`, or a server error — is retried with an
exponential backoff and jitter, up to 7 attempts (about a minute of waiting):

```
  rate limited on event insert, waiting 4s (attempt 3/7)
```

Failures that aren't about pace aren't retried, since that would only spend more
of the same quota. Drive and Sheets calls go through the same retry.

One case can't be retried away: `quotaExceeded` on creating calendars. Google
rations calendar *creation* much harder than ordinary writes, and going over is
a cooldown rather than a momentary throttle, so the run stops and says to come
back later. To stay clear of it, calendar creation is paced a second apart —
which only costs anything on a first run, since calendars are reused afterwards.

Note that colors are assigned in the order people first appear, so adding or
removing tickets can shift several people's calendar colors and rewrite them.
That's at most one small write per person.

### Naming the Drive folder, Sheet, and Calendar

The name they all share comes from `config.toml` in the project root:

```toml
name = "Octopus 2026 Tickets"
```

If `config.toml` doesn't exist yet, the first `--upload-drive` run asks for a
name on the command line and creates the file for you, so future runs don't
ask again. Edit the file (or delete it to be asked again) to change the name;
custom path: `--config PATH`. `--name NAME` overrides it for one run, and is
how a run with nobody to ask — a GitHub Action — gets one.

## Taking the input from Drive

`--input-drive` reads the ticket PDFs out of a folder called `unsorted` inside
the Drive folder everything is sorted into, instead of out of local `input/`:

```
Octopus 2026 Tickets/     <- the folder named by config.toml
├── unsorted/             <- you drop bilety(3).pdf in here
│   └── ...
├── tickets/              <- and the run produces all of this
├── duplicates/
├── tickets.csv
├── duplicates.csv
└── Octopus 2026 Tickets  <- the Sheet
```

Create that `unsorted` folder yourself the first time (in Drive, inside the
folder named by `config.toml`) and put the PDFs in it. Then:

```sh
uv run ticket-sorter --input-drive --upload-drive --create-calendar
```

Notes:

- **The PDFs you drop in stay there.** That folder is the drop point, not a
  queue that gets drained, and re-sorting the same tickets costs a download and
  changes nothing else — every later step syncs rather than rebuilds.
- They're downloaded to `unsorted/` in the project directory, which is wiped
  and refilled on each run exactly like `output/` is. Nothing but the download
  lives there, so don't put anything of your own in it either.
- Two files in one Drive folder are allowed to share a name, and tickets tend
  to all arrive called `bilety.pdf`; the second one downloaded becomes
  `bilety-2.pdf` locally. It makes no difference to the sorting, which reads
  names out of the PDFs rather than off them.
- Anything in `unsorted` that isn't a PDF is skipped with a note, the same way
  a stray file in `input/` is.
- **A run that would sort nothing stops instead**, saying so, when that folder
  is missing or holds no PDFs. Sorting nothing isn't a quiet no-op here: the
  upload syncs, so it would take every ticket, row and event an earlier run
  created back out of Drive. To empty it all on purpose, delete the Drive
  folder.

### Keeping the config in Drive too

`--config-drive` (which needs `--upload-drive`) keeps `config.toml` in the
Drive folder: it's fetched before the run reads anything, and written back with
the rest of the upload. That's what makes the merged names and the duplicate
marks survive a run happening somewhere that keeps nothing — without it, every
run starts with an empty `[aliases]` and `[duplicates]`, and a ticket you
handed back on the Sheet comes back as a ticket on the next run.

The fetched copy *replaces* the file at `--config` (the project's
`config.toml`, unless you point it elsewhere), since it's the same config
seen from another machine. The name is the one thing not taken from it: it's
what found the folder in the first place, so pass `--name` or have it in the
local file already.

## Running it from GitHub Actions

With the input and the config both in Drive, a run needs nothing of its own on
the machine it happens on except a way to authorize — so it can happen on a
GitHub runner. `.github/workflows/sort-tickets.yml` does exactly the full run
above, and you trigger it from the **Actions** tab (or uncomment its `schedule`
to have it sort by itself every hour).

Two settings, both under **Settings → Secrets and variables → Actions**:

| | Where | What |
|---|---|---|
| `TICKET_SORTER_TOKEN` | Secrets | The contents of a `token.json` from a local run |
| `TICKET_SORTER_NAME` | Variables | The name of the Drive folder, Sheet and calendars |

To make the token, run it once locally with everything the Action will use, so
the token carries the Drive *and* Calendar access it'll need:

```sh
uv run ticket-sorter --upload-drive --create-calendar
cat token.json      # paste this into the TICKET_SORTER_TOKEN secret
```

`$TICKET_SORTER_TOKEN` is read ahead of `token.json` whenever it's set, and is
never written back to — the refresh it needs lasts the run, and a run that got
its token from the environment has nowhere to keep one. A run with no usable
token and no terminal to open a browser on says that, rather than hanging on a
browser that's never coming back.

Things worth knowing before you set this up:

- **While your OAuth consent screen is in "Testing", Google expires refresh
  tokens after 7 days.** The Action then fails with an `invalid_grant` error
  and the fix is to delete `token.json`, authorize locally again, and update
  the secret. To stop that, hit **Publish app** on the OAuth consent screen:
  for sensitive scopes an unverified app still works for its own owner (with a
  warning screen at authorization time, and a cap on how many users it can
  have), and its refresh tokens stop expiring on a weekly clock.
- **That secret is a key to your Google account**, within the scopes the token
  was granted: files this app created in Drive (`drive.file`), your
  spreadsheets, and your calendars. Anyone who can push to the repository, or
  edit a workflow in it, can use it. GitHub doesn't hand secrets to workflows
  from forked pull requests, but a private repository is still the right home
  for this.
- **`--merge-names` can't run there**: it's a conversation. Merge names in a
  local run with `--config-drive` (any run with it, including one that also
  uses `--input-drive`), and the answers land in the `config.toml` kept in
  Drive, which every later Action run reads.
- Runs are serialized by a `concurrency` group. Two runs sorting one folder at
  once would each trash what the other had just uploaded.

## Tests and QA

```sh
uv run poe qa       # lint + types + tests
```

The pieces run on their own too: `poe lint` (ruff), `poe types` (ty), `poe test`
(pytest). `poe format` (`ruff format`) is deliberately left out of `qa` — the
source is wrapped by hand and the formatter would rewrap it.

Nothing in the suite talks to Google or reads the real `input/`, `unsorted/`,
`output/` or `config.toml`. Ticket PDFs are generated with pypdf and parsed back through it,
and the Google clients are real `googleapiclient` services running on
`HttpMockSequence`, so requests are still built and checked against each API's
discovery document.
