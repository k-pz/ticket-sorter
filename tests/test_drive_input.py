"""Taking a run's input from Drive: what gets downloaded, where it lands, and what a run
that would sort nothing does instead."""
from pathlib import Path

import pytest
from conftest import bodies, uris

from ticket_sorter import drive_input as di
from ticket_sorter import drive_upload as du

CREDS = (Path("creds.json"), Path("token.json"))  # never opened: fake_google patches the auth away

FOLDERS = [{"files": [{"id": "ROOT"}]}, {"files": [{"id": "DROP"}]}]  # the two lookups every read starts with


def entry(name: str, file_id: str = "F1", mime: str = di.PDF_MIME) -> dict:
    return {"id": file_id, "name": name, "mimeType": mime}


def file_ids(http) -> list[str]:
    """The file each request was about, for the ones that name one in their URI."""
    return [uri.split("/files/")[1].split("?")[0] for uri in uris(http) if "/files/" in uri]


# --- naming a download -------------------------------------------------------------


@pytest.mark.parametrize("drive_name, expected", [
    ("bilety(3).pdf", "bilety(3).pdf"),
    ("2026/08/11 bilety.pdf", "2026-08-11 bilety.pdf"),  # "/" is an ordinary character in Drive
    ("..", "ticket.pdf"),  # and so is a name that would be a way out of the directory here
])
def test_local_name(drive_name, expected):
    assert di._local_name(drive_name) == expected


# --- download_unsorted -------------------------------------------------------------


def test_download_unsorted(fake_google, tmp_path):
    http = fake_google(di, [
        *FOLDERS,
        {"files": [entry("bilety.pdf", "F1"), entry("bilety.pdf", "F2"), entry("Faktura", "F3", "text/plain")]},
        "%PDF-first", "%PDF-second",
    ])
    dest = tmp_path / "unsorted"
    downloaded = di.download_unsorted("Octopus", dest, *CREDS)

    # Two Drive files may share a name; two files in a directory may not, so the second moves.
    assert {path.name: f["id"] for path, f in downloaded.items()} == {"bilety.pdf": "F1", "bilety-2.pdf": "F2"}
    assert (dest / "bilety.pdf").read_bytes() == b"%PDF-first"
    assert (dest / "bilety-2.pdf").read_bytes() == b"%PDF-second"
    assert file_ids(http) == ["F1", "F2"]  # the third isn't a PDF, and isn't fetched
    assert all("alt=media" in uri for uri in uris(http)[3:])


def test_download_unsorted_says_what_it_skipped(fake_google, tmp_path, capsys):
    fake_google(di, [*FOLDERS, {"files": [entry("bilety.pdf"), entry("notes", "F2", "text/plain")]}, "%PDF-x"])
    di.download_unsorted("Octopus", tmp_path / "unsorted", *CREDS)
    assert "skip (not a PDF): notes" in capsys.readouterr().err


def test_download_unsorted_starts_from_an_empty_directory(fake_google, tmp_path):
    dest = tmp_path / "unsorted"
    dest.mkdir()
    (dest / "last-run.pdf").write_bytes(b"%PDF-stale")
    fake_google(di, [*FOLDERS, {"files": [entry("bilety.pdf")]}, "%PDF-x"])

    di.download_unsorted("Octopus", dest, *CREDS)
    assert sorted(p.name for p in dest.iterdir()) == ["bilety.pdf"]


def test_download_unsorted_leaves_the_drive_originals_alone(fake_google, tmp_path):
    http = fake_google(di, [*FOLDERS, {"files": [entry("bilety.pdf")]}, "%PDF-x"])
    di.download_unsorted("Octopus", tmp_path / "unsorted", *CREDS)
    # Everything sent is a GET: the folder is where tickets get dropped, not a queue to drain.
    assert {method for _uri, method, *_ in http.request_sequence} == {"GET"}


@pytest.mark.parametrize("responses, missing", [
    ([{"files": []}], True),                        # no folder of that name in Drive at all
    ([{"files": [{"id": "ROOT"}]}, {"files": []}], True),  # a folder, but nothing dropped in it
    ([*FOLDERS, {"files": [entry("notes", "F1", "text/plain")]}], False),  # nothing that is a PDF
])
def test_download_unsorted_refuses_to_sort_nothing(fake_google, tmp_path, responses, missing):
    """Sorting no tickets isn't a quiet no-op: every later step syncs, so it would take an
    earlier run's tickets, rows and events back out of Drive."""
    fake_google(di, responses)
    dest = tmp_path / "unsorted"
    with pytest.raises(RuntimeError, match="unsorted" if missing else "sort nothing"):
        di.download_unsorted("Octopus", dest, *CREDS)
    assert not dest.exists()  # and it says so before wiping anything


# --- download_config ---------------------------------------------------------------


def test_download_config_replaces_the_local_file(fake_google, tmp_path):
    http = fake_google(di, [{"files": [{"id": "ROOT"}]}, {"files": [{"id": "CFG"}]}, 'name = "Octopus"\n'])
    config = tmp_path / "config.toml"
    config.write_text("name = 'stale'\n")

    assert di.download_config("Octopus", config, *CREDS) is True
    assert config.read_text() == 'name = "Octopus"\n'
    assert file_ids(http) == ["CFG"]


def test_download_config_without_one_in_drive_yet(fake_google, tmp_path):
    fake_google(di, [{"files": [{"id": "ROOT"}]}, {"files": []}])
    config = tmp_path / "config.toml"
    config.write_text("name = 'local'\n")

    assert di.download_config("Octopus", config, *CREDS) is False
    assert config.read_text() == "name = 'local'\n"  # the local file stands, and is uploaded later


# --- rename_unsorted ---------------------------------------------------------------


def test_rename_unsorted(fake_google):
    http = fake_google(di, [{}, {}])
    di.rename_unsorted({"F1": "matrix-ABC123__anna-nowak.pdf", "F2": "motor-city-B7XQZ1.pdf"}, *CREDS)

    assert file_ids(http) == ["F1", "F2"]
    assert bodies(http) == [{"name": "matrix-ABC123__anna-nowak.pdf"}, {"name": "motor-city-B7XQZ1.pdf"}]


def test_rename_unsorted_with_nothing_to_rename_asks_for_nothing(fake_google):
    http = fake_google(di, [])
    di.rename_unsorted({}, *CREDS)
    assert http.request_sequence == []


# --- the layout both halves agree on -----------------------------------------------


def test_the_folder_read_from_is_the_one_the_upload_keeps():
    """download_unsorted reads a folder that upload_to_drive would otherwise trash for not
    being its own output, so the two have to mean the same folder."""
    assert du.UNSORTED in du.KEPT_IN_ROOT
    assert du.CONFIG_NAME in du.KEPT_IN_ROOT
