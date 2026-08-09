"""config.py: reading config.toml, prompting for the name, and rewriting the tables."""
import sys
import tomllib
from types import SimpleNamespace

import pytest

from ticket_sorter.config import (
    load_aliases,
    load_duplicates,
    load_flags,
    load_name,
    save_tables,
)


@pytest.fixture
def terminal(monkeypatch):
    """A run with a terminal to prompt on, which pytest's captured stdin is not."""
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))


@pytest.fixture
def config_path(tmp_path):
    """A config.toml that does not exist yet; tests that need one write it themselves."""
    return tmp_path / "config.toml"


def test_missing_file_reads_as_empty(config_path):
    assert load_flags(config_path) == {"upload_drive": False, "create_calendar": False, "input_drive": False}
    assert load_aliases(config_path) == {}
    assert load_duplicates(config_path) == {}


def test_reads_flags_and_tables_off_a_real_file(config_path):
    config_path.write_text(
        'name = "Octopus 2026 Tickets"\n'
        "upload_drive = true\n"
        "create_calendar = false\n"
        "input_drive = true\n"
        "\n"
        "[aliases]\n"
        '"Ania" = "Anna Nowak"\n'
        "\n"
        "[duplicates]\n"
        '"ABC123" = "Motor City"\n',
        encoding="utf-8",
    )
    assert load_flags(config_path) == {"upload_drive": True, "create_calendar": False, "input_drive": True}
    assert load_aliases(config_path) == {"Ania": "Anna Nowak"}
    assert load_duplicates(config_path) == {"ABC123": "Motor City"}


def test_load_name_returns_the_stored_name_without_prompting(config_path, monkeypatch):
    config_path.write_text('name = "Octopus 2026 Tickets"\n', encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("prompted anyway"))
    assert load_name(config_path) == "Octopus 2026 Tickets"


def test_load_name_without_a_terminal_to_ask_on_says_so(config_path):
    # what a GitHub Actions run hits if it neither passes --name nor has a config to read
    with pytest.raises(RuntimeError, match="no terminal"):
        load_name(config_path)


def test_load_name_prompts_until_answered_and_prepends(config_path, terminal, monkeypatch):
    config_path.write_text(
        'upload_drive = true\n\n[aliases]\n"Ania" = "Anna Nowak"\n', encoding="utf-8"
    )
    answers = iter(["", "   ", "  Octopus 2026 Tickets  "])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert load_name(config_path) == "Octopus 2026 Tickets"

    # Prepended, so the bare key still precedes the table it was written above.
    written = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert written == {
        "name": "Octopus 2026 Tickets",
        "upload_drive": True,
        "aliases": {"Ania": "Anna Nowak"},
    }


def test_load_name_creates_a_missing_file(config_path, terminal, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "Octopus 2026 Tickets")
    assert load_name(config_path) == "Octopus 2026 Tickets"
    assert tomllib.loads(config_path.read_text(encoding="utf-8")) == {"name": "Octopus 2026 Tickets"}


def test_save_tables_keeps_the_preamble_and_rewrites_both_tables(config_path):
    config_path.write_text(
        'name = "Octopus 2026 Tickets"\n'
        "upload_drive = true\n"
        "\n"
        "[aliases]\n"
        '"Stara" = "Nieaktualna"\n'
        "\n"
        "[duplicates]\n"
        '"OLD123" = "stary"\n',
        encoding="utf-8",
    )
    save_tables(
        config_path,
        {"Ania": "Anna Nowak", "Paweł": "Paweł Żak"},
        {"ABC123": "Motor City"},
    )

    text = config_path.read_text(encoding="utf-8")
    assert text.startswith('name = "Octopus 2026 Tickets"\nupload_drive = true\n\n[aliases]\n')
    assert "Stara" not in text and "OLD123" not in text
    assert text.index('"Ania"') < text.index('"Paweł"')  # sorted
    assert tomllib.loads(text) == {
        "name": "Octopus 2026 Tickets",
        "upload_drive": True,
        "aliases": {"Paweł": "Paweł Żak", "Ania": "Anna Nowak"},
        "duplicates": {"ABC123": "Motor City"},
    }


def test_save_tables_drops_an_empty_table(config_path):
    config_path.write_text('[aliases]\n"Ania" = "Anna"\n\n[duplicates]\n"ABC123" = "Motor City"\n', encoding="utf-8")
    save_tables(config_path, {"Ania": "Anna Nowak"}, {})
    text = config_path.read_text(encoding="utf-8")
    assert "[duplicates]" not in text
    assert tomllib.loads(text) == {"aliases": {"Ania": "Anna Nowak"}}


def test_save_tables_creates_the_file_and_escapes_names(config_path):
    quoted = 'Joe "Rocket" Doe\\Jr'
    save_tables(config_path, {quoted: "Joe Doe"}, {})
    assert tomllib.loads(config_path.read_text(encoding="utf-8")) == {
        "aliases": {quoted: "Joe Doe"}
    }


def test_save_tables_appends_to_a_file_with_no_tables_yet(config_path):
    config_path.write_text('name = "Octopus 2026 Tickets"\n', encoding="utf-8")
    save_tables(config_path, {}, {"ABC123": "Motor City"})
    assert config_path.read_text(encoding="utf-8") == (
        'name = "Octopus 2026 Tickets"\n\n[duplicates]\n"ABC123" = "Motor City"\n'
    )
