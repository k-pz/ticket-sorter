#!/usr/bin/env python3
"""Rename biletomat.pl ticket PDFs and export their metadata to a CSV."""
import argparse
import csv
import re
import shutil
import sys
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

import pypdf

ROOT_DIR = Path(__file__).resolve().parents[2]
INPUT_DIR = ROOT_DIR / "input"
OUTPUT_DIR = ROOT_DIR / "output"
TICKETS_DIR = OUTPUT_DIR / "tickets"
DUPLICATES_DIR = OUTPUT_DIR / "duplicates"

DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}$")

# Runtimes in minutes for every film on the program, from each one's "Czas"
# field on https://octopusfilmfestival.com/wydarzenie/<slug>/ (linked from
# https://octopusfilmfestival.com/program/). Keyed by title; entries with a
# "+ spotkanie..." suffix use whichever wording that film's own ticket or
# event page uses (they can differ), and some titles appear twice with a
# straight vs. curly apostrophe for the same reason. Events with no listed
# runtime (talks, panels, club nights, etc.) are simply absent.
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
    "Klub Komediowy vs. VHS Hell – Bitwa lektorów": 90,
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


def parse_ticket(path: Path):
    lines = [l.strip() for l in pypdf.PdfReader(path).pages[0].extract_text().split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    i = next((n for n, l in enumerate(lines) if DATE_RE.match(l)), None)
    if i is None:
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


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upload-drive", action="store_true", help="upload output to Google Drive and build a linked Sheet")
    parser.add_argument(
        "--rename-input", action="store_true",
        help="rename each PDF in input/ in place to what it would be named as a ticket PDF",
    )
    parser.add_argument(
        "--create-calendar", action="store_true",
        help="also create a Google Calendar with one event per showing, linking each attendee's ticket "
             "(requires --upload-drive, since events link to the uploaded tickets)",
    )
    parser.add_argument("--credentials", type=Path, default=ROOT_DIR / "credentials.json", help="OAuth client secrets file")
    parser.add_argument("--token", type=Path, default=ROOT_DIR / "token.json", help="cached OAuth token file")
    parser.add_argument(
        "--config", type=Path, default=ROOT_DIR / "config.toml",
        help="config file holding the Drive folder/Sheet/Calendar name (created interactively if missing)",
    )
    args = parser.parse_args()
    if args.create_calendar and not args.upload_drive:
        parser.error("--create-calendar requires --upload-drive")
    return args


def main():
    args = parse_args()

    shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
    OUTPUT_DIR.mkdir(parents=True)

    rows, duplicate_rows = [], []
    seen_shows = set()
    for pdf in sorted(INPUT_DIR.glob("*.pdf")):
        ticket = parse_ticket(pdf)
        if ticket is None:
            print(f"skip (unrecognized layout): {pdf.name}", file=sys.stderr)
            continue

        # A "show" is one person's booking for one screening; any ticket beyond
        # the first for the same show is a duplicate.
        show_key = (ticket["issued_to"], ticket["movie"], ticket["start_time"], ticket["address"])
        is_duplicate = show_key in seen_shows
        seen_shows.add(show_key)

        base_name = f"{slugify(ticket['movie'])}-{ticket['reservation_code']}"
        if is_duplicate:
            new_path = DUPLICATES_DIR / f"{base_name}.pdf"
            DUPLICATES_DIR.mkdir(parents=True, exist_ok=True)
        else:
            recipient_dir = TICKETS_DIR / slugify(ticket["issued_to"])
            new_path = recipient_dir / f"{base_name}__{slugify(ticket['issued_to'])}.pdf"
            recipient_dir.mkdir(parents=True, exist_ok=True)

        if new_path.exists():
            print(f"skip copy (target exists): {new_path.name}", file=sys.stderr)
        else:
            shutil.copy2(pdf, new_path)
        ticket["file"] = str(new_path.relative_to(OUTPUT_DIR))

        if args.rename_input:
            renamed = pdf.with_name(new_path.name)
            if renamed != pdf:
                if renamed.exists():
                    print(f"skip rename (target exists): {renamed.name}", file=sys.stderr)
                else:
                    pdf.rename(renamed)

        (duplicate_rows if is_duplicate else rows).append(ticket)

    rows.sort(key=lambda t: t["start_time"])
    duplicate_rows.sort(key=lambda t: t["start_time"])

    write_csv(OUTPUT_DIR / "tickets.csv", rows)
    write_csv(OUTPUT_DIR / "duplicates.csv", duplicate_rows)

    print(f"wrote {len(rows)} tickets and {len(duplicate_rows)} duplicates to {OUTPUT_DIR}")

    if args.upload_drive:
        from .config import load_name
        from .drive_upload import upload_to_drive

        name = load_name(args.config)
        folder_url, sheet_url = upload_to_drive(rows, duplicate_rows, OUTPUT_DIR, name, args.credentials, args.token)
        print(f"Drive folder: {folder_url}")
        print(f"Sheet: {sheet_url}")

        if args.create_calendar:
            from .calendar_upload import create_calendar

            calendar_url = create_calendar(rows, name, args.credentials, args.token)
            print(f"Calendar: {calendar_url}")


if __name__ == "__main__":
    main()
