"""Upload sorted tickets to Google Drive and build a Sheet with linked tables."""
import json
import os
import random
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from .colors import showing_colors, showing_key
from .config import CLIENT_ENV, CLIENT_ID_ENV, CLIENT_SECRET_ENV, REFRESH_ENV, TOKEN_ENV
from .main import BY_HOLDER, BY_SHOWING, showing_slug, slugify

MAX_ATTEMPTS = 7  # ~1+2+4+8+16+32s of waiting before giving up
MAX_BACKOFF_S = 32
# 403 covers several unrelated things; only these two mean "you're going too fast".
RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}

# Whole-Drive rather than the narrower drive.file, which is what an app that only ever
# reads back its own uploads would ask for. This one starts by reading two things nobody
# here made: the `unsorted` folder, created by hand in Drive, and the ticket PDFs dropped
# into it. drive.file shows an app the files it created itself and nothing else — and it
# hides them by leaving them out of a listing rather than by refusing it, so a search for
# that folder comes back empty exactly as if it had never been created.
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar",
]

# Google's own endpoints, for a client given as an id and a secret rather than as the JSON
# those normally arrive wrapped in. Also the fallback for a JSON that leaves them out.
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"

UNSORTED = "unsorted"    # the folder in the Drive folder that --input-drive reads from
CONFIG_NAME = "config.toml"  # and the config a --config-drive run keeps beside it
# Left alone by the sync that trashes whatever a run didn't put in the Drive folder: neither
# is this run's output, and the drop folder in particular holds the only copy of its files.
KEPT_IN_ROOT = {UNSORTED, CONFIG_NAME}

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
# A file in Drive has one parent folder and no more — multi-parenting was withdrawn in
# 2020 — so the second way of grouping the tickets is folders of shortcuts pointing at the
# PDFs the first one holds. A shortcut costs one metadata write rather than an upload, and
# leaves one file per ticket to share by link instead of two.
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

# Drive lets a folder hold several files under one name, and orders a listing however it
# likes unless told. Both the lookup and the sync ask for newest first, so the one a run
# adopts is the one written last, and the ones it trashes are the older copies of it.
NEWEST_FIRST = "modifiedTime desc"


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


def _env_json(name: str, what: str) -> dict | None:
    """What an environment variable holding a file's contents parses to, if it's set. Said
    plainly rather than as a JSON traceback: what's in it was pasted into a secret."""
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError as error:
        raise RuntimeError(f"${name} is set, but isn't {what}: {error}") from error


def _oauth_client() -> dict | None:
    """The Cloud project's OAuth client, if the environment carries one, in either of the two
    shapes it comes in: the whole JSON the Credentials page hands you, "installed" wrapper and
    all, or the client id and secret on their own. The second is for setting this up from a
    phone, where those two have copy buttons next to them and the JSON is a file download."""
    client = _env_json(CLIENT_ENV, "an OAuth client's JSON")
    if client:
        if not (client.get("installed") or client.get("web")):
            raise RuntimeError(
                f"${CLIENT_ENV} is set, but holds JSON with no 'installed' or 'web' section: pass the "
                "whole file downloaded from the Cloud project's Credentials page, not a part of it."
            )
        return client

    client_id, client_secret = os.environ.get(CLIENT_ID_ENV), os.environ.get(CLIENT_SECRET_ENV)
    if client_id and client_secret:
        return {"installed": {
            "client_id": client_id, "client_secret": client_secret,
            "auth_uri": AUTH_URI, "token_uri": TOKEN_URI,
        }}
    if client_id or client_secret:
        raise RuntimeError(f"${CLIENT_ID_ENV} and ${CLIENT_SECRET_ENV} only work as a pair; one of them is unset.")
    return None


def _from_refresh_token(client: dict, refresh_token: str) -> Credentials:
    """Credentials built from the Cloud project's OAuth client and a refresh token issued to
    it. That pair is what a token.json is made of, minus the expired access token it also
    carries, so a run holding it can mint a live token of its own."""
    details = client.get("installed") or client["web"]
    return Credentials(
        None,
        refresh_token=refresh_token,
        token_uri=details.get("token_uri", TOKEN_URI),
        client_id=details["client_id"],
        client_secret=details["client_secret"],
        scopes=SCOPES,
    )


def _check_granted(granted: Sequence[str] | str | None) -> None:
    """Stop a token that carries less than SCOPES before anything is read with it.

    A refresh token holds the access it was granted at consent and no others: asking for
    more here doesn't widen one, it gets the old set back and a warning in a log nobody
    reads. What follows is worse than an error — Drive answers a search made without the
    scope to see a file with no file rather than with a refusal, so a token granted the
    older, narrower drive.file makes a folder created by hand look like one that was
    never created at all.

    Said of whatever the run has to go on: what a stored token records it was granted, or
    what the token endpoint hands back with a fresh one. Neither is always there, and
    nothing said is not the same as nothing granted — that goes through, and the API
    answers for itself.
    """
    # A token file is allowed to hold them as one space-separated string, the way the token
    # endpoint sends them, rather than as a list.
    listed = set(granted.split() if isinstance(granted, str) else granted or [])
    missing = [scope for scope in SCOPES if scope not in listed] if listed else []
    if missing:
        raise RuntimeError(
            "The Google token doesn't carry " + ", ".join(missing) + ". A refresh token "
            "keeps the access you consented to and can't gain more, so one made before "
            "this app asked for these leaves the run finding none of what it's meant to "
            "read — a Drive search made without the scope to see a file comes back empty "
            "rather than refused. Authorize again, asking for all of: "
            f"{' '.join(SCOPES)} — and replace ${REFRESH_ENV} (or ${TOKEN_ENV}, or "
            "token.json) with what that hands back. See 'Setting it up from a phone' in "
            "README.md."
        )


def _stored_credentials(token_path: Path) -> tuple[Credentials | None, bool]:
    """What a run has to go on before it talks to Google, and whether it came from the
    environment. Three ways in, in the order they're preferred: a whole token.json in
    $TICKET_SORTER_TOKEN; the OAuth client and a refresh token for it, as two variables of
    their own; and the token file a local run leaves behind."""
    token = _env_json(TOKEN_ENV, "a token file's contents")
    if token:
        try:
            creds = Credentials.from_authorized_user_info(token, SCOPES)
        except ValueError as error:
            raise RuntimeError(f"${TOKEN_ENV} is set, but isn't a token file's contents: {error}") from error
        # What the file records it was granted, which loading it drops in favour of what
        # this run asks for — and which is the only word on the matter until a refresh.
        _check_granted(token.get("scopes"))
        return creds, True

    client, refresh_token = _oauth_client(), os.environ.get(REFRESH_ENV)
    if client and refresh_token:
        return _from_refresh_token(client, refresh_token), True

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        _check_granted(json.loads(token_path.read_text()).get("scopes"))
        return creds, False
    return None, False


def _authorize(credentials_path: Path) -> Credentials:
    """Ask Google for a token from scratch, which means opening a browser. A run with no
    terminal has nobody to open one for, so it says what to set instead of hanging on a
    browser that is never coming back."""
    client = _oauth_client()
    if not sys.stdin.isatty():
        raise RuntimeError(
            "No usable Google token, and no way to ask for one: authorizing opens a browser and "
            f"this run has no terminal. Set ${REFRESH_ENV} to a refresh token, along with the "
            f"OAuth client it was issued to — as ${CLIENT_ID_ENV} and ${CLIENT_SECRET_ENV}, or "
            f"as ${CLIENT_ENV}. A whole token file in ${TOKEN_ENV} does instead (see README.md)."
        )
    if not client and not credentials_path.exists():
        raise FileNotFoundError(
            f"{credentials_path} not found. Create a Google Cloud OAuth Desktop client "
            "and save it there (see README.md)."
        )
    print("Opening browser to authorize with Google...")
    flow = (
        InstalledAppFlow.from_client_config(client, SCOPES) if client
        else InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    )
    return flow.run_local_server(port=0)


def get_credentials(credentials_path: Path, token_path: Path) -> Credentials:
    """The Google account a run acts as. Credentials from the environment come ahead of the
    token file and are never written back to: the refresh they need lasts the run, and a run
    given its credentials as secrets has nowhere of its own to keep one."""
    creds, from_env = _stored_credentials(token_path)
    if creds and creds.valid:
        return creds
    if creds and creds.refresh_token:
        # Also the way in for a refresh token passed on its own: it arrives with no access
        # token at all rather than an expired one, and redeeming it is what makes it usable.
        print("Refreshing Google auth token...")
        creds.refresh(Request())
    else:
        creds = _authorize(credentials_path)
    _check_granted(creds.granted_scopes)
    if not from_env:
        token_path.write_text(creds.to_json())
    return creds


def find_child(drive, name: str, mime_type: str | None, parent_id: str | None) -> str | None:
    """The id of what's named `name` inside parent_id (or the Drive root), if anything is.
    The newest of them, if several are: a Drive folder can hold any number of files sharing a
    name, and which of those a run picks up decides what it treats as current — so it has to
    be the last one written rather than whichever Drive happened to list first."""
    # Escaped, not interpolated raw: a name is whatever the config says, and an apostrophe in
    # it would otherwise end the quoted string and leave Drive with a query it can't parse.
    quoted = name.replace("\\", "\\\\").replace("'", "\\'")
    q = f"name = '{quoted}' and trashed = false and '{parent_id or 'root'}' in parents"
    if mime_type:
        q += f" and mimeType = '{mime_type}'"
    listed = execute(
        drive.files().list(q=q, fields="files(id)", orderBy=NEWEST_FIRST, pageSize=1), "lookup"
    ).get("files", [])
    return listed[0]["id"] if listed else None


def _create_folder(drive, name: str, parent_id: str | None = None) -> str:
    body = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        body["parents"] = [parent_id]
    return execute(drive.files().create(body=body, fields="id"), "folder create")["id"]


def _find_or_create_folder(drive, name: str, parent_id: str | None = None) -> str:
    return find_child(drive, name, FOLDER_MIME, parent_id) or _create_folder(drive, name, parent_id)


def _trash(drive, file_id: str) -> None:
    execute(drive.files().update(fileId=file_id, body={"trashed": True}), "trash")


def _sync(drive, folder_id: str, wanted: set[str]) -> dict[str, dict]:
    """Trash whatever in folder_id isn't named in wanted; return the survivors by name.
    Newest first, so that of several files sharing a name the survivor is the last one
    written — the same one find_child would have looked up."""
    q = f"'{folder_id}' in parents and trashed = false"
    keep: dict[str, dict] = {}
    # permissions comes along for the ride, so checking what's already link-shared
    # costs no request of its own; so does what a shortcut points at, for the same reason.
    fields = "files(id,name,mimeType,webViewLink,permissions(type,role),shortcutDetails(targetId))"
    files = execute(drive.files().list(q=q, fields=fields, orderBy=NEWEST_FIRST, pageSize=1000), "folder listing")
    for f in files.get("files", []):
        if f["name"] in wanted and f["name"] not in keep:
            keep[f["name"]] = f
        else:
            print(f"  removing {f['name']}")
            _trash(drive, f["id"])
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


def _upload_file(drive, path: Path, parent_id: str) -> dict:
    media = MediaFileUpload(str(path), mimetype="application/pdf")
    body = {"name": path.name, "parents": [parent_id]}
    file = execute(drive.files().create(body=body, media_body=media, fields="id,webViewLink"), "file upload")
    _link_share(drive, file["id"], "reader", "file sharing")  # freshly created: never shared yet
    return file


def _shortcut_to(drive, existing: dict, name: str, target_id: str, parent_id: str) -> bool:
    """File `name` in parent_id as a shortcut to target_id, and say whether one had to be
    made. A shortcut already pointing there is left alone, so a re-run costs nothing; one
    pointing somewhere else is replaced rather than kept, since a shortcut outlives the
    file it names and opening a stale one gets an error page instead of a ticket."""
    found = existing.get(name)
    if found and found.get("shortcutDetails", {}).get("targetId") == target_id:
        return False
    if found:
        _trash(drive, found["id"])
    body = {
        "name": name, "mimeType": SHORTCUT_MIME, "parents": [parent_id],
        "shortcutDetails": {"targetId": target_id},
    }
    execute(drive.files().create(body=body, fields="id"), "shortcut create")
    return True


def _replace_file(drive, existing: dict, parent_id: str, name: str, path: Path, mime_type: str) -> None:
    """Put `path` in the folder under `name`, over whatever is already there under it. Unlike
    the ticket PDFs these are rewritten every run, since their contents change every run."""
    media = MediaFileUpload(str(path), mimetype=mime_type)
    found = existing.get(name)
    if found:
        execute(drive.files().update(fileId=found["id"], media_body=media), f"{name} upload")
    else:
        execute(drive.files().create(body={"name": name, "parents": [parent_id]}, media_body=media), f"{name} upload")


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

    root_id = find_child(drive, name, FOLDER_MIME, None)
    sheet_id = find_child(drive, name, SHEET_MIME, root_id) if root_id else None
    if not sheet_id:
        return set(), set()

    grabbed = execute(
        sheets.spreadsheets().values().batchGet(spreadsheetId=sheet_id, ranges=["Tickets", "Duplicates"]),
        "sheet marks",
    )
    tabs = [r.get("values", []) for r in grabbed.get("valueRanges", [])]
    tickets_tab, duplicates_tab = [*tabs, [], []][:2]
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
    rows: list, duplicate_rows: list, output_dir: Path, name: str, credentials_path: Path, token_path: Path,
    config_path: Path | None = None,
) -> tuple[str, str]:
    creds = get_credentials(credentials_path, token_path)
    drive = build("drive", "v3", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)

    root_id = _find_or_create_folder(drive, name)

    print(f"Syncing '{name}'...")
    root = _sync(drive, root_id, {"tickets", "duplicates", "tickets.csv", "duplicates.csv", name} | KEPT_IN_ROOT)
    sheet = root.get(name)
    sheet_id_existing = sheet["id"] if sheet and sheet["mimeType"] == SHEET_MIME else None

    tickets_id = _child_folder(drive, root, "tickets", root_id)
    duplicates_id = _child_folder(drive, root, "duplicates", root_id)

    # The two groupings, as they are locally. A folder that held the recipient folders
    # directly — anything a version before these two sorted — is trashed by the sync.
    groupings = _sync(drive, tickets_id, {BY_HOLDER, BY_SHOWING})
    by_holder_id = _child_folder(drive, groupings, BY_HOLDER, tickets_id)
    by_showing_id = _child_folder(drive, groupings, BY_SHOWING, tickets_id)

    by_slug: dict[str, list] = {}
    for t in rows:
        by_slug.setdefault(slugify(t["issued_to"]), []).append(t)

    total = len(rows) + len(duplicate_rows)
    print(f"Uploading {total} PDFs (skipping any already there)...")
    recipients = _sync(drive, by_holder_id, set(by_slug))
    n, newly_shared = 0, 0
    for slug, tickets in by_slug.items():
        folder_id = _child_folder(drive, recipients, slug, by_holder_id)
        existing = _sync(drive, folder_id, {Path(t["file"]).name for t in tickets})
        for t in tickets:
            n += 1
            found = existing.get(Path(t["file"]).name)
            print(f"  [{n}/{total}] {t['file']}{' (already there)' if found else ''}")
            if not found:
                found = _upload_file(drive, output_dir / t["file"], folder_id)
            else:
                newly_shared += _share_listed(drive, found)
            # The id as well as the link: the by-showing shortcuts are made to point here.
            t["drive_id"], t["drive_link"] = found["id"], found["webViewLink"]

    existing = _sync(drive, duplicates_id, {Path(t["file"]).name for t in duplicate_rows})
    for t in duplicate_rows:
        n += 1
        found = existing.get(Path(t["file"]).name)
        print(f"  [{n}/{total}] {t['file']} (duplicate{', already there' if found else ''})")
        if found:
            t["drive_link"] = found["webViewLink"]
            newly_shared += _share_listed(drive, found)
        else:
            t["drive_link"] = _upload_file(drive, output_dir / t["file"], duplicates_id)["webViewLink"]
    if newly_shared:
        print(f"  shared {newly_shared} already-uploaded PDFs by link")

    by_showing: dict[str, list] = {}
    for t in rows:
        by_showing.setdefault(showing_slug(t), []).append(t)

    print(f"Grouping {len(rows)} tickets by showing ({len(by_showing)} of them)...")
    showings = _sync(drive, by_showing_id, set(by_showing))
    made = 0
    for slug, tickets in by_showing.items():
        folder_id = _child_folder(drive, showings, slug, by_showing_id)
        existing = _sync(drive, folder_id, {Path(t["file"]).name for t in tickets})
        for t in tickets:
            made += _shortcut_to(drive, existing, Path(t["file"]).name, t["drive_id"], folder_id)
    print(f"  {made} shortcuts added, {len(rows) - made} already pointing at their ticket")

    for csv_name in ("tickets.csv", "duplicates.csv"):
        csv_path = output_dir / csv_name
        if csv_path.exists():
            print(f"Uploading {csv_name}...")
            _replace_file(drive, root, root_id, csv_name, csv_path, "text/csv")

    # Kept next to them so the next run — on a machine that has none of this, in the usual
    # case — starts from the aliases and duplicate marks this one ends with.
    if config_path and config_path.exists():
        print(f"Uploading {CONFIG_NAME}...")
        _replace_file(drive, root, root_id, CONFIG_NAME, config_path, "text/plain")

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
