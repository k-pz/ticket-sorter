"""main.py: slugs, PDF parsing, the CSV, argument parsing, and a full sorting run.

parse_ticket is driven by real PDFs written by the make_pdf fixture, and main() runs
against tmp_path directories patched over its module-level INPUT_DIR/OUTPUT_DIR, with
the Drive and Calendar flags off so nothing network-shaped is reached — bar the one
--upload-drive test, which patches out the two drive_upload functions main calls.
"""
import csv
import importlib
import io
import sys
import tomllib
from datetime import datetime
from types import SimpleNamespace

import conftest
import pytest

# the package's __init__ re-exports main(), shadowing the submodule of the same name
m = importlib.import_module("octopus_tickets_sorter.main")


# --- slugify -----------------------------------------------------------------------


@pytest.mark.parametrize("text, slug", [
    ("Paweł Żak", "pawel-zak"),
    ("ŁÓDŹ", "lodz"),  # both cases of ł, which NFKD on its own drops rather than strips
    ("Krzycz, królowo! Mój prywatny koszmar", "krzycz-krolowo-moj-prywatny-koszmar"),
    ("Wypożyczalnia Kim's  Video / DVD", "wypozyczalnia-kim-s-video-dvd"),  # runs collapse to one dash
    ("  (Motor City)  ", "motor-city"),  # and the leading/trailing ones go
])
def test_slugify(text, slug):
    assert m.slugify(text) == slug


# --- parse_ticket ------------------------------------------------------------------


def test_parse_ticket_reads_every_field(make_pdf):
    assert m.parse_ticket(make_pdf()) == {
        "start_time": datetime(2026, 8, 11, 21, 30),
        "length_min": 103,
        "end_time": datetime(2026, 8, 11, 23, 13),
        "section": "Nocne Szaleństwo",
        "movie": "Motor City",
        "reservation_code": "ABC123",
        "ticket_code": "T-1234567",
        "address": "Kino Żak, Gdańsk, ul. Wajdeloty 12",  # venue line joined to the city line
        "issued_to": "Anna Nowak",
    }


def test_parse_ticket_without_a_known_runtime_has_no_end(make_pdf):
    ticket = m.parse_ticket(make_pdf(movie="Klub Nocny Marków", start="11.08.2026 21:30"))
    assert (ticket["length_min"], ticket["end_time"]) == (None, None)


def test_parse_ticket_rejoins_a_title_wrapped_over_several_lines(make_pdf):
    lines = conftest.ticket_lines()
    lines[-2:-1] = [  # the one title line, as the export wraps it
        "Nocne Szaleństwo | Krzycz, królowo! Mój prywatny",
        "koszmar na ulicy Wiązów",
    ]
    ticket = m.parse_ticket(make_pdf(lines=lines))
    assert ticket["movie"] == "Krzycz, królowo! Mój prywatny koszmar na ulicy Wiązów"
    assert ticket["length_min"] == 99  # only matches FILM_LENGTHS_MIN once rejoined


def test_parse_ticket_returns_none_without_a_date_line(make_pdf):
    assert m.parse_ticket(make_pdf(lines=["Bilet wstępu", "biletomat.pl", "Organizator"])) is None


# A receipt that strayed into input/ has a date line but no field block after it; without
# the length guard the fixed offsets read past the end and one stray PDF kills the run.
def test_parse_ticket_skips_a_date_line_without_a_ticket_block(make_pdf):
    assert m.parse_ticket(make_pdf(lines=["Faktura VAT", "11.08.2026 21:30", "razem 30 zł"])) is None


# --- write_csv ---------------------------------------------------------------------


def read_csv(path):
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.reader(f))


def test_write_csv_header_and_row(tmp_path, make_ticket):
    path = tmp_path / "tickets.csv"
    m.write_csv(path, [make_ticket(file="tickets/anna-nowak/motor-city-ABC123.pdf")])
    header, row = read_csv(path)
    assert header == [
        "start_time", "end_time", "length_min", "movie", "issued_to", "address",
        "section", "reservation_code", "ticket_code", "file",
    ]
    assert row == [
        "2026-08-11 21:30", "2026-08-11 23:13", "103", "Motor City", "Anna Nowak",
        "Kino Żak, Gdańsk, ul. Wajdeloty 12", "Nocne Szaleństwo", "ABC123", "T-1234567",
        "tickets/anna-nowak/motor-city-ABC123.pdf",
    ]


def test_write_csv_blanks_the_unknown_runtime(tmp_path, make_ticket):
    path = tmp_path / "tickets.csv"
    m.write_csv(path, [make_ticket(length_min=None, file="duplicates/x.pdf")])
    assert read_csv(path)[1][:3] == ["2026-08-11 21:30", "", ""]


# --- parse_args --------------------------------------------------------------------


@pytest.fixture
def argv(monkeypatch, tmp_path):
    """Run parse_args over a config of our own, so the repo's real config.toml — which
    turns both upload flags on — never leaks in as the default."""
    config = tmp_path / "config.toml"

    def run(*flags):
        monkeypatch.setattr(sys, "argv", ["octopus-tickets-sorter", "--config", str(config), *flags])
        return m.parse_args()

    return SimpleNamespace(run=run, config=config)


@pytest.mark.parametrize("config_text, expected", [
    ("", (False, False)),
    ("upload_drive = true\ncreate_calendar = true\n", (True, True)),
])
def test_parse_args_takes_the_flag_defaults_from_the_config(argv, config_text, expected):
    argv.config.write_text(config_text, encoding="utf-8")
    args = argv.run()
    assert (args.upload_drive, args.create_calendar) == expected


def test_parse_args_rejects_a_calendar_without_drive(argv, capsys):
    with pytest.raises(SystemExit):
        argv.run("--create-calendar")
    assert "--create-calendar requires --upload-drive" in capsys.readouterr().err


# --- main --------------------------------------------------------------------------


@pytest.fixture
def sorter(tmp_path, monkeypatch):
    """A full run against tmp_path, with both upload flags left off."""
    paths = SimpleNamespace(
        input=tmp_path / "input",
        output=tmp_path / "output",
        config=tmp_path / "config.toml",
    )
    paths.input.mkdir()
    monkeypatch.setattr(m, "INPUT_DIR", paths.input)
    monkeypatch.setattr(m, "OUTPUT_DIR", paths.output)
    monkeypatch.setattr(m, "TICKETS_DIR", paths.output / "tickets")
    monkeypatch.setattr(m, "DUPLICATES_DIR", paths.output / "duplicates")

    def run(*flags):
        monkeypatch.setattr(sys, "argv", ["octopus-tickets-sorter", "--config", str(paths.config), *flags])
        m.main()

    paths.run = run
    return paths


def names_in(directory):
    return sorted(p.name for p in directory.iterdir()) if directory.exists() else []


def written_config(sorter):
    return tomllib.loads(sorter.config.read_text(encoding="utf-8"))


def test_main_files_each_ticket_under_its_recipient(sorter, make_pdf):
    make_pdf("1.pdf", into=sorter.input)
    make_pdf("2.pdf", into=sorter.input, issued_to="Paweł Żak", reservation_code="B7XQZ1",
             movie="Matrix", start="12.08.2026 18:00")
    sorter.run()

    assert names_in(sorter.output / "tickets") == ["pawel-zak", "anna-nowak"]
    assert names_in(sorter.output / "tickets" / "anna-nowak") == [
        "motor-city-ABC123__anna-nowak.pdf"
    ]
    assert names_in(sorter.output / "tickets" / "pawel-zak") == [
        "matrix-B7XQZ1__pawel-zak.pdf"
    ]
    assert names_in(sorter.output / "duplicates") == []


def test_main_files_a_repeat_of_the_same_show_as_a_duplicate(sorter, make_pdf):
    make_pdf("1.pdf", into=sorter.input)
    make_pdf("2.pdf", into=sorter.input, reservation_code="B7XQZ1")
    sorter.run()

    assert names_in(sorter.output / "tickets" / "anna-nowak") == [
        "motor-city-ABC123__anna-nowak.pdf"  # the first one keeps the slot
    ]
    assert names_in(sorter.output / "duplicates") == ["motor-city-B7XQZ1.pdf"]


def test_main_does_not_call_two_people_at_one_showing_duplicates(sorter, make_pdf):
    make_pdf("1.pdf", into=sorter.input)
    make_pdf("2.pdf", into=sorter.input, issued_to="Jan Nowak", reservation_code="B7XQZ1")
    sorter.run()

    assert names_in(sorter.output / "tickets") == ["jan-nowak", "anna-nowak"]
    assert names_in(sorter.output / "duplicates") == []


def test_main_hands_back_a_ticket_marked_in_the_config(sorter, make_pdf):
    sorter.config.write_text('[duplicates]\n"ABC123" = "handed back"\n', encoding="utf-8")
    make_pdf("1.pdf", into=sorter.input)  # the only ticket for this show, marked anyway
    sorter.run()

    assert names_in(sorter.output / "tickets") == []
    assert names_in(sorter.output / "duplicates") == ["motor-city-ABC123.pdf"]


def test_main_leaves_the_slot_open_for_a_marked_shows_other_ticket(sorter, make_pdf):
    sorter.config.write_text('[duplicates]\n"ABC123" = "handed back"\n', encoding="utf-8")
    make_pdf("1.pdf", into=sorter.input)
    make_pdf("2.pdf", into=sorter.input, reservation_code="B7XQZ1")
    sorter.run()

    assert names_in(sorter.output / "tickets" / "anna-nowak") == [
        "motor-city-B7XQZ1__anna-nowak.pdf"
    ]
    assert names_in(sorter.output / "duplicates") == ["motor-city-ABC123.pdf"]


def test_main_rewrites_the_duplicate_marks_it_keeps(sorter, make_pdf):
    sorter.config.write_text('name = "Octopus"\n\n[duplicates]\n"ABC123" = "handed back"\n', encoding="utf-8")
    make_pdf("1.pdf", into=sorter.input)
    sorter.run()

    # the hand-back survives the run, relabelled with what it actually is
    assert written_config(sorter) == {
        "name": "Octopus",  # and everything above the tables is left alone
        "duplicates": {"ABC123": "Anna Nowak – Motor City, 2026-08-11 21:30"},
    }
    text = sorter.config.read_text(encoding="utf-8")
    sorter.run()
    assert sorter.config.read_text(encoding="utf-8") == text  # a second run must not churn the file


def test_main_untick_on_the_sheet_beats_a_stored_mark(sorter, make_pdf, monkeypatch):
    from octopus_tickets_sorter import drive_upload

    sorter.config.write_text('name = "Octopus"\n\n[duplicates]\n"ABC123" = "handed back"\n', encoding="utf-8")
    make_pdf("1.pdf", into=sorter.input)
    make_pdf("2.pdf", into=sorter.input, reservation_code="B7XQZ1", movie="Matrix", start="12.08.2026 18:00")
    # B7XQZ1 ticked on the Tickets tab, ABC123 unticked on the Duplicates tab: the way back
    monkeypatch.setattr(drive_upload, "read_sheet_marks", lambda *a: ({"B7XQZ1"}, {"ABC123"}))
    monkeypatch.setattr(drive_upload, "upload_to_drive", lambda *a: ("f", "s"))
    sorter.run("--upload-drive")

    assert names_in(sorter.output / "tickets" / "anna-nowak") == [
        "motor-city-ABC123__anna-nowak.pdf"
    ]
    assert names_in(sorter.output / "duplicates") == ["matrix-B7XQZ1.pdf"]
    assert written_config(sorter)["duplicates"] == {
        "B7XQZ1": "Anna Nowak – Matrix, 2026-08-12 18:00"
    }


def test_main_merge_names_files_both_spellings_under_one_recipient(sorter, make_pdf, monkeypatch):
    make_pdf("1.pdf", into=sorter.input)
    make_pdf("2.pdf", into=sorter.input, issued_to="Ania", reservation_code="B7XQZ1", movie="Matrix")
    monkeypatch.setattr(sys, "stdin", io.StringIO("1 2\n1\n"))  # merge both, keep the first, then EOF
    sorter.run("--merge-names")

    assert names_in(sorter.output / "tickets") == ["anna-nowak"]
    assert names_in(sorter.output / "tickets" / "anna-nowak") == [
        "matrix-B7XQZ1__anna-nowak.pdf",
        "motor-city-ABC123__anna-nowak.pdf",
    ]
    assert written_config(sorter) == {"aliases": {"Ania": "Anna Nowak"}}


def test_main_collapses_an_alias_into_one_recipient(sorter, make_pdf):
    sorter.config.write_text('[aliases]\n"Ania" = "Anna Nowak"\n', encoding="utf-8")
    make_pdf("1.pdf", into=sorter.input, issued_to="Ania", reservation_code="B7XQZ1", movie="Matrix")
    make_pdf("2.pdf", into=sorter.input)
    sorter.run()

    assert names_in(sorter.output / "tickets") == ["anna-nowak"]
    assert names_in(sorter.output / "tickets" / "anna-nowak") == [
        "matrix-B7XQZ1__anna-nowak.pdf",
        "motor-city-ABC123__anna-nowak.pdf",
    ]


def test_main_skips_an_unrecognised_layout(sorter, make_pdf, capsys):
    make_pdf("good.pdf", into=sorter.input)
    make_pdf("junk.pdf", into=sorter.input, lines=["Bilet wstępu", "nothing else useful"])
    sorter.run()

    assert "skip (unrecognized layout): junk.pdf" in capsys.readouterr().err
    assert len(read_csv(sorter.output / "tickets.csv")) == 2  # header plus the one good ticket


def test_main_rename_input_renames_the_source_in_place(sorter, make_pdf):
    make_pdf("bilety.pdf", into=sorter.input)
    sorter.run("--rename-input")
    assert names_in(sorter.input) == ["motor-city-ABC123__anna-nowak.pdf"]


def test_main_skips_a_copy_whose_target_is_already_there(sorter, make_pdf, capsys):
    for name in ("1.pdf", "2.pdf", "3.pdf"):
        make_pdf(name, into=sorter.input)  # the same ticket downloaded three times
    sorter.run("--rename-input")

    # both duplicate rows name the one file that got copied, and the third source keeps its name
    assert names_in(sorter.output / "duplicates") == ["motor-city-ABC123.pdf"]
    assert [row[-1] for row in read_csv(sorter.output / "duplicates.csv")[1:]] == [
        "duplicates/motor-city-ABC123.pdf"
    ] * 2
    assert names_in(sorter.input) == [
        "3.pdf", "motor-city-ABC123.pdf", "motor-city-ABC123__anna-nowak.pdf"
    ]
    err = capsys.readouterr().err
    assert "skip copy (target exists): motor-city-ABC123.pdf" in err
    assert "skip rename (target exists): motor-city-ABC123.pdf" in err


def test_main_writes_both_csvs_sorted_by_start_time(sorter, make_pdf):
    late = {"movie": "Matrix", "start": "12.08.2026 18:00"}
    make_pdf("1.pdf", into=sorter.input, reservation_code="LATE01", **late)
    make_pdf("2.pdf", into=sorter.input, reservation_code="EARLY1")  # Motor City, 11.08 21:30
    make_pdf("3.pdf", into=sorter.input, reservation_code="LATE02", **late)  # repeats of both shows
    make_pdf("4.pdf", into=sorter.input, reservation_code="EARLY2")
    sorter.run()

    tickets = read_csv(sorter.output / "tickets.csv")[1:]
    assert [(row[0], row[7]) for row in tickets] == [
        ("2026-08-11 21:30", "EARLY1"), ("2026-08-12 18:00", "LATE01"),
    ]
    assert tickets[0][-1] == "tickets/anna-nowak/motor-city-EARLY1__anna-nowak.pdf"

    duplicates = read_csv(sorter.output / "duplicates.csv")[1:]
    assert [(row[0], row[7]) for row in duplicates] == [
        ("2026-08-11 21:30", "EARLY2"), ("2026-08-12 18:00", "LATE02"),
    ]


def test_main_wipes_the_output_directory_first(sorter, make_pdf):
    stray = sorter.output / "tickets" / "kto-to" / "stale.pdf"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"%PDF-stale")
    make_pdf("1.pdf", into=sorter.input)
    sorter.run()

    assert not stray.exists()
    assert names_in(sorter.output / "tickets") == ["anna-nowak"]
