"""Drive/Sheets syncing: the pure decisions, the retry policy, then the requests sent."""
import json
from pathlib import Path

import httplib2
import pytest
from conftest import bodies, uris
from googleapiclient.errors import HttpError

from ticket_sorter import drive_upload as du

CREDS = (Path("creds.json"), Path("token.json"))  # never opened: fake_google patches the auth away


def service(fake_google, responses, api="drive", version="v3"):
    http = fake_google(du, responses)  # patches du.build, so the service must be built after
    return du.build(api, version, credentials=None), http


def error_body(status: int, reason: str) -> str:
    """What Google puts in the body of a throttled (or otherwise refused) call."""
    return json.dumps({"error": {"errors": [{"reason": reason}], "code": status}})


def sheet_row(code: str, mark: str) -> list:
    row = [""] * len(du.HEADER)
    row[du.DUPLICATE_COL], row[du.CODE_COL] = mark, code
    return row


# --- failure classification --------------------------------------------------------


@pytest.mark.parametrize("status, content, expected", [
    (429, error_body(429, "rateLimitExceeded"), True),
    (500, error_body(500, "backendError"), True),
    (503, error_body(503, "backendError"), True),
    (403, error_body(403, "rateLimitExceeded"), True),
    (403, error_body(403, "userRateLimitExceeded"), True),
    (403, error_body(403, "insufficientPermissions"), False),  # 403 also covers what retrying won't fix
    (404, error_body(404, "notFound"), False),
    (403, '{"error": {"code": 500}}', False),      # no machine-readable reason given
    (403, "<html>502 Bad Gateway</html>", False),  # nor a JSON body to look for one in
])
def test_retryable(status, content, expected):
    assert du._retryable(HttpError(httplib2.Response({"status": status}), content.encode())) is expected


# --- execute -----------------------------------------------------------------------


def listing(fake_google, responses):
    """A real Drive request to re-send, so the HttpError comes from googleapiclient."""
    drive, http = service(fake_google, responses)
    return drive.files().list(q="trashed = false", fields="files(id)"), http


def test_execute_retries_what_is_about_pace(fake_google, capsys):
    request, http = listing(fake_google, [(429, error_body(429, "rateLimitExceeded")), {"files": []}])
    assert du.execute(request, "listing") == {"files": []}
    assert len(http.request_sequence) == 2
    assert f"attempt 2/{du.MAX_ATTEMPTS}" in capsys.readouterr().err


def test_execute_raises_the_rest_at_once(fake_google):
    # The unused 200 is the point: a second attempt would have worked, and none is made.
    request, http = listing(fake_google, [(404, error_body(404, "notFound")), {"files": []}])
    with pytest.raises(HttpError):
        du.execute(request, "listing")
    assert len(http.request_sequence) == 1


def test_execute_turns_a_used_up_quota_into_advice(fake_google):
    request, http = listing(fake_google, [(403, error_body(403, "quotaExceeded")), {"files": []}])
    with pytest.raises(RuntimeError, match="Re-run later"):
        du.execute(request, "calendar create")
    assert len(http.request_sequence) == 1


def test_execute_gives_up_after_max_attempts(fake_google):
    throttled = (429, error_body(429, "rateLimitExceeded"))
    request, http = listing(fake_google, [throttled] * du.MAX_ATTEMPTS)
    with pytest.raises(HttpError):
        du.execute(request, "always throttled")
    assert len(http.request_sequence) == du.MAX_ATTEMPTS


# --- credentials -------------------------------------------------------------------


def token_file(tmp_path, expiry: str) -> Path:
    """A cached authorized-user token, expiring whenever the caller says."""
    path = tmp_path / "token.json"
    path.write_text(json.dumps({
        "token": "cached", "refresh_token": "r", "client_id": "c", "client_secret": "s",
        "scopes": du.SCOPES, "expiry": expiry,
    }))
    return path


def test_get_credentials_without_a_client_secret_says_where_to_put_one(tmp_path):
    with pytest.raises(FileNotFoundError, match=r"credentials\.json"):
        du.get_credentials(tmp_path / "credentials.json", tmp_path / "token.json")


def test_get_credentials_reuses_a_token_that_is_still_valid(tmp_path):
    token = token_file(tmp_path, "2099-01-01T00:00:00Z")
    before = token.read_text()
    # there is no credentials.json to fall back on, so any other branch would raise
    assert du.get_credentials(tmp_path / "credentials.json", token).token == "cached"
    assert token.read_text() == before  # and the token file is left as it was


def test_get_credentials_refreshes_an_expired_token_and_saves_it(tmp_path, monkeypatch, capsys):
    token = token_file(tmp_path, "2000-01-01T00:00:00Z")
    # all that matters here is that a refresh happened; its transport isn't ours to test
    monkeypatch.setattr(du.Credentials, "refresh", lambda self, _request: setattr(self, "token", "fresh"))
    assert du.get_credentials(tmp_path / "credentials.json", token).token == "fresh"
    assert json.loads(token.read_text())["token"] == "fresh"  # written back, so next run reuses it
    assert "Refreshing" in capsys.readouterr().out


# --- the Sheet's tables ------------------------------------------------------------


def test_marks_from_values_reads_each_direction():
    tab = [
        du.HEADER,
        sheet_row("AAA111", "TRUE"),
        sheet_row("BBB222", "FALSE"),
        sheet_row("CCC333", ""),
        ["2026-08-12 18:00"],  # a row Sheets trimmed to its last non-empty cell
    ]
    assert du._marks_from_values(tab, True) == {"AAA111"}
    assert du._marks_from_values(tab, False) == {"BBB222", "CCC333"}
    assert du._marks_from_values([], True) == set()


def test_marks_from_values_ignores_a_sheet_without_the_column():
    old = [[c for c in du.HEADER if c != "duplicate"], ["x"] * (len(du.HEADER) - 1)]
    assert du._marks_from_values(old, True) == set()
    assert du._marks_from_values(old, False) == set()


@pytest.mark.parametrize("length_min, is_duplicate, head", [
    (103, False, ["2026-08-11 21:30", "2026-08-11 23:13", 103, "FALSE"]),
    (None, True, ["2026-08-11 21:30", "", "", "TRUE"]),  # an unknown runtime blanks end_time and length
])
def test_row_values(make_ticket, length_min, is_duplicate, head):
    assert du._row_values(make_ticket(length_min=length_min), "https://d/x", is_duplicate) == [
        *head,
        "Motor City", "Anna Nowak", "Kino Żak, Gdańsk, ul. Wajdeloty 12",
        "Nocne Szaleństwo", "ABC123", "T-1234567", '=HYPERLINK("https://d/x", "Open ticket")',
    ]


def test_write_table_makes_the_duplicate_column_tickable(fake_google, make_ticket):
    sheets, http = service(fake_google, [{}, {}], "sheets", "v4")  # values, then formatting
    du._write_table(sheets, "SS1", 0, "Tickets", [make_ticket(drive_link="https://d/x")], marked=False)

    requests = bodies(http)[1]["requests"]
    assert [r["setDataValidation"] for r in requests if "setDataValidation" in r] == [{
        "range": {
            "sheetId": 0, "startRowIndex": 1, "endRowIndex": 2,  # the header row is left out
            "startColumnIndex": du.DUPLICATE_COL, "endColumnIndex": du.DUPLICATE_COL + 1,
        },
        "rule": {"condition": {"type": "BOOLEAN"}, "strict": True},
    }]
    backgrounds = [r["repeatCell"]["range"]["startRowIndex"] for r in requests
                   if "backgroundColor" in r.get("repeatCell", {}).get("cell", {}).get("userEnteredFormat", {})]
    assert backgrounds == [1]  # one per row, and none over the header


def test_write_table_asks_for_no_checkbox_on_an_empty_table(fake_google):
    # a zero-height validation range is rejected outright, which would abort the run at the end
    sheets, http = service(fake_google, [{}, {}], "sheets", "v4")
    du._write_table(sheets, "SS1", 1, "Duplicates", [], marked=True)
    assert not [r for r in bodies(http)[1]["requests"] if "setDataValidation" in r]


# --- sharing -----------------------------------------------------------------------


@pytest.mark.parametrize("permissions, role, expected", [
    ([{"type": "anyone", "role": "reader"}], "reader", True),
    ([{"type": "anyone", "role": "reader"}], "writer", False),
    ([{"type": "anyone", "role": "writer"}], "writer", True),
    ([{"type": "user", "role": "reader"}], "reader", False),  # one named person, not by link
    ([], "reader", False),
    (None, "reader", False),  # the listing came back without the field
])
def test_link_shared_as(permissions, role, expected):
    assert du._link_shared_as(permissions, role) is expected


def test_share_listed_costs_nothing_when_already_link_shared(fake_google):
    drive, http = service(fake_google, [])
    assert du._share_listed(drive, {"id": "F1", "permissions": [{"type": "anyone", "role": "reader"}]}) is False
    assert http.request_sequence == []


# --- syncing a folder --------------------------------------------------------------


def test_sync_keeps_one_per_wanted_name_and_trashes_the_rest(fake_google):
    drive, http = service(fake_google, [
        {"files": [
            {"id": "1", "name": "a.pdf", "webViewLink": "L1"},
            {"id": "2", "name": "a.pdf", "webViewLink": "L2"},   # same-name leftover
            {"id": "3", "name": "gone.pdf", "webViewLink": "L3"},
        ]},
        {}, {},  # the two trashes
    ])
    keep = du._sync(drive, "folder", {"a.pdf", "not-uploaded-yet.pdf"})

    assert list(keep) == ["a.pdf"]
    assert keep["a.pdf"]["webViewLink"] == "L1"
    assert [u.split("/files/")[1].split("?")[0] for u in uris(http)[1:]] == ["2", "3"]
    assert bodies(http)[1:] == [{"trashed": True}] * 2


# --- read_sheet_marks --------------------------------------------------------------


@pytest.mark.parametrize("responses", [
    [{"files": []}],                        # no root folder at all
    [{"files": [{"id": "ROOT"}]}, {"files": []}],  # a root folder, but no Sheet in it
])
def test_read_sheet_marks_without_a_sheet(fake_google, responses):
    http = fake_google(du, responses)
    assert du.read_sheet_marks("Octopus", *CREDS) == (set(), set())
    assert len(http.request_sequence) == len(responses)


def test_read_sheet_marks(fake_google):
    http = fake_google(du, [
        {"files": [{"id": "ROOT"}]},  # root folder lookup
        {"files": [{"id": "SS1"}]},   # the Sheet inside it
        {"valueRanges": [
            {"values": [du.HEADER, sheet_row("AAA111", "TRUE"), sheet_row("BBB222", "FALSE")]},
            {"values": [du.HEADER, sheet_row("CCC333", "TRUE"), sheet_row("DDD444", "FALSE")]},
        ]},
    ])
    assert du.read_sheet_marks("Octopus", *CREDS) == ({"AAA111"}, {"DDD444"})
    assert "spreadsheets/SS1/values:batchGet" in uris(http)[2]


def test_read_sheet_marks_with_only_one_tab_returned(fake_google):
    # a Sheet whose Duplicates tab was never written comes back a valueRange short
    fake_google(du, [
        {"files": [{"id": "ROOT"}]},
        {"files": [{"id": "SS1"}]},
        {"valueRanges": [{"values": [du.HEADER, sheet_row("AAA111", "TRUE")]}]},
    ])
    assert du.read_sheet_marks("Octopus", *CREDS) == ({"AAA111"}, set())


# --- upload_to_drive ---------------------------------------------------------------


def test_upload_to_drive(fake_google, make_pdf, make_ticket, tmp_path):
    out = tmp_path / "out"
    make_pdf("motor-city.pdf", into=out / "anna-nowak")
    make_pdf("vice.pdf", into=out / "duplicates")
    (out / "tickets.csv").write_text("reservation_code\nABC123\n")
    (out / "duplicates.csv").write_text("reservation_code\nB7QQQ1\n")

    ticket = make_ticket(file="anna-nowak/motor-city.pdf")
    duplicate = make_ticket(movie="Vice", reservation_code="B7QQQ1", file="duplicates/vice.pdf")

    responses = [
        {"files": []},          # root folder lookup: not there yet
        {"id": "ROOT"},         # so create it
        {"files": []},          # sync the root: empty, so no Sheet and no CSVs to replace
        {"id": "TICKETS"},      # create tickets/
        {"id": "DUPES"},        # create duplicates/
        {"files": []},          # sync tickets/: no recipient folders yet
        {"id": "ANNA"},        # create hers
        {"files": []},          # sync hers: nothing uploaded yet
        {"id": "F1", "webViewLink": "https://drive/F1"},  # upload the PDF
        {},                     # share the upload by link
        {"files": [{"id": "D1", "name": "vice.pdf", "webViewLink": "https://drive/D1",
                    "permissions": [{"type": "user", "role": "owner"}]}]},  # sync duplicates/: already there
        {},                     # ...but not link-shared, so share it
        {}, {},                 # tickets.csv, duplicates.csv
        {"spreadsheetId": "SS1", "sheets": [  # create the Sheet
            {"properties": {"title": "Tickets", "sheetId": 0}},
            {"properties": {"title": "Duplicates", "sheetId": 1}},
        ]},
        {"parents": ["myDrive"]},  # where Drive put it
        {},                     # move it into the root folder
        {},                     # locale
        {}, {},                 # Tickets values, Tickets formatting
        {}, {},                 # Duplicates values, Duplicates formatting
        {"permissions": [{"id": "p1", "type": "user", "role": "owner"}]},  # Sheet not shared yet
        {},                     # share it for editing
    ]
    http = fake_google(du, responses)
    folder_url, sheet_url = du.upload_to_drive([ticket], [duplicate], out, "Octopus", *CREDS)

    assert (folder_url, sheet_url) == (
        "https://drive.google.com/drive/folders/ROOT",
        "https://docs.google.com/spreadsheets/d/SS1/edit",
    )
    assert len(http.request_sequence) == len(responses)  # every response used, none left over
    assert ticket["drive_link"] == "https://drive/F1"
    assert duplicate["drive_link"] == "https://drive/D1"  # reused rather than uploaded again

    shares = [(u.split("/files/")[1].split("/permissions")[0], b["role"])
              for u, b in zip(uris(http), bodies(http), strict=True) if b and b.get("type") == "anyone"]
    assert shares == [("F1", "reader"), ("D1", "reader"), ("SS1", "writer")]

    tables = {u.split("/values/")[1].split("?")[0]: b["values"]
              for u, b in zip(uris(http), bodies(http), strict=True) if b and "values" in b}
    assert list(tables) == ["Tickets%21A1", "Duplicates%21A1"]  # %21 is "!": both start top left
    tickets_tab, duplicates_tab = tables.values()
    assert tickets_tab[0] == du.HEADER
    assert tickets_tab[1][du.CODE_COL:] == ["ABC123", "T-1234567", '=HYPERLINK("https://drive/F1", "Open ticket")']
    assert tickets_tab[1][du.DUPLICATE_COL] == "FALSE"
    assert duplicates_tab[1][du.DUPLICATE_COL] == "TRUE"  # the whole Duplicates tab is ticked


def test_upload_to_drive_reuses_the_sheet_and_replaces_what_changed(fake_google, tmp_path):
    (tmp_path / "tickets.csv").write_text("reservation_code\n")
    (tmp_path / "duplicates.csv").write_text("reservation_code\n")
    responses = [
        {"files": [{"id": "ROOT"}]},  # root folder lookup: already there
        {"files": [  # and it holds last run's Sheet, folders and CSVs
            {"id": "SS1", "name": "Octopus", "mimeType": du.SHEET_MIME},
            {"id": "TICKETS", "name": "tickets", "mimeType": du.FOLDER_MIME},
            {"id": "DUPES", "name": "duplicates", "mimeType": du.FOLDER_MIME},
            {"id": "CSV1", "name": "tickets.csv"},
            {"id": "CSV2", "name": "duplicates.csv"},
        ]},
        {"files": [{"id": "OLD", "name": "ania", "mimeType": du.FOLDER_MIME}]},  # sync tickets/
        {},                            # nobody has tickets this run, so her folder goes
        {"files": []},                 # sync duplicates/: nothing to upload
        {}, {},                        # both CSVs, replaced in place
        {"sheets": [  # the Sheet's tabs, looked up instead of created
            {"properties": {"title": "Tickets", "sheetId": 0}},
            {"properties": {"title": "Duplicates", "sheetId": 11}},
        ]},
        {}, {},  # clear both tabs
        {},      # locale
        {}, {}, {}, {},  # both tables written and formatted
        {"permissions": [{"id": "p1", "type": "anyone", "role": "writer"}]},  # already shared
    ]
    http = fake_google(du, responses)
    _folder_url, sheet_url = du.upload_to_drive([], [], tmp_path, "Octopus", *CREDS)

    assert sheet_url == "https://docs.google.com/spreadsheets/d/SS1/edit"
    # Nothing created, nothing shared again: exactly the reuse path and no more.
    assert len(http.request_sequence) == len(responses)
    assert [b for b in bodies(http) if b and "name" in b] == []  # nothing named, so nothing created
    assert [u.split("/values/")[1] for u in uris(http) if ":clear" in u] == [
        "Tickets:clear?alt=json", "Duplicates:clear?alt=json"
    ]
    # The recipient folder is trashed, and the CSVs are overwritten (their bodies are the
    # files themselves, so they read back as None) rather than uploaded beside the old ones.
    assert [(u.split("/files/")[1].split("?")[0], b)
            for (u, method, *_), b in zip(http.request_sequence, bodies(http), strict=True) if method == "PATCH"
            ] == [("OLD", {"trashed": True}), ("CSV1", None), ("CSV2", None)]
