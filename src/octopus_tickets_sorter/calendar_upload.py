"""Create a Google Calendar with one event per showing, linking each attendee's uploaded ticket."""
import colorsys
from datetime import timedelta
from pathlib import Path

from googleapiclient.discovery import build

from .colors import showing_hues, showing_key
from .drive_upload import get_credentials

CALENDAR_NAME = "Octopus 2026 Tickets"
TIME_ZONE = "Europe/Warsaw"
FALLBACK_LENGTH_MIN = 90  # used only if a movie's runtime is unknown


def _find_calendar(cal, name: str) -> str | None:
    page_token = None
    while True:
        page = cal.calendarList().list(pageToken=page_token).execute()
        for entry in page.get("items", []):
            if entry["summary"] == name:
                return entry["id"]
        page_token = page.get("nextPageToken")
        if not page_token:
            return None


def _wipe_events(cal, calendar_id: str) -> None:
    page_token = None
    while True:
        page = cal.events().list(calendarId=calendar_id, pageToken=page_token).execute()
        for event in page.get("items", []):
            cal.events().delete(calendarId=calendar_id, eventId=event["id"]).execute()
        page_token = page.get("nextPageToken")
        if not page_token:
            return


def _group_showings(rows: list) -> dict[tuple, list]:
    """One entry per distinct (start_time, movie, address) showing."""
    showings: dict[tuple, list] = {}
    for t in rows:
        showings.setdefault(showing_key(t), []).append(t)
    return showings


def _hls(hex_color: str) -> tuple[float, float, float]:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return colorsys.rgb_to_hls(r, g, b)


def _hue_distance(a: float, b: float) -> float:
    d = abs(a - b) % 1.0
    return min(d, 1.0 - d)


def _color_hues(event_colors: dict[str, dict]) -> dict[str, float]:
    """Hue per colorId, excluding Graphite (grey, no real hue) so it never wins a match by coincidence."""
    hls = {color_id: _hls(definition["background"]) for color_id, definition in event_colors.items()}
    return {color_id: h for color_id, (h, _l, s) in hls.items() if s > 0.1}


def _nearest_color_id(hue: float, hues_by_color_id: dict[str, float]) -> str:
    """Calendar events only support Google's fixed 11-color palette, so match the Sheet's pastel by hue."""
    return min(hues_by_color_id, key=lambda cid: _hue_distance(hue, hues_by_color_id[cid]))


def _event_body(key: tuple, tickets: list, color_id: str) -> dict:
    start_time, movie, address = key
    end_time = tickets[0]["end_time"] or start_time + timedelta(minutes=FALLBACK_LENGTH_MIN)
    lines = [f"Section: {tickets[0]['section']}", ""]
    lines += [f'<a href="{t["drive_link"]}">{t["issued_to"]}\'s ticket</a>' for t in tickets]
    return {
        "summary": movie,
        "location": address,
        "description": "<br>".join(lines),
        "start": {"dateTime": start_time.isoformat(), "timeZone": TIME_ZONE},
        "end": {"dateTime": end_time.isoformat(), "timeZone": TIME_ZONE},
        "colorId": color_id,
    }


def create_calendar(rows: list, credentials_path: Path, token_path: Path) -> str:
    creds = get_credentials(credentials_path, token_path)
    cal = build("calendar", "v3", credentials=creds)

    calendar_id = _find_calendar(cal, CALENDAR_NAME)
    if calendar_id:
        print(f"Reusing calendar '{CALENDAR_NAME}'...")
        _wipe_events(cal, calendar_id)
    else:
        print(f"Creating calendar '{CALENDAR_NAME}'...")
        calendar_id = cal.calendars().insert(body={"summary": CALENDAR_NAME, "timeZone": TIME_ZONE}).execute()["id"]

    color_hues = _color_hues(cal.colors().get().execute()["event"])
    color_ids = {key: _nearest_color_id(hue, color_hues) for key, hue in showing_hues(rows).items()}

    showings = _group_showings(rows)
    print(f"Adding {len(showings)} showings to the calendar...")
    for key, tickets in showings.items():
        body = _event_body(key, tickets, color_ids[key])
        cal.events().insert(calendarId=calendar_id, body=body).execute()

    return f"https://calendar.google.com/calendar/r?cid={calendar_id}"
