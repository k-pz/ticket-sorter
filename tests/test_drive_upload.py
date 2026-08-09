"""Drive/Sheets syncing: the pure decisions, the retry policy, then the requests sent."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote_plus, unquote_plus

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


def token_json(expiry: str, token: str = "cached") -> str:
    """A stored authorized-user token, expiring whenever the caller says."""
    return json.dumps({
        "token": token, "refresh_token": "r", "client_id": "c", "client_secret": "s",
        "scopes": du.SCOPES, "expiry": expiry,
    })


def token_file(tmp_path, expiry: str) -> Path:
    path = tmp_path / "token.json"
    path.write_text(token_json(expiry))
    return path


CLIENT_JSON = json.dumps({"installed": {  # what the Cloud project's Credentials page hands you
    "client_id": "c.apps.googleusercontent.com", "client_secret": "s",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
}})


@pytest.fixture
def terminal(monkeypatch):
    """A run with a terminal, i.e. one that can open a browser to authorize. pytest's
    captured stdin isn't one, which is what the headless path is tested through below."""
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))


def test_get_credentials_without_a_client_secret_says_where_to_put_one(tmp_path, terminal):
    with pytest.raises(FileNotFoundError, match=r"credentials\.json"):
        du.get_credentials(tmp_path / "credentials.json", tmp_path / "token.json")


def test_get_credentials_without_a_terminal_says_what_to_set_instead(tmp_path):
    # A GitHub Actions run with no token: the browser flow would hang forever unwatched.
    (tmp_path / "credentials.json").write_text("{}")
    with pytest.raises(RuntimeError, match=du.TOKEN_ENV):
        du.get_credentials(tmp_path / "credentials.json", tmp_path / "token.json")


def test_get_credentials_prefers_the_token_in_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(du.TOKEN_ENV, token_json("2099-01-01T00:00:00Z", token="from-env"))
    token = token_file(tmp_path, "2099-01-01T00:00:00Z")  # a token file too, and it loses
    assert du.get_credentials(tmp_path / "credentials.json", token).token == "from-env"


@pytest.mark.parametrize("value", ["not json at all", '{"token": "t"}'])  # nor a token, without the rest
def test_get_credentials_says_what_is_wrong_with_the_token_it_was_given(tmp_path, monkeypatch, value):
    monkeypatch.setenv(du.TOKEN_ENV, value)
    with pytest.raises(RuntimeError, match=f"^\\${du.TOKEN_ENV} is set"):
        du.get_credentials(tmp_path / "credentials.json", tmp_path / "token.json")


def test_get_credentials_does_not_write_back_a_token_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(du.TOKEN_ENV, token_json("2000-01-01T00:00:00Z", token="from-env"))
    monkeypatch.setattr(du.Credentials, "refresh", lambda self, _request: setattr(self, "token", "fresh"))
    token = tmp_path / "token.json"

    assert du.get_credentials(tmp_path / "credentials.json", token).token == "fresh"
    assert not token.exists()  # the refresh lasts the run; there is nowhere to keep it


def test_get_credentials_from_the_oauth_client_and_a_refresh_token(tmp_path, monkeypatch):
    """The Cloud project's own credentials, as the two secrets they're made of. They arrive
    with no access token at all rather than an expired one, so redeeming comes first."""
    monkeypatch.setenv(du.CLIENT_ENV, CLIENT_JSON)
    monkeypatch.setenv(du.REFRESH_ENV, "1//a-refresh-token")
    refreshed = []
    monkeypatch.setattr(du.Credentials, "refresh",
                        lambda self, _request: (refreshed.append(self), setattr(self, "token", "fresh")))
    token = tmp_path / "token.json"

    creds = du.get_credentials(tmp_path / "credentials.json", token)
    assert (creds.token, creds.refresh_token) == ("fresh", "1//a-refresh-token")
    assert (creds.client_id, creds.client_secret) == ("c.apps.googleusercontent.com", "s")
    assert len(refreshed) == 1
    assert not token.exists()  # from the environment, so there is nowhere to keep it


def test_a_refresh_token_redeems_for_a_usable_one():
    """The pair really is enough, checked by letting google-auth redeem it over a transport
    of ours rather than by taking its own refresh() out of the picture."""
    posted = {}

    def transport(url, method="POST", body=b"", headers=None, **kwargs):
        posted.update(url=url, body=body.decode())
        return SimpleNamespace(status=200, data=json.dumps({
            "access_token": "ya29.minted", "expires_in": 3599, "token_type": "Bearer",
            "scope": " ".join(du.SCOPES),
        }).encode())

    # No token_uri in this one, the way a client JSON trimmed down by hand would arrive.
    creds = du._from_refresh_token({"installed": {"client_id": "c", "client_secret": "s"}}, "1//a-refresh-token")
    assert creds.valid is False  # no access token yet, only the means to mint one

    creds.refresh(transport)
    assert (creds.token, creds.valid) == ("ya29.minted", True)
    assert posted["url"] == du.TOKEN_URI  # defaulted, since the client didn't say
    assert "grant_type=refresh_token" in posted["body"]
    assert "refresh_token=1%2F%2Fa-refresh-token" in posted["body"]
    for scope in du.SCOPES:  # all three asked for, so a token short of one fails here
        assert quote_plus(scope) in posted["body"]


def test_get_credentials_from_a_client_id_and_secret(tmp_path, monkeypatch):
    """The same client, as the two values the Cloud console has copy buttons for — which is
    all anyone setting this up from a phone can reasonably get into a secret."""
    monkeypatch.setenv(du.CLIENT_ID_ENV, "c.apps.googleusercontent.com")
    monkeypatch.setenv(du.CLIENT_SECRET_ENV, "s")
    monkeypatch.setenv(du.REFRESH_ENV, "1//a-refresh-token")
    monkeypatch.setattr(du.Credentials, "refresh", lambda self, _request: setattr(self, "token", "fresh"))

    creds = du.get_credentials(tmp_path / "credentials.json", tmp_path / "token.json")
    assert (creds.client_id, creds.client_secret) == ("c.apps.googleusercontent.com", "s")
    assert (creds.token, creds.refresh_token) == ("fresh", "1//a-refresh-token")


@pytest.mark.parametrize("half", ["TICKET_SORTER_CLIENT_ID", "TICKET_SORTER_CLIENT_SECRET"])
def test_get_credentials_will_not_take_half_a_client(tmp_path, monkeypatch, half):
    monkeypatch.setenv(half, "one-half")
    monkeypatch.setenv(du.REFRESH_ENV, "1//a-refresh-token")
    with pytest.raises(RuntimeError, match="only work as a pair"):
        du.get_credentials(tmp_path / "credentials.json", tmp_path / "token.json")


def test_a_client_id_and_secret_are_enough_to_authorize_with(tmp_path, terminal, monkeypatch):
    """from_client_config insists on auth_uri and token_uri, which a pasted id and secret
    don't come with — so the pair is filled out to Google's own endpoints, not left short."""
    monkeypatch.setenv(du.CLIENT_ID_ENV, "c.apps.googleusercontent.com")
    monkeypatch.setenv(du.CLIENT_SECRET_ENV, "s")
    flow = du.InstalledAppFlow.from_client_config(du._oauth_client(), du.SCOPES)  # the real one
    assert flow.client_config["client_id"] == "c.apps.googleusercontent.com"


def test_get_credentials_prefers_a_whole_token_to_the_client_pair(tmp_path, monkeypatch):
    monkeypatch.setenv(du.TOKEN_ENV, token_json("2099-01-01T00:00:00Z", token="from-the-token"))
    monkeypatch.setenv(du.CLIENT_ENV, CLIENT_JSON)
    monkeypatch.setenv(du.REFRESH_ENV, "1//a-refresh-token")
    assert du.get_credentials(tmp_path / "credentials.json", tmp_path / "token.json").token == "from-the-token"


@pytest.mark.parametrize("value, said", [
    ("not json at all", "isn't an OAuth client"),
    ('{"client_id": "c", "client_secret": "s"}', "no 'installed' or 'web' section"),  # unwrapped
])
def test_get_credentials_says_what_is_wrong_with_the_client_it_was_given(tmp_path, monkeypatch, value, said):
    monkeypatch.setenv(du.CLIENT_ENV, value)
    monkeypatch.setenv(du.REFRESH_ENV, "1//a-refresh-token")
    with pytest.raises(RuntimeError, match=said):
        du.get_credentials(tmp_path / "credentials.json", tmp_path / "token.json")


def test_authorizing_uses_the_client_from_the_environment(tmp_path, terminal, monkeypatch):
    """The client alone, with no refresh token to go with it, is still the client: a local
    run authorizes against it without credentials.json ever being on disk."""
    monkeypatch.setenv(du.CLIENT_ENV, CLIENT_JSON)
    authorized = SimpleNamespace(valid=True, to_json=lambda: '{"token": "new"}')
    seen = {}
    monkeypatch.setattr(du.InstalledAppFlow, "from_client_config", lambda config, scopes: (
        seen.update(config=config, scopes=scopes),
        SimpleNamespace(run_local_server=lambda port: authorized),
    )[1])
    token = tmp_path / "token.json"

    assert du.get_credentials(tmp_path / "credentials.json", token) is authorized
    assert seen == {"config": json.loads(CLIENT_JSON), "scopes": du.SCOPES}
    assert token.read_text() == '{"token": "new"}'  # this one is the run's own, so it is kept


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
    # The survivor is the first listed, so the listing has to be the one that puts the file
    # written last there — otherwise the copy a run keeps is whichever Drive felt like.
    assert f"orderBy={quote_plus(du.NEWEST_FIRST)}" in uris(http)[0]


def test_find_child_takes_the_newest_of_several_with_one_name(fake_google):
    """Drive lets a folder hold two files called config.toml. Which one a run reads decides
    whether it carries on from the last run's aliases or from a copy of them from before."""
    drive, http = service(fake_google, [{"files": [{"id": "NEW"}, {"id": "OLD"}]}])
    assert du.find_child(drive, "config.toml", None, "ROOT") == "NEW"
    assert f"orderBy={quote_plus(du.NEWEST_FIRST)}" in uris(http)[0]


def test_sync_keeps_what_no_run_uploads(fake_google):
    """The drop folder --input-drive reads from, and the config --config-drive keeps beside
    it, are in the root folder without being this run's output — and the drop folder holds
    the only copy of what's in it."""
    drive, http = service(fake_google, [{"files": [
        {"id": "1", "name": "unsorted", "mimeType": du.FOLDER_MIME},
        {"id": "2", "name": "config.toml"},
    ]}])
    assert set(du._sync(drive, "folder", {"tickets"} | du.KEPT_IN_ROOT)) == {"unsorted", "config.toml"}
    assert len(http.request_sequence) == 1  # the listing, and nothing trashed


# --- find_child --------------------------------------------------------------------


@pytest.mark.parametrize("name, quoted", [
    ("Octopus 2026", "Octopus 2026"),
    ("Kim's Video", r"Kim\'s Video"),  # an apostrophe would otherwise close the query's string
])
def test_find_child_quotes_the_name_it_looks_for(fake_google, name, quoted):
    drive, http = service(fake_google, [{"files": [{"id": "F1"}]}])
    assert du.find_child(drive, name, du.FOLDER_MIME, "ROOT") == "F1"
    assert f"name = '{quoted}'" in unquote_plus(uris(http)[0])


def test_find_child_without_a_mime_type_takes_whatever_is_named_that(fake_google):
    drive, http = service(fake_google, [{"files": []}])
    assert du.find_child(drive, "config.toml", None, "ROOT") is None
    assert "mimeType" not in unquote_plus(uris(http)[0])


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
    config = tmp_path / "config.toml"
    config.write_text('name = "Octopus"\n')
    responses = [
        {"files": [{"id": "ROOT"}]},  # root folder lookup: already there
        {"files": [  # and it holds last run's Sheet, folders, CSVs and config
            {"id": "SS1", "name": "Octopus", "mimeType": du.SHEET_MIME},
            {"id": "TICKETS", "name": "tickets", "mimeType": du.FOLDER_MIME},
            {"id": "DUPES", "name": "duplicates", "mimeType": du.FOLDER_MIME},
            {"id": "CSV1", "name": "tickets.csv"},
            {"id": "CSV2", "name": "duplicates.csv"},
            {"id": "CFG", "name": "config.toml"},
            {"id": "DROP", "name": "unsorted", "mimeType": du.FOLDER_MIME},  # what the next run reads
        ]},
        {"files": [{"id": "OLD", "name": "ania", "mimeType": du.FOLDER_MIME}]},  # sync tickets/
        {},                            # nobody has tickets this run, so her folder goes
        {"files": []},                 # sync duplicates/: nothing to upload
        {}, {}, {},                    # both CSVs and the config, replaced in place
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
    _folder_url, sheet_url = du.upload_to_drive([], [], tmp_path, "Octopus", *CREDS, config_path=config)

    assert sheet_url == "https://docs.google.com/spreadsheets/d/SS1/edit"
    # Nothing created, nothing shared again: exactly the reuse path and no more.
    assert len(http.request_sequence) == len(responses)
    assert [b for b in bodies(http) if b and "name" in b] == []  # nothing named, so nothing created
    assert [u.split("/values/")[1] for u in uris(http) if ":clear" in u] == [
        "Tickets:clear?alt=json", "Duplicates:clear?alt=json"
    ]
    # The recipient folder is trashed, and the CSVs and config are overwritten (their bodies
    # are the files themselves, so they read back as None) rather than left beside the old
    # ones. The drop folder this run didn't upload is not trashed for it: nothing else has it.
    assert [(u.split("/files/")[1].split("?")[0], b)
            for (u, method, *_), b in zip(http.request_sequence, bodies(http), strict=True) if method == "PATCH"
            ] == [("OLD", {"trashed": True}), ("CSV1", None), ("CSV2", None), ("CFG", None)]
