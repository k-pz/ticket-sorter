"""Upload sorted tickets to Google Drive and build a Sheet with linked tables."""
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from .colors import showing_colors, showing_key
from .main import slugify

MAX_ATTEMPTS = 7  # ~1+2+4+8+16+32s of waiting before giving up
MAX_BACKOFF_S = 32
# 403 covers several unrelated things; only these two mean "you're going too fast".
RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar",
]

# "duplicate" is a checkbox sitting right before the film, for handing a showing back
# from the Sheet the way the DUPLICATE prefix does from a calendar.
HEADER = [
    "start_time", "end_time", "length_min", "duplicate", "movie", "issued_to", "address",
    "section", "reservation_code", "ticket_code", "ticket_pdf",
]
DUPLICATE_COL = HEADER.index("duplicate")
CODE_COL = HEADER.index("reservation_code")

FOLDER_MIME = "application/vnd.google-apps.folder"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"


def _reason(error: HttpError) -> str:
    """The API's machine-readable reason for a failure, or "" if it didn't give one."""
    try:
        errors = json.loads(error.content.decode()).get("error", {}).get("errors", [])
        return errors[0].get("reason", "") if errors else ""
    except (ValueError, AttributeError, KeyError, IndexError):
        return ""


def _retryable(error: HttpError) -> bool:
    status = getattr(error.resp, "status", 0)
    return status == 429 or status >= 500 or (status == 403 and _reason(error) in RATE_LIMIT_REASONS)


def execute(request, what: str = "request"):
    """Send a Google API request, waiting out the rate limits a whole run runs into.
    Backs off exponentially with jitter, so a burst that gets throttled resumes instead
    of killing the run. Failures that aren't about pace are raised straight away —
    retrying those would just spend more of the same quota."""
    for attempt in range(MAX_ATTEMPTS):
        try:
            return request.execute()
        except HttpError as error:
            if _reason(error) == "quotaExceeded":
                raise RuntimeError(
                    f"Google turned down a {what}: the quota for it is used up. Unlike a rate "
                    "limit this doesn't clear in seconds — creating many calendars in one go is "
                    "the usual cause, and it frees up after a while. Re-run later; whatever was "
                    "already created is reused rather than made again."
                ) from error
            if attempt == MAX_ATTEMPTS - 1 or not _retryable(error):
                raise
            delay = min(2 ** attempt, MAX_BACKOFF_S) + random.uniform(0, 1)
            print(
                f"  rate limited on {what}, waiting {delay:.0f}s "
                f"(attempt {attempt + 2}/{MAX_ATTEMPTS})",
                file=sys.stderr,
            )
            time.sleep(delay)


def get_credentials(credentials_path: Path, token_path: Path) -> Credentials:
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("Refreshing Google auth token...")
            creds.refresh(Request())
        else:
            if not credentials_path.exists():
                raise FileNotFoundError(
                    f"{credentials_path} not found. Create a Google Cloud OAuth Desktop client "
                    "and save it there (see README.md)."
                )
            print("Opening browser to authorize with Google...")
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
    return creds


def _find(drive, name: str, mime_type: str, parent_id: str | None) -> str | None:
    q = f"name = '{name}' and mimeType = '{mime_type}' and trashed = false and '{parent_id or 'root'}' in parents"
    files = execute(drive.files().list(q=q, fields="files(id)", pageSize=1), "folder lookup").get("files", [])
    return files[0]["id"] if files else None


def _create_folder(drive, name: str, parent_id: str | None = None) -> str:
    body = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        body["parents"] = [parent_id]
    return execute(drive.files().create(body=body, fields="id"), "folder create")["id"]


def _find_or_create_folder(drive, name: str, parent_id: str | None = None) -> str:
    return _find(drive, name, FOLDER_MIME, parent_id) or _create_folder(drive, name, parent_id)


def _sync(drive, folder_id: str, wanted: set[str]) -> dict[str, dict]:
    """Trash whatever in folder_id isn't named in wanted; return the survivors by name."""
    q = f"'{folder_id}' in parents and trashed = false"
    keep: dict[str, dict] = {}
    # permissions comes along for the ride, so checking what's already link-shared
    # costs no request of its own.
    fields = "files(id,name,mimeType,webViewLink,permissions(type,role))"
    files = execute(drive.files().list(q=q, fields=fields, pageSize=1000), "folder listing")
    for f in files.get("files", []):
        if f["name"] in wanted and f["name"] not in keep:
            keep[f["name"]] = f
        else:
            print(f"  removing {f['name']}")
            execute(drive.files().update(fileId=f["id"], body={"trashed": True}), "trash")
    return keep


def _child_folder(drive, existing: dict, name: str, parent_id: str) -> str:
    entry = existing.get(name)
    return entry["id"] if entry else _create_folder(drive, name, parent_id)


def _link_shared_as(permissions, role: str) -> bool:
    return any(p.get("type") == "anyone" and p.get("role") == role for p in permissions or [])


def _link_share(drive, file_id: str, role: str, what: str) -> None:
    execute(drive.permissions().create(fileId=file_id, body={"role": role, "type": "anyone"}), what)


def _share_listed(drive, file: dict) -> bool:
    """Make a already-uploaded PDF readable by anyone holding its link, so the ticket
    links in the Sheet and in calendar events open for whoever they were sent to.
    The permissions came with the folder listing, so a file already shared costs
    nothing; a listing that omits them costs a redundant share rather than a dead link."""
    if _link_shared_as(file.get("permissions"), "reader"):
        return False
    _link_share(drive, file["id"], "reader", "file sharing")
    return True


def _upload_file(drive, path: Path, parent_id: str) -> str:
    media = MediaFileUpload(str(path), mimetype="application/pdf")
    body = {"name": path.name, "parents": [parent_id]}
    file = execute(drive.files().create(body=body, media_body=media, fields="id,webViewLink"), "file upload")
    _link_share(drive, file["id"], "reader", "file sharing")  # freshly created: never shared yet
    return file["webViewLink"]


def _share_sheet(drive, file_id: str) -> bool:
    """Let anyone with the Sheet's link edit it, and say whether that had to be changed.
    Drive, unlike Calendar, does allow write access on an anyone-with-the-link share.
    Read first, so a Sheet that is already shared costs no write."""
    listed = execute(drive.permissions().list(fileId=file_id, fields="permissions(id,type,role)"), "sheet sharing")
    if _link_shared_as(listed.get("permissions"), "writer"):
        return False
    _link_share(drive, file_id, "writer", "sheet sharing")
    return True


def _marks_from_values(values: list, want_ticked: bool) -> set[str]:
    """Reservation codes whose checkbox is (or isn't) ticked, read off a tab's own header
    row rather than HEADER, so a Sheet written before the column existed is simply
    ignored instead of misread."""
    if not values or not values[0]:
        return set()
    header = values[0]
    if "duplicate" not in header or "reservation_code" not in header:
        return set()
    box, code = header.index("duplicate"), header.index("reservation_code")
    found = set()
    for row in values[1:]:
        if len(row) <= max(box, code) or not row[code]:
            continue
        if (str(row[box]).strip().upper() == "TRUE") == want_ticked:
            found.add(row[code])
    return found


def read_sheet_marks(name: str, credentials_path: Path, token_path: Path) -> tuple[set[str], set[str]]:
    """Showings handed back, or taken back, with the Sheet's checkbox: codes ticked on the
    Tickets tab, and codes unticked on the Duplicates tab. Only those two directions count
    — a ticked row on Duplicates and an unticked one on Tickets are just their normal
    state, so reading them would undo whatever else this run decided."""
    creds = get_credentials(credentials_path, token_path)
    drive = build("drive", "v3", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)

    root_id = _find(drive, name, FOLDER_MIME, None)
    sheet_id = _find(drive, name, SHEET_MIME, root_id) if root_id else None
    if not sheet_id:
        return set(), set()

    grabbed = execute(
        sheets.spreadsheets().values().batchGet(spreadsheetId=sheet_id, ranges=["Tickets", "Duplicates"]),
        "sheet marks",
    )
    tabs = [r.get("values", []) for r in grabbed.get("valueRanges", [])]
    tickets_tab, duplicates_tab = (tabs + [[], []])[:2]
    return _marks_from_values(tickets_tab, True), _marks_from_values(duplicates_tab, False)


def _row_values(t: dict, drive_link: str, is_duplicate: bool) -> list:
    return [
        t["start_time"].strftime("%Y-%m-%d %H:%M"),
        t["end_time"].strftime("%Y-%m-%d %H:%M") if t["end_time"] else "",
        t["length_min"] or "",
        "TRUE" if is_duplicate else "FALSE",
        t["movie"],
        t["issued_to"],
        t["address"],
        t["section"],
        t["reservation_code"],
        t["ticket_code"],
        f'=HYPERLINK("{drive_link}", "Open ticket")',
    ]


def _write_table(sheets, spreadsheet_id: str, sheet_id: int, title: str, rows: list, marked: bool) -> None:
    values = [HEADER] + [_row_values(t, t["drive_link"], marked) for t in rows]
    execute(
        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{title}!A1",
            valueInputOption="USER_ENTERED",
            body={"values": values},
        ),
        f"{title} values",
    )

    num_rows, num_cols = len(values), len(HEADER)
    colors = showing_colors(rows)
    row_requests = [
        {"repeatCell": {
            "range": {
                "sheetId": sheet_id, "startRowIndex": i + 1, "endRowIndex": i + 2,
                "startColumnIndex": 0, "endColumnIndex": num_cols,
            },
            "cell": {"userEnteredFormat": {"backgroundColor": colors[showing_key(t)]}},
            "fields": "userEnteredFormat.backgroundColor",
        }}
        for i, t in enumerate(rows)
    ]
    # Turns the column's TRUE/FALSE into tickable boxes. Skipped when there are no rows,
    # since a zero-height range is rejected.
    checkbox_request = [
        {"setDataValidation": {
            "range": {
                "sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": num_rows,
                "startColumnIndex": DUPLICATE_COL, "endColumnIndex": DUPLICATE_COL + 1,
            },
            "rule": {"condition": {"type": "BOOLEAN"}, "strict": True},
        }}
    ] if num_rows > 1 else []
    formatting = sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {"repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat.bold",
                }},
                {"updateSheetProperties": {
                    "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount",
                }},
                {"setBasicFilter": {
                    "filter": {"range": {
                        "sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": num_rows,
                        "startColumnIndex": 0, "endColumnIndex": num_cols,
                    }},
                }},
                {"autoResizeDimensions": {
                    "dimensions": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": num_cols},
                }},
                *row_requests,
                *checkbox_request,
            ]
        },
    )
    execute(formatting, f"{title} formatting")


def upload_to_drive(
    rows: list, duplicate_rows: list, output_dir: Path, name: str, credentials_path: Path, token_path: Path
) -> tuple[str, str]:
    creds = get_credentials(credentials_path, token_path)
    drive = build("drive", "v3", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)

    root_id = _find_or_create_folder(drive, name)

    print(f"Syncing '{name}'...")
    root = _sync(drive, root_id, {"tickets", "duplicates", "tickets.csv", "duplicates.csv", name})
    sheet = root.get(name)
    sheet_id_existing = sheet["id"] if sheet and sheet["mimeType"] == SHEET_MIME else None

    tickets_id = _child_folder(drive, root, "tickets", root_id)
    duplicates_id = _child_folder(drive, root, "duplicates", root_id)

    by_slug: dict[str, list] = {}
    for t in rows:
        by_slug.setdefault(slugify(t["issued_to"]), []).append(t)

    total = len(rows) + len(duplicate_rows)
    print(f"Uploading {total} PDFs (skipping any already there)...")
    recipients = _sync(drive, tickets_id, set(by_slug))
    n, newly_shared = 0, 0
    for slug, tickets in by_slug.items():
        folder_id = _child_folder(drive, recipients, slug, tickets_id)
        existing = _sync(drive, folder_id, {Path(t["file"]).name for t in tickets})
        for t in tickets:
            n += 1
            found = existing.get(Path(t["file"]).name)
            print(f"  [{n}/{total}] {t['file']}{' (already there)' if found else ''}")
            if found:
                t["drive_link"] = found["webViewLink"]
                newly_shared += _share_listed(drive, found)
            else:
                t["drive_link"] = _upload_file(drive, output_dir / t["file"], folder_id)

    existing = _sync(drive, duplicates_id, {Path(t["file"]).name for t in duplicate_rows})
    for t in duplicate_rows:
        n += 1
        found = existing.get(Path(t["file"]).name)
        print(f"  [{n}/{total}] {t['file']} (duplicate{', already there' if found else ''})")
        if found:
            t["drive_link"] = found["webViewLink"]
            newly_shared += _share_listed(drive, found)
        else:
            t["drive_link"] = _upload_file(drive, output_dir / t["file"], duplicates_id)
    if newly_shared:
        print(f"  shared {newly_shared} already-uploaded PDFs by link")

    for csv_name in ("tickets.csv", "duplicates.csv"):
        csv_path = output_dir / csv_name
        if csv_path.exists():
            # Always re-uploaded, unlike the PDFs: same name, but the contents change every run.
            print(f"Uploading {csv_name}...")
            media = MediaFileUpload(str(csv_path), mimetype="text/csv")
            found = root.get(csv_name)
            if found:
                execute(drive.files().update(fileId=found["id"], media_body=media), "csv upload")
            else:
                execute(drive.files().create(body={"name": csv_name, "parents": [root_id]}, media_body=media), "csv upload")

    if sheet_id_existing:
        print(f"Reusing spreadsheet '{name}'...")
        spreadsheet_id = sheet_id_existing
        meta = execute(sheets.spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets.properties"), "sheet lookup")
        sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}
        for title in ("Tickets", "Duplicates"):
            execute(sheets.spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range=title), "sheet clear")
    else:
        print(f"Creating spreadsheet '{name}'...")
        created = execute(
            sheets.spreadsheets().create(
                body={
                    "properties": {"title": name},
                    "sheets": [{"properties": {"title": "Tickets"}}, {"properties": {"title": "Duplicates"}}],
                },
                fields="spreadsheetId,sheets.properties",
            ),
            "sheet create",
        )
        spreadsheet_id = created["spreadsheetId"]
        sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in created["sheets"]}

        existing_parents = execute(drive.files().get(fileId=spreadsheet_id, fields="parents"), "sheet parents")
        prev_parents = existing_parents.get("parents", [])
        execute(
            drive.files().update(
                fileId=spreadsheet_id, addParents=root_id, removeParents=",".join(prev_parents), fields="id,parents"
            ),
            "sheet move",
        )

    execute(
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"updateSpreadsheetProperties": {"properties": {"locale": "en_US"}, "fields": "locale"}}]},
        ),
        "sheet locale",
    )

    print(f"Writing Tickets table ({len(rows)} rows)...")
    _write_table(sheets, spreadsheet_id, sheet_ids["Tickets"], "Tickets", rows, marked=False)
    print(f"Writing Duplicates table ({len(duplicate_rows)} rows)...")
    _write_table(sheets, spreadsheet_id, sheet_ids["Duplicates"], "Duplicates", duplicate_rows, marked=True)

    if _share_sheet(drive, spreadsheet_id):
        print("Shared the Sheet: anyone with the link can edit")

    return f"https://drive.google.com/drive/folders/{root_id}", f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"


if __name__ == "__main__":
    # _sync keeps wanted files (with their links, so they can be skipped on
    # upload) and trashes everything else, including same-name leftovers.
    class _Exec:
        def __init__(self, value):
            self.value = value

        def execute(self):
            return self.value

    class _FakeFiles:
        def __init__(self, files):
            self.files, self.trashed = files, []

        def list(self, **kwargs):
            return _Exec({"files": self.files})

        def update(self, fileId, body):
            self.trashed.append(fileId)
            return _Exec({})

    class _FakeDrive:
        def __init__(self, files):
            self._files = _FakeFiles(files)

        def files(self):
            return self._files

    drive = _FakeDrive([
        {"id": "1", "name": "a.pdf", "webViewLink": "L1"},
        {"id": "2", "name": "a.pdf", "webViewLink": "L2"},
        {"id": "3", "name": "gone.pdf", "webViewLink": "L3"},
    ])
    keep = _sync(drive, "folder", {"a.pdf", "not-uploaded-yet.pdf"})
    assert list(keep) == ["a.pdf"], keep
    assert keep["a.pdf"]["webViewLink"] == "L1", keep
    assert drive.files().trashed == ["2", "3"], drive.files().trashed

    # _marks_from_values reads each tab's own header, and only in the direction asked for.
    tab = [
        HEADER,
        ["2026-08-11 16:30", "", "", "TRUE", "Motor City", "Ala", "", "", "AAA111", "", ""],
        ["2026-08-11 21:30", "", "", "FALSE", "Zoolander", "Ala", "", "", "BBB222", "", ""],
        ["2026-08-12 14:30", "", "", "", "Matrix", "Ala", "", "", "CCC333", "", ""],
        ["2026-08-12 18:00"],  # a row Sheets trimmed to its last non-empty cell
    ]
    assert _marks_from_values(tab, True) == {"AAA111"}, _marks_from_values(tab, True)
    assert _marks_from_values(tab, False) == {"BBB222", "CCC333"}, _marks_from_values(tab, False)
    assert _marks_from_values([], True) == set()
    # A Sheet written before the checkbox column existed is ignored, not misread.
    old = [[c for c in HEADER if c != "duplicate"], ["x"] * (len(HEADER) - 1)]
    assert _marks_from_values(old, True) == set()
    assert _marks_from_values(old, False) == set()

    # The checkbox sits immediately before the film, and reflects which tab it's on.
    assert HEADER[DUPLICATE_COL + 1] == "movie", HEADER
    ticket = {
        "start_time": datetime(2026, 8, 11, 21, 30), "end_time": None, "length_min": None,
        "movie": "Motor City", "issued_to": "Ala", "address": "Kino", "section": "S",
        "reservation_code": "ABC123", "ticket_code": "T",
    }
    assert _row_values(ticket, "https://d/x", False)[DUPLICATE_COL] == "FALSE"
    assert _row_values(ticket, "https://d/x", True)[DUPLICATE_COL] == "TRUE"
    assert _row_values(ticket, "https://d/x", True)[CODE_COL] == "ABC123"

    # Link sharing is per role: a PDF needs anyone/reader, the Sheet anyone/writer, and
    # a share with a named person is not a link share at all.
    assert _link_shared_as([{"type": "anyone", "role": "reader"}], "reader")
    assert not _link_shared_as([{"type": "anyone", "role": "reader"}], "writer")
    assert _link_shared_as([{"type": "anyone", "role": "writer"}], "writer")
    assert not _link_shared_as([{"type": "user", "role": "reader"}], "reader")
    assert not _link_shared_as([], "reader")
    assert not _link_shared_as(None, "reader")  # listing came back without the field

    # execute() retries what's worth retrying and gives up immediately on the rest.
    class _Resp:
        def __init__(self, status):
            self.status, self.reason = status, "test"

    def _error(status, reason):
        body = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()
        return HttpError(_Resp(status), body)

    class _Flaky:
        def __init__(self, errors):
            self.errors, self.calls = list(errors), 0

        def execute(self):
            self.calls += 1
            if self.errors:
                raise self.errors.pop(0)
            return "done"

    time.sleep = lambda _s: None  # the point is the retry logic, not the waiting

    for label, errors in [
        ("429", [_error(429, "rateLimitExceeded")]),
        ("403 rate limit", [_error(403, "userRateLimitExceeded")]),
        ("500", [_error(500, "backendError")]),
    ]:
        flaky = _Flaky(errors)
        assert execute(flaky, label) == "done", label
        assert flaky.calls == 2, (label, flaky.calls)

    for label, error in [
        ("404", _error(404, "notFound")),
        ("403 not about pace", _error(403, "insufficientPermissions")),
    ]:
        flaky = _Flaky([error])
        try:
            execute(flaky, label)
            raise AssertionError(f"{label} should not have been retried")
        except HttpError:
            assert flaky.calls == 1, (label, flaky.calls)

    # A used-up quota isn't a pace problem, so it fails at once and says what to do.
    flaky = _Flaky([_error(403, "quotaExceeded")])
    try:
        execute(flaky, "calendar create")
        raise AssertionError("quotaExceeded should have been raised")
    except RuntimeError as e:
        assert "Re-run later" in str(e), e
        assert flaky.calls == 1, flaky.calls

    # Giving up after MAX_ATTEMPTS rather than retrying forever.
    flaky = _Flaky([_error(429, "rateLimitExceeded")] * MAX_ATTEMPTS)
    try:
        execute(flaky, "always throttled")
        raise AssertionError("should have given up")
    except HttpError:
        assert flaky.calls == MAX_ATTEMPTS, flaky.calls
    print("ok")
