"""Upload sorted tickets to Google Drive and build a Sheet with linked tables."""
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .colors import showing_colors, showing_key
from .main import slugify

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar",
]

HEADER = [
    "start_time", "end_time", "length_min", "movie", "issued_to", "address",
    "section", "reservation_code", "ticket_code", "ticket_pdf",
]

ROOT_FOLDER_NAME = "Octopus 2026 Tickets"
SHEET_NAME = "Octopus 2026 Tickets"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"


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
    files = drive.files().list(q=q, fields="files(id)", pageSize=1).execute().get("files", [])
    return files[0]["id"] if files else None


def _create_folder(drive, name: str, parent_id: str | None = None) -> str:
    body = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        body["parents"] = [parent_id]
    return drive.files().create(body=body, fields="id").execute()["id"]


def _find_or_create_folder(drive, name: str, parent_id: str | None = None) -> str:
    return _find(drive, name, FOLDER_MIME, parent_id) or _create_folder(drive, name, parent_id)


def _wipe_children(drive, folder_id: str, keep_id: str | None = None) -> None:
    q = f"'{folder_id}' in parents and trashed = false"
    for f in drive.files().list(q=q, fields="files(id)", pageSize=1000).execute().get("files", []):
        if f["id"] != keep_id:
            drive.files().update(fileId=f["id"], body={"trashed": True}).execute()


def _upload_file(drive, path: Path, parent_id: str) -> str:
    media = MediaFileUpload(str(path), mimetype="application/pdf")
    body = {"name": path.name, "parents": [parent_id]}
    file = drive.files().create(body=body, media_body=media, fields="id,webViewLink").execute()
    return file["webViewLink"]


def _row_values(t: dict, drive_link: str) -> list:
    return [
        t["start_time"].strftime("%Y-%m-%d %H:%M"),
        t["end_time"].strftime("%Y-%m-%d %H:%M") if t["end_time"] else "",
        t["length_min"] or "",
        t["movie"],
        t["issued_to"],
        t["address"],
        t["section"],
        t["reservation_code"],
        t["ticket_code"],
        f'=HYPERLINK("{drive_link}", "Open ticket")',
    ]


def _write_table(sheets, spreadsheet_id: str, sheet_id: int, title: str, rows: list) -> None:
    values = [HEADER] + [_row_values(t, t["drive_link"]) for t in rows]
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{title}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()

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
    sheets.spreadsheets().batchUpdate(
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
            ]
        },
    ).execute()


def upload_to_drive(rows: list, duplicate_rows: list, output_dir: Path, credentials_path: Path, token_path: Path) -> tuple[str, str]:
    creds = get_credentials(credentials_path, token_path)
    drive = build("drive", "v3", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)

    root_id = _find_or_create_folder(drive, ROOT_FOLDER_NAME)
    sheet_id_existing = _find(drive, SHEET_NAME, SHEET_MIME, root_id)

    print(f"Wiping previous contents of '{ROOT_FOLDER_NAME}'...")
    _wipe_children(drive, root_id, keep_id=sheet_id_existing)

    tickets_id = _create_folder(drive, "tickets", root_id)
    duplicates_id = _create_folder(drive, "duplicates", root_id)

    total = len(rows) + len(duplicate_rows)
    print(f"Uploading {total} PDFs...")
    recipient_folders: dict[str, str] = {}
    n = 0
    for t in rows:
        n += 1
        slug = slugify(t["issued_to"])
        if slug not in recipient_folders:
            recipient_folders[slug] = _create_folder(drive, slug, tickets_id)
        print(f"  [{n}/{total}] {t['file']}")
        t["drive_link"] = _upload_file(drive, output_dir / t["file"], recipient_folders[slug])

    for t in duplicate_rows:
        n += 1
        print(f"  [{n}/{total}] {t['file']} (duplicate)")
        t["drive_link"] = _upload_file(drive, output_dir / t["file"], duplicates_id)

    for csv_name in ("tickets.csv", "duplicates.csv"):
        csv_path = output_dir / csv_name
        if csv_path.exists():
            print(f"Uploading {csv_name}...")
            media = MediaFileUpload(str(csv_path), mimetype="text/csv")
            drive.files().create(body={"name": csv_name, "parents": [root_id]}, media_body=media).execute()

    if sheet_id_existing:
        print(f"Reusing spreadsheet '{SHEET_NAME}'...")
        spreadsheet_id = sheet_id_existing
        meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets.properties").execute()
        sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}
        for title in ("Tickets", "Duplicates"):
            sheets.spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range=title).execute()
    else:
        print(f"Creating spreadsheet '{SHEET_NAME}'...")
        created = sheets.spreadsheets().create(
            body={
                "properties": {"title": SHEET_NAME},
                "sheets": [{"properties": {"title": "Tickets"}}, {"properties": {"title": "Duplicates"}}],
            },
            fields="spreadsheetId,sheets.properties",
        ).execute()
        spreadsheet_id = created["spreadsheetId"]
        sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in created["sheets"]}

        prev_parents = drive.files().get(fileId=spreadsheet_id, fields="parents").execute().get("parents", [])
        drive.files().update(
            fileId=spreadsheet_id, addParents=root_id, removeParents=",".join(prev_parents), fields="id,parents"
        ).execute()

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"updateSpreadsheetProperties": {"properties": {"locale": "en_US"}, "fields": "locale"}}]},
    ).execute()

    print(f"Writing Tickets table ({len(rows)} rows)...")
    _write_table(sheets, spreadsheet_id, sheet_ids["Tickets"], "Tickets", rows)
    print(f"Writing Duplicates table ({len(duplicate_rows)} rows)...")
    _write_table(sheets, spreadsheet_id, sheet_ids["Duplicates"], "Duplicates", duplicate_rows)

    return f"https://drive.google.com/drive/folders/{root_id}", f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
