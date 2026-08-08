# octopus-tickets-sorter

Sorts biletomat.pl movie ticket PDFs from `input/` into `output/`: one copy per
unique show goes to `output/tickets/<recipient>/`, repeat bookings for the same
show go to `output/duplicates/`. Also writes `tickets.csv` and `duplicates.csv`
with the parsed metadata (showtime, end time, runtime, movie, section, codes,
address, recipient). Runtimes come from a lookup table covering every film on
the festival's program (sourced from each film's event page, linked off
https://octopusfilmfestival.com/program/), keyed by title.

## Usage

```sh
uv run octopus-tickets-sorter
```

## Renaming input files

Add `--rename-input` to also rename each PDF in `input/` in place to what it
would be named as a ticket PDF (e.g. `bilety(3).pdf` becomes
`matrix-GHI789__jan-nowak.pdf`), instead of leaving the
downloaded filenames as-is. It's a rename, not a copy — the file stays in
`input/`, and this is independent of `--upload-drive`/`--create-calendar`.

```sh
uv run octopus-tickets-sorter --rename-input
```

## Collapsing one person's several names

The same person's tickets don't always carry the same name: they get booked as
`PAWEŁ ŻAK` and `PAWEŁ ZAK`, as `Joe` and `Joe
"Rocket" Doe`, as `Anna Nowak` and `Ania Anna
Nowak`. Left alone, each spelling becomes its own recipient folder, its own
calendar, and its own set of rows.

Add `--merge-names` to be asked, before anything is sorted, which of the names
are the same person. It lists every name with its ticket count, guesses which
ones go together, and takes a group at a time until you're done:

```sh
uv run octopus-tickets-sorter --merge-names
```

```
12 names across 107 tickets:
    1  Jane                             2
    2  Jane Doe                  1
  ...
   11  Janina Kowalska                13
   12  Janina Kowalska-WIŚNIEWSKA       2

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
holds the name and flags below) and applied on every run from then on, so you
only merge a name once:

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
contained in the other's (`Jane` vs `Jane Doe`), which a shared
surname alone isn't. Anything subtler than that only shows up in the
`--merge-names` list.

## Uploading to Google Drive + Sheets

Add `--upload-drive` to also upload everything to a Google Drive folder
(mirroring the `tickets/`/`duplicates/` layout, plus the two CSVs) and build
a Google Sheet with a `Tickets` tab and a `Duplicates` tab. Each row gets a
"ticket_pdf" column that links straight to the uploaded PDF, and both tabs
get a bold frozen header row with a filter applied so you can sort/filter
them like a normal table.

The Drive folder and Sheet are both named after the `name` in `config.toml`
(see below).

Re-running syncs rather than re-uploads: a PDF already in Drive under the
name this run would give it is left alone (its existing link is reused), and
only files that this run wouldn't upload — tickets that changed name,
recipients who dropped out — get moved to the trash. The two CSVs and the
Sheet are always rewritten, since their contents change every run.

```sh
uv run octopus-tickets-sorter --upload-drive
```

## Creating the Google Calendars

Add `--create-calendar` (alongside `--upload-drive`, since events link to the
uploaded tickets) to also build one calendar per ticket holder, named
`<name> – <person>` after the Drive folder/Sheet name, holding only that
person's showings: title is the movie, location is the venue address, and
the description links to their ticket PDF. It doesn't try to match names to
Google accounts or invite anyone as an attendee. Re-running wipes and
rebuilds each calendar's events.

```sh
uv run octopus-tickets-sorter --upload-drive --create-calendar
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
  `main.py`, its event falls back to a 90-minute length.
- Each printed `Calendar (<person>):` link opens that person's calendar and, as
  described below, is all anyone needs to add and edit it.

### Sharing the calendars

Each calendar is shared so that anyone with its link can edit it, which is what
the printed `Calendar (<person>):` links are for — send someone theirs and they
can add it to their own Google Calendar and change it, with no invite to accept
and nothing for you to approve.

```
Creating calendar 'Octopus 2026 Tickets – Jan Kowalski'...
  shared: anyone with the link can edit
```

Be aware of what that actually means, because Google Calendar has no
"anyone with the link" tier the way Drive does. The only link-based sharing it
offers is **public**: the calendar is world-readable and world-writable, and its
id — the long string in the link — is the only thing gating access. It isn't
restricted to the people you send the link to.

In practice, for a calendar holding film times, that means:

- Anyone with the link can add, change, and delete events. A run puts back
  anything deleted or edited, since events are synced against the tickets, so
  interference is undone rather than permanent.
- The one edit a run doesn't undo is the `DUPLICATE` prefix below — it's read as
  a deliberate instruction. Someone with the link can therefore hand back one of
  your showings, and the run will record it in `config.toml`.
- Event descriptions link to the ticket PDFs in Drive. Those links are
  separately permissioned, so a public calendar doesn't expose the PDFs.

Re-running doesn't re-share a calendar that's already shared; the rule is read
first, so this costs nothing once set. To take it back, remove "Make available
to public" in the calendar's Google Calendar settings — but note the next run
will set it again. If you'd rather share with named people instead of publicly,
that's a different sharing rule (per-email, `writer`) and would need their
addresses in `config.toml`.

### Handing a showing back from the calendar

A ticket you've decided not to use — a genuine double booking, or one you're
giving away — can be marked straight from the calendar, without going back to
the PDFs. Rename the event to put `DUPLICATE` in front of its title:

```
Motor City            ->   DUPLICATE Motor City
```

The next run picks it up, records it in `config.toml`, and treats that ticket
as a duplicate from then on: the PDF moves to `output/duplicates/`, the row
moves to the `Duplicates` tab, and the showing loses its calendar event.

```toml
[duplicates]
"ABC123" = "Joe “Rocket” Doe – Motor City, 2026-08-11 21:30"
```

The key is the ticket's reservation code, which is what actually matters; the
value is a label so the file says what you marked, refreshed on each run. Delete
a line to hand the showing back to its owner.

Marking is read before the calendars are rebuilt, so the mark isn't lost to the
rebuild — and because it's saved, it keeps applying after the event it was typed
on is gone. That also means the calendar can't undo it: once marked, edit
`config.toml` to reverse it.

Notes:

- The prefix is matched case-insensitively, as a whole word, and may be followed
  by a separator — `DUPLICATE Motor City`, `duplicate: Motor City`, and
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
ask again. Edit the file (or delete it to be asked again) to change the
name; custom path: `--config PATH`.

### Setting flags via config.toml

`--upload-drive` and `--create-calendar` can also be set as booleans in
`config.toml`, so you don't have to pass them on every run:

```toml
upload_drive = true
create_calendar = true
```

A flag is on if either the CLI flag or the config value is true; the CLI
flag never turns one off that the config enabled.

### One-time setup: Google Cloud OAuth client

This needs an OAuth client so the script can act as you against your own
Drive/Calendar. It's free and only takes a couple of minutes:

1. Go to [console.cloud.google.com](https://console.cloud.google.com/) and
   create a new project (or pick an existing one).
2. In **APIs & Services → Library**, enable the **Google Drive API**, the
   **Google Sheets API**, and (if you'll use `--create-calendar`) the
   **Google Calendar API**.
3. In **APIs & Services → OAuth consent screen**, choose **External**, fill
   in the required fields (app name, your email), and add yourself as a test
   user. You don't need to submit it for verification for personal use.
4. In **APIs & Services → Credentials**, click **Create Credentials → OAuth
   client ID**, choose **Desktop app**, and create it.
5. Download the resulting JSON and save it as `credentials.json` in the
   project root (next to `pyproject.toml`). It's gitignored, so it stays
   local.

The first `--upload-drive` run opens a browser tab for you to authorize; a
`token.json` (also gitignored) is cached afterwards so future runs don't
need to re-authorize. Delete `token.json` to re-authorize — needed after
revoking access, or the first time you add `--create-calendar` to a session
that already had a cached token (it was authorized without calendar access).

Custom paths for either file: `--credentials PATH` / `--token PATH`.
