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

## Uploading to Google Drive + Sheets

Add `--upload-drive` to also upload everything to a Google Drive folder
(mirroring the `tickets/`/`duplicates/` layout, plus the two CSVs) and build
a Google Sheet with a `Tickets` tab and a `Duplicates` tab. Each row gets a
"ticket_pdf" column that links straight to the uploaded PDF, and both tabs
get a bold frozen header row with a filter applied so you can sort/filter
them like a normal table.

The Drive folder and Sheet are both named after the `name` in `config.toml`
(see below).

```sh
uv run octopus-tickets-sorter --upload-drive
```

## Creating a Google Calendar

Add `--create-calendar` (alongside `--upload-drive`, since events link to the
uploaded tickets) to also build a Google Calendar — named the same as the
Drive folder/Sheet — with one event per showing: title is the movie,
location is the venue address, and the description links to each attendee's
ticket PDF by name (e.g. "Anna Nowak's ticket"). It doesn't try
to match names to Google accounts or invite anyone as an attendee — the
names are just text with a link next to them. Re-running wipes and rebuilds
the calendar's events, same as the Drive folder and Sheet.

```sh
uv run octopus-tickets-sorter --upload-drive --create-calendar
```

Notes:

- Each showing's event is colored to match its row's background in the
  Sheet's `Tickets` tab. Google Calendar only offers 11 fixed event colors
  (vs. the Sheet's continuous palette), so it's the closest available color
  by hue, not an exact match.
- Only rows from `tickets.csv` get an event — a repeat booking recorded in
  `duplicates.csv` is for a show that already has one.
- Events are scheduled in the festival's local timezone (Europe/Warsaw), not
  whatever timezone your Google account or machine uses.
- If a movie's runtime isn't in the `FILM_LENGTHS_MIN` lookup table in
  `main.py`, its event falls back to a 90-minute length.
- The printed `Calendar:` link opens the calendar directly; from there use
  Google Calendar's own sharing options if you want to add it to someone
  else's account.

### Naming the Drive folder, Sheet, and Calendar

The name they all share comes from `config.toml` in the project root:

```toml
name = "Octopus 2026 Tickets"
```

If `config.toml` doesn't exist yet, the first `--upload-drive` run asks for a
name on the command line and creates the file for you, so future runs don't
ask again. Edit the file (or delete it to be asked again) to change the
name; custom path: `--config PATH`.

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
