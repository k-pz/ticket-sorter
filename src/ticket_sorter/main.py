#!/usr/bin/env python3
"""Rename biletomat.pl ticket PDFs and export their metadata to a CSV."""
import argparse
import csv
import hashlib
import io
import logging
import os
import re
import shutil
import sys
import unicodedata
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pypdf

from .config import CONFIG_ENV, NAME_ENV, load_aliases, load_duplicates, load_flags, load_name, save_tables
from .names import look_alikes, merge_names, resolve

ROOT_DIR = Path(__file__).resolve().parents[2]
INPUT_DIR = ROOT_DIR / "input"
UNSORTED_DIR = ROOT_DIR / "unsorted"  # where --input-drive puts what it downloads
OUTPUT_DIR = ROOT_DIR / "output"
TICKETS_DIR = OUTPUT_DIR / "tickets"
DUPLICATES_DIR = OUTPUT_DIR / "duplicates"

# The two ways the sorted tickets are grouped, as a subfolder of tickets/ each. Every
# sorted ticket is filed under both: a folder per person answers "what am I going to?",
# and one stitched PDF per showing answers "who is coming to this?" — a single file to
# hand over or print at the door, rather than a folder to gather up first. Neither answer
# is one you should have to assemble out of the other by hand.
BY_HOLDER = "by-holder"
BY_SHOWING = "by-showing"

DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}$")

# Runtimes in minutes for every film on the program, from each one's "Czas"
# field on https://octopusfilmfestival.com/wydarzenie/<slug>/ (linked from
# https://octopusfilmfestival.com/program/). Last checked against all 143
# showings on 2026-08-10: 86 titles list one, and none had changed.
#
# Keyed by the title the *ticket* carries, which isn't always the site's:
# biletomat renames some events outright, and others differ only in a straight
# vs. curly apostrophe, so one film can appear here under both spellings.
#
# The remaining 22 events — talks, panels, club nights, and a few films the
# site just doesn't measure — list no runtime anywhere, and are absent; their
# events fall back to calendar_upload's FALLBACK_LENGTH_MIN. The program's own
# timeslots can't stand in: consecutive slots at one venue sit 60 to 300
# minutes apart, and ten of the 22 are the last thing on there that day.
FILM_LENGTHS_MIN = {
    "Amerykańska muzyka pop": 96,
    "Amerykański domek dla lalek": 87,
    "Angel Mine": 68,
    "Aniołki Charliego": 98,
    "Anzu. Kot-duch / Kino dostępne": 97,
    "Bilardziści": 98,
    "Bliskie spotkania trzeciego stopnia": 138,
    "Body Melt": 81,
    "Cela": 107,
    "Coonskin": 100,
    "Czterech jeźdźców Apokalipsy + spotkanie z Fabio Testim": 104,
    "Człowiek ciemności": 96,
    "DKF Pełna Sala: Nie oglądaj się teraz": 110,
    "Drabina Jakubowa": 113,
    "Dzieci nocy": 87,
    "Dziecko nocy": 92,
    "Dzień, w którym zatrzymała się Ziemia": 92,
    "Fascination": 82,
    "Film otwarcia festiwalu: Miłość, śmierć i dojrzewanie na Obozie Miasma": 106,
    "Gala wręczenia nagród + seans China O’Brien": 86,
    "Ghost in the Cell": 106,
    "Hakerzy": 105,
    "Heavy Traffic": 77,
    "Hodowla": 97,
    "I Want Your Sex": 90,
    "Inni": 101,
    "Kim jest Fabio Soldani?": 47,
    "Klub Komediowy vs VHS HELL pojedynek lektorów": 90,  # what the ticket calls it
    "Klub Komediowy vs. VHS Hell – Bitwa lektorów": 90,   # and what the program does
    "Kot Fritz": 78,
    "Krzycz, królowo! Mój prywatny koszmar na ulicy Wiązów": 99,
    "Krótkie metraże z ASP w Gdańsku": 48,
    "Labirynt": 101,
    "Lalka": 83,
    "Leviticus": 88,
    "Lisica": 90,
    "Martwe zło 2": 84,
    "Martwi przed świtem (wersja VHS)": 92,
    "Matrix": 136,
    "Miasto zaginionych dzieci": 112,
    "Motor City": 103,
    "Na krańcu świata": 109,
    "Najważniejsze to kochać + spotkanie z Fabio Testim": 109,
    "Niepokonani + spotkanie z Cynthią Rothrock": 95,
    "Nowy początek": 116,
    "Octopus Shorts – Blok 1: Ja chyba śnię": 87,
    "Octopus Shorts – Blok 2: Bliskie spotkania": 88,
    "Octopus Shorts – Blok 3: Homo filmus": 90,
    "Octopus Shorts – Blok 4: Oto ciało moje": 92,
    "Octopus Shorts – Blok 5: LAB": 92,
    "Oddział specjalny": 89,
    "Opowieści z magicznego ogrodu": 71,
    "Piekielne bebechy": 111,
    "Pitch Black": 110,
    "Pokaz krótkich metraży Krzysztofa Kiwerskiego i spotkanie z reżyserem": 58,
    "Poza prawem": 106,
    "Przemocowiec": 76,
    "Rewolwer + spotkanie z Fabio Testim": 111,
    "Sacharyna": 113,
    "Seans zamknięcia: Witajcie w Hope": 160,
    "Stalowy gigant": 86,
    "Stone": 132,
    "Szczur lądowy": 87,
    "Tajemnica śpiewających ptaków": 77,
    "Tank Girl": 104,
    "The Quiet Earth": 91,
    "Tura!": 105,
    "Uprowadzenie": 109,
    "Uzurpatorzy": 102,
    "Vampyres": 87,
    "Wampiryczne lesbijki": 89,
    "What Have You Done to Solange?": 107,
    "Who Am I. Możesz być kim chcesz": 102,
    "Wiedźmia matka": 104,
    "Wiedźmin + spotkanie": 130,
    "Wiedźmin + spotkanie z reżyserem Markiem Brodzkim": 130,
    "Wolf Cat Fever": 100,
    "Wredne dziewczyny": 97,
    "Wybawcy": 90,
    "Wypożyczalnia Kim's Video": 88,
    "Wypożyczalnia Kim’s Video": 88,
    "Władca pierścieni": 132,
    "Za murami klasztoru": 95,
    "Zestaw krótkich metraży dla najmłodszych": 38,
    "Zestaw krótkich metraży z festiwalu Curtas (Hiszpania)": 111,
    "Zoolander": 90,
    "Złowieszcze nasienie": 70,
    "Śmiertelna obsesja": 82,
    "Śmiertelnie proste": 99,
}


def slugify(text: str) -> str:
    text = text.translate(str.maketrans("łŁ", "lL"))  # NFKD drops ł/Ł instead of stripping the stroke
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()


def showing_slug(ticket: dict) -> str:
    """The name one showing's tickets are stitched into: when it starts, then what's on.

    Time first, so that listing by-showing/ reads as the festival's running order rather
    than as an alphabet of film titles. The venue is left out for brevity, so one film
    playing at two places in the same minute would share a PDF — nothing a single program
    does, and its pages are bookmarked by holder either way.
    """
    return f"{ticket['start_time']:%Y-%m-%d-%H%M}-{slugify(ticket['movie'])}"


def free_path(directory: Path, name: str) -> Path:
    """Where to put a file called `name` in `directory`, given everything already there.

    A directory may not hold two files under one name, and two files that want the same one
    are still two files — so the second gets `-2` before its suffix rather than landing on
    top of the first, or being left out for being unable to.
    """
    path, stem, suffix, n = directory / name, Path(name).stem, Path(name).suffix, 1
    while path.exists():
        n += 1
        path = directory / f"{stem}-{n}{suffix}"
    return path


def _digest(path: Path) -> bytes:
    """What a file holds, as something short enough to compare against every other file."""
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").digest()


def input_pdfs(directory: Path) -> list[Path]:
    """Every PDF under `directory`, subfolders and all, bar the ones that are plain copies
    of a PDF already found.

    Tickets arrive from biletomat.pl all called the same thing — `bilety.pdf` — and a
    directory can't hold two files under that one name. So a person with several of them
    has several folders, one per download, and a scan that read only the top level would
    sort whichever file happened to be moved out of its folder and silently drop the rest.
    Each of those is a ticket of its own, whatever it shares a name with.

    Sharing a name and a byte for byte identical content is another matter: that is one
    download that got copied about — into a backup folder, or twice out of the same mail
    — and sorting it twice would put the same ticket in the output a second time as a
    duplicate of itself. Only that pair counts as a copy. Two files that hold the same
    bytes under different names have been told apart by hand and are left as two, and
    a shared name alone says nothing at all when every ticket arrives under one name.

    Of a set of copies it's the first in path order that's read, so which of them the run
    sorts — and with --rename-input, which one gets renamed — is the same on every run.
    """
    found: list[Path] = []
    seen: set[tuple[str, bytes]] = set()
    # Every file is hashed, rather than only the ones whose name turns out to repeat: a
    # ticket PDF is a few tens of KB, and reading each of them once more costs less than
    # the bookkeeping to work out which ones could be left unread.
    for path in sorted(path for path in directory.rglob("*.pdf") if path.is_file()):
        key = (path.name, _digest(path))
        if key in seen:
            print(f"skip (a copy of one already read): {path.relative_to(directory)}", file=sys.stderr)
            continue
        seen.add(key)
        found.append(path)
    return found


class _Complaints(logging.Handler):
    """A log handler that keeps a tally by wording rather than writing anything out."""

    def __init__(self):
        super().__init__()
        self.counts = Counter()

    def emit(self, record: logging.LogRecord) -> None:
        self.counts[record.getMessage()] += 1


@contextmanager
def pypdf_complaints(doing: str):
    """Tally what pypdf has to say about the ticket PDFs, and say it once at the end.

    biletomat's exports name objects they don't contain. Reading a ticket's text never
    follows those references, but stitching a showing walks the whole of every ticket, so
    pypdf says so — through `logging`, which with no logging set up means a bare line
    straight to stderr, one per lookup, with no file name and nothing to say what it means.
    A run of a hundred-odd tickets is hundreds of "Object 1 0 not defined." and nothing
    else, which reads like the run coming apart.

    It isn't: pypdf reads a reference it can't resolve as null and the page still comes out
    whole. So the count and the wording, said once and marked as a note like the rest, is
    all of that worth keeping — and the log stays the run's own to talk in.
    """
    logger = logging.getLogger("pypdf")
    handler = _Complaints()
    # Having a handler at all is also what stops logging's bare last-resort one.
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        # Only the common few by name: one broken reference per ticket is one line, while a
        # PDF broken in some way of its own would otherwise be a hundred lines of its own.
        for message, count in handler.counts.most_common(5):
            print(f'note: pypdf, {doing}: "{message}"{f" ×{count}" if count > 1 else ""}', file=sys.stderr)
        if len(handler.counts) > 5:
            print(f"note: pypdf, {doing}: and {len(handler.counts) - 5} other complaints", file=sys.stderr)


def parse_ticket(path: Path):
    lines = [line.strip() for line in pypdf.PdfReader(path).pages[0].extract_text().split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    i = next((n for n, line in enumerate(lines) if DATE_RE.match(line)), None)
    # Anything short of a title line at +11 and an organizer line after it isn't a ticket,
    # whatever matched the date: a stray receipt in input/ is skipped, not read past.
    if i is None or len(lines) <= i + 12:
        return None
    # ponytail: fixed offsets from the date line, holds because every
    # biletomat.pl export uses the same field order. Movie title fills
    # whatever's between the seat line and the last (organizer) line, as
    # "festival section | movie name".
    section, _, movie = " ".join(lines[i + 11:-1]).partition("|")
    movie = movie.strip()
    start_time = datetime.strptime(lines[i], "%d.%m.%Y %H:%M")
    length_min = FILM_LENGTHS_MIN.get(movie)
    return {
        "start_time": start_time,
        "length_min": length_min,
        "end_time": start_time + timedelta(minutes=length_min) if length_min else None,
        "section": section.strip(),
        "movie": movie,
        "reservation_code": lines[i + 9],
        "ticket_code": lines[i + 6],
        "address": f"{lines[i + 5]}, {lines[i + 2]}",
        "issued_to": lines[i + 8],
    }


def write_csv(csv_path: Path, rows: list) -> None:
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "start_time", "end_time", "length_min", "movie", "issued_to", "address",
            "section", "reservation_code", "ticket_code", "file",
        ])
        for t in rows:
            w.writerow([
                t["start_time"].strftime("%Y-%m-%d %H:%M"),
                t["end_time"].strftime("%Y-%m-%d %H:%M") if t["end_time"] else "",
                t["length_min"] or "", t["movie"], t["issued_to"], t["address"],
                t["section"], t["reservation_code"], t["ticket_code"], t["file"],
            ])


def resolve_name(args) -> str:
    """The name the Drive folder, Sheet and Calendars share. Resolved once and kept on args:
    --config-drive replaces the config file mid-run with the copy from Drive, and the name
    that found that copy has to outlive being replaced."""
    if not args.name:
        args.name = load_name(args.config)
    return args.name


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--upload-drive", action="store_true",
        help="upload output to Google Drive and build a linked Sheet (also settable as upload_drive in config.toml)",
    )
    parser.add_argument(
        "--input-drive", action="store_true",
        help="read the input PDFs from the 'unsorted' folder inside the Drive folder rather than from input/ "
             "(also settable as input_drive in config.toml)",
    )
    parser.add_argument(
        "--config-drive", action="store_true",
        help="keep config.toml in the Drive folder: read it from there before the run, and write it back as part "
             "of the upload (requires --upload-drive; a CLI flag only, since it picks the config to read)",
    )
    parser.add_argument(
        "--name", default=os.environ.get(NAME_ENV),
        help=f"name for the Drive folder, Sheet and Calendars, instead of the one in config.toml "
             f"(also settable as ${NAME_ENV})",
    )
    parser.add_argument(
        "--rename-input", action="store_true",
        help="rename each input PDF in place to what it would be named as a ticket PDF "
             "(in input/, or in the Drive folder it came from with --input-drive)",
    )
    parser.add_argument(
        "--merge-names", action="store_true",
        help="before sorting, ask which of the names tickets were issued to are the same person, "
             "and save the answers as an [aliases] table in config.toml",
    )
    parser.add_argument(
        "--create-calendar", action="store_true",
        help="also create one Google Calendar per ticket holder, with an event per showing linking their ticket "
             "(requires --upload-drive, since events link to the uploaded tickets; "
             "also settable as create_calendar in config.toml)",
    )
    parser.add_argument("--credentials", type=Path, default=ROOT_DIR / "credentials.json", help="OAuth client secrets file")
    parser.add_argument("--token", type=Path, default=ROOT_DIR / "token.json", help="cached OAuth token file")
    parser.add_argument(
        "--config", type=Path, default=Path(os.environ.get(CONFIG_ENV) or ROOT_DIR / "config.toml"),
        help=f"config file holding the Drive folder/Sheet/Calendar name and upload flags, created interactively "
             f"if missing (also settable as ${CONFIG_ENV})",
    )
    args = parser.parse_args()

    # Before the flags are read, so that a run driven from Drive takes its flags, aliases and
    # duplicate marks off the copy kept there rather than off whatever this machine holds.
    if args.config_drive:
        from .drive_input import download_config

        download_config(resolve_name(args), args.config, args.credentials, args.token)

    flags = load_flags(args.config)
    args.upload_drive = args.upload_drive or flags["upload_drive"]
    args.create_calendar = args.create_calendar or flags["create_calendar"]
    args.input_drive = args.input_drive or flags["input_drive"]

    if args.create_calendar and not args.upload_drive:
        parser.error("--create-calendar requires --upload-drive (set either as a CLI flag or in config.toml)")
    if args.config_drive and not args.upload_drive:
        parser.error("--config-drive requires --upload-drive (the config is written back as part of the upload)")
    return args


def rename_sources(renames: dict, drive_files: dict, credentials_path: Path, token_path: Path) -> None:
    """Give every input PDF the name its ticket got. Input taken from Drive is renamed where
    it actually lives, in the unsorted folder — the copies of it this run downloaded are
    thrown away with the rest of the scratch directory."""
    if drive_files:
        from .drive_input import rename_unsorted

        rename_unsorted(
            {drive_files[source]["id"]: name for source, name in renames.items()
             if source in drive_files and drive_files[source]["name"] != name},
            credentials_path, token_path,
        )
        return
    for source, name in renames.items():
        renamed = source.with_name(name)
        if renamed == source:
            continue
        if renamed.exists():
            print(f"skip rename (target exists): {renamed.name}", file=sys.stderr)
        else:
            source.rename(renamed)


def main():
    # Anywhere but a terminal — a GitHub Actions log, or a tee — Python holds stdout in a
    # block buffer and lets the lot out at exit. A run takes minutes, and everything it
    # prints as it goes is there to be read while it goes; buffered, it says nothing until
    # it is over, and a run stopped part-way says nothing at all. A flush per line is what
    # that costs, and it also puts the notes back beside what they're about, stderr being
    # line-buffered already. Under a test runner stdout is something else again, with
    # nothing to reconfigure and nothing needing it, so it's asked of a real stream only.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)

    args = parse_args()

    # Empty rather than None when nothing needs it: every reader of it is behind a flag that
    # does. Resolved before anything is read, since with --input-drive it's what says where.
    name = resolve_name(args) if args.upload_drive or args.input_drive else ""

    input_dir, drive_files = INPUT_DIR, {}
    if args.input_drive:
        from .drive_input import download_unsorted

        input_dir = UNSORTED_DIR
        drive_files = download_unsorted(name, input_dir, args.credentials, args.token)

    tickets = []
    with pypdf_complaints("reading the tickets"):
        for pdf in input_pdfs(input_dir):
            ticket = parse_ticket(pdf)
            if ticket is None:
                # Named by where it sits under the input folder, not just by its file name:
                # several of those are `bilety.pdf` and only the folder tells them apart.
                print(f"skip (unrecognized layout): {pdf.relative_to(input_dir)}", file=sys.stderr)
                continue
            ticket["source"] = pdf
            tickets.append(ticket)

    # Said out loud, because it's otherwise silent: the run carries on, and the showing
    # just gets a calendar event of a made-up length. Most of these are events the program
    # lists no runtime for at all, but a title biletomat spells its own way looks exactly
    # the same from here — and that one is fixed by adding it to FILM_LENGTHS_MIN.
    for movie in sorted({t["movie"] for t in tickets if t["length_min"] is None}):
        print(f"note: no runtime known for {movie} — its event gets a fallback length", file=sys.stderr)

    # One person can have tickets issued to several spellings of their name; the
    # saved aliases collapse those before anything is grouped by ticket holder.
    stored_aliases = load_aliases(args.config)
    aliases = stored_aliases
    if args.merge_names:
        aliases = merge_names(Counter(t["issued_to"] for t in tickets), stored_aliases)
    for t in tickets:
        t["issued_to"] = resolve(t["issued_to"], aliases)
    if not args.merge_names:
        for group in look_alikes({t["issued_to"] for t in tickets}, strict=True):
            print(
                f"note: {' / '.join(group)} may be one person — re-run with --merge-names to collapse them",
                file=sys.stderr,
            )

    # Showings handed back by hand: a DUPLICATE prefix on a calendar event, or the Sheet's
    # checkbox. Both are read before their surface is rewritten, and remembered here so the
    # ticket stays a duplicate afterwards. Unticking on the Sheet is the way back, so it is
    # applied last.
    stored_duplicates = load_duplicates(args.config)
    marked = set(stored_duplicates)
    if args.create_calendar:
        from .calendar_upload import read_duplicate_marks

        marked |= read_duplicate_marks(name, tickets, aliases, args.credentials, args.token)
    if args.upload_drive:
        from .drive_upload import read_sheet_marks

        ticked, unticked = read_sheet_marks(name, args.credentials, args.token)
        marked = (marked | ticked) - unticked
    labels = {
        t["reservation_code"]: f"{t['issued_to']} – {t['movie']}, {t['start_time']:%Y-%m-%d %H:%M}"
        for t in tickets
    }
    duplicates = {code: labels.get(code, stored_duplicates.get(code, "")) for code in marked}
    if (aliases, duplicates) != (stored_aliases, stored_duplicates):
        save_tables(args.config, aliases, duplicates)

    shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
    OUTPUT_DIR.mkdir(parents=True)

    rows, duplicate_rows = [], []
    by_showing = {}  # showing slug -> the tickets stitched into that showing's PDF
    seen_shows = set()
    renames = {}  # source -> the name its ticket got, applied once every source has one
    for ticket in tickets:
        source = ticket["source"]
        # A "show" is one person's booking for one screening; any ticket beyond
        # the first for the same show is a duplicate, as is one marked by hand.
        # A marked ticket doesn't take the show's slot, so another ticket for the
        # same show still gets to be the one that counts.
        show_key = (ticket["issued_to"], ticket["movie"], ticket["start_time"], ticket["address"])
        is_duplicate = ticket["reservation_code"] in duplicates or show_key in seen_shows
        if ticket["reservation_code"] not in duplicates:
            seen_shows.add(show_key)

        base_name = f"{slugify(ticket['movie'])}-{ticket['reservation_code']}"
        if is_duplicate:
            directory, file_name = DUPLICATES_DIR, f"{base_name}.pdf"
        else:
            slug = slugify(ticket["issued_to"])
            directory, file_name = TICKETS_DIR / BY_HOLDER / slug, f"{base_name}__{slug}.pdf"
        directory.mkdir(parents=True, exist_ok=True)

        # output/ was wiped at the start of the run, so anything already sitting under this
        # name belongs to another ticket of this same run — one reservation code can cover
        # several tickets. That's a different ticket PDF, so it gets a name of its own
        # instead of being left out of the output for having an unlucky name.
        new_path = free_path(directory, file_name)
        shutil.copy2(source, new_path)
        # The by-holder copy is the ticket's one address: what the CSVs point at, and what
        # gets uploaded and linked from the Sheet and the calendars.
        ticket["file"] = str(new_path.relative_to(OUTPUT_DIR))

        if not is_duplicate:
            by_showing.setdefault(showing_slug(ticket), []).append(ticket)

        renames[source] = new_path.name
        (duplicate_rows if is_duplicate else rows).append(ticket)

    # The second grouping: everyone's tickets for one showing stitched into one PDF, so a
    # showing is handed over or printed as a single file. In holder order and bookmarked
    # by holder, since one page of it otherwise looks much like the next.
    print(f"Stitching {len(by_showing)} showing PDFs ({len(rows)} tickets)...")
    with pypdf_complaints("stitching the showings"):
        for n, (slug, group) in enumerate(by_showing.items(), 1):
            writer = pypdf.PdfWriter()
            for t in sorted(group, key=lambda t: (t["issued_to"], t["reservation_code"])):
                writer.append(OUTPUT_DIR / t["file"], outline_item=t["issued_to"])
            showing_pdf = TICKETS_DIR / BY_SHOWING / f"{slug}.pdf"
            showing_pdf.parent.mkdir(parents=True, exist_ok=True)
            writer.write(showing_pdf)
            print(f"  [{n}/{len(by_showing)}] {showing_pdf.name} ({len(group)} tickets)")

    if args.rename_input:
        rename_sources(renames, drive_files, args.credentials, args.token)

    rows.sort(key=lambda t: t["start_time"])
    duplicate_rows.sort(key=lambda t: t["start_time"])

    write_csv(OUTPUT_DIR / "tickets.csv", rows)
    write_csv(OUTPUT_DIR / "duplicates.csv", duplicate_rows)

    print(f"wrote {len(rows)} tickets and {len(duplicate_rows)} duplicates to {OUTPUT_DIR}")

    if args.upload_drive:
        from .drive_upload import upload_to_drive

        folder_url, sheet_url = upload_to_drive(
            rows, duplicate_rows, OUTPUT_DIR, name, args.credentials, args.token,
            config_path=args.config if args.config_drive else None,
        )
        print(f"Drive folder: {folder_url}")
        print(f"Sheet: {sheet_url}")

        if args.create_calendar:
            from .calendar_upload import create_calendars

            for person, calendar_url in create_calendars(rows, name, args.credentials, args.token).items():
                print(f"Calendar ({person}): {calendar_url}")


if __name__ == "__main__":
    main()
