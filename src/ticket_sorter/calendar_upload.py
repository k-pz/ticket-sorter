"""Create one Google Calendar per ticket holder, with an event per showing linking their uploaded ticket."""
import colorsys
import re
import sys
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

from googleapiclient.discovery import build

from .colors import person_hues
from .drive_upload import execute, get_credentials
from .names import resolve

TIME_ZONE = "Europe/Warsaw"
FALLBACK_LENGTH_MIN = 90  # used only if a movie's runtime is unknown
SEPARATOR = " – "  # a person's calendar is titled "<name><SEPARATOR><person>"
CREATE_PAUSE_S = 1.0  # breathing room between calendar creations, which are rationed hardest
PUBLIC_ROLE = "reader"  # the most a link can carry: Google rejects "writer" on the public scope

# What someone types in front of an event title to hand that showing back.
DUPLICATE_MARK = re.compile(r"^\s*DUPLICATE\b[\s:–—-]*", re.IGNORECASE)


def _owned_calendars(cal) -> list[dict]:
    """Every calendar the account owns. Calendars merely subscribed to are skipped:
    they aren't ours to delete, and we'd fail trying."""
    entries, page_token = [], None
    while True:
        page = execute(cal.calendarList().list(pageToken=page_token), "calendar list")
        entries += [e for e in page.get("items", []) if e.get("accessRole") == "owner"]
        page_token = page.get("nextPageToken")
        if not page_token:
            return entries


def _partition(entries, name: str, wanted: set[str]) -> tuple[dict[str, dict], list[dict]]:
    """Split the account's calendars into the ones this run reuses (by title) and the
    ones it should delete: the single shared calendar older versions created, calendars
    for people this run has no tickets for — someone who dropped out, or a name spelling
    since merged away — and same-title leftovers. Anything not ours is left alone."""
    keep, drop = {}, []
    for entry in entries:
        title = entry["summary"]
        if title in wanted and title not in keep:
            keep[title] = entry
        elif title == name or title.startswith(f"{name}{SEPARATOR}"):
            drop.append(entry)
    return keep, drop


def _events(cal, calendar_id: str) -> list[dict]:
    events, page_token = [], None
    while True:
        page = execute(cal.events().list(calendarId=calendar_id, pageToken=page_token), "event list")
        events += page.get("items", [])
        page_token = page.get("nextPageToken")
        if not page_token:
            return events


def _is_public_reader(rule: dict) -> bool:
    return rule.get("scope", {}).get("type") == "default" and rule.get("role") == PUBLIC_ROLE


def _share(cal, calendar_id: str) -> bool:
    """Make the calendar's link work for anyone it's sent to, and say whether that had
    to be changed. Calendar has no "anyone with the link" tier the way Drive does —
    scope "default" means public, and Google refuses to pair it with a role that can
    write ("The default scope cannot be granted a write or owners access role"), so a
    link can only ever carry read access. The rule is read first, so a calendar that is
    already shared costs no write."""
    rules = execute(cal.acl().list(calendarId=calendar_id), "sharing rules").get("items", [])
    if any(_is_public_reader(rule) for rule in rules):
        return False
    execute(
        cal.acl().insert(calendarId=calendar_id, body={"role": PUBLIC_ROLE, "scope": {"type": "default"}}),
        "sharing rule",
    )
    return True


def _code_of(event: dict) -> str | None:
    return event.get("extendedProperties", {}).get("private", {}).get("reservation_code")


def _differs(event: dict, body: dict) -> bool:
    """Whether a calendar event still says what this run wants it to say. Start/end are
    compared without the UTC offset Google echoes back on them."""
    if any(event.get(field) != body[field] for field in ("summary", "location", "description")):
        return True
    return any(event.get(k, {}).get("dateTime", "")[:19] != body[k]["dateTime"][:19] for k in ("start", "end"))


def _sync_events(cal, calendar_id: str, tickets: list) -> tuple[int, int, int]:
    """Bring a calendar's events in line with tickets, writing only where they differ.
    Returns (added, updated, removed).

    Deleting every event and re-inserting it is what makes a run cost a couple of
    hundred calendar writes and get rate limited; matching on the reservation code each
    event carries means a re-run with nothing new costs only the read. It also puts back
    a title someone edited, so an unresolved DUPLICATE mark doesn't linger."""
    existing, stale = {}, []
    for event in _events(cal, calendar_id):
        code = _code_of(event)
        if code and code not in existing:
            existing[code] = event
        else:
            stale.append(event)  # untagged (predates tagging) or a same-code leftover

    added = updated = 0
    for t in tickets:
        body = _event_body(t)
        event = existing.pop(t["reservation_code"], None)
        if event is None:
            execute(cal.events().insert(calendarId=calendar_id, body=body), "event insert")
            added += 1
        elif _differs(event, body):
            execute(cal.events().update(calendarId=calendar_id, eventId=event["id"], body=body), "event update")
            updated += 1

    removed = stale + list(existing.values())
    for event in removed:
        execute(cal.events().delete(calendarId=calendar_id, eventId=event["id"]), "event delete")
    return added, updated, len(removed)


def _marked_code(event: dict, person: str, by_showing: dict) -> str | None:
    """The reservation code of an event retitled with a DUPLICATE prefix, or None if it
    isn't marked. The code is read off the private property every event is created with;
    matching the showing is a fallback for events made before this run started tagging them."""
    summary = event.get("summary", "")
    if not DUPLICATE_MARK.match(summary):
        return None
    code = event.get("extendedProperties", {}).get("private", {}).get("reservation_code")
    start = event.get("start", {}).get("dateTime", "")[:16]  # "2026-08-11T21:30", offset dropped
    return code or by_showing.get((person, DUPLICATE_MARK.sub("", summary), start))


def read_duplicate_marks(
    name: str, tickets: list, aliases: dict, credentials_path: Path, token_path: Path
) -> set[str]:
    """Reservation codes for showings someone handed back by putting DUPLICATE in front of
    the event title. Read before the calendars are rebuilt, since that wipes the marks."""
    creds = get_credentials(credentials_path, token_path)
    cal = build("calendar", "v3", credentials=creds)

    by_showing = {
        (t["issued_to"], t["movie"], t["start_time"].strftime("%Y-%m-%dT%H:%M")): t["reservation_code"]
        for t in tickets
    }
    codes = set()
    for entry in _owned_calendars(cal):
        title = entry["summary"]
        if not title.startswith(f"{name}{SEPARATOR}"):
            continue
        # Through the aliases too: a calendar still titled with a name spelling that has
        # since been merged away is about the person that spelling now collapses into.
        person = resolve(title[len(name) + len(SEPARATOR):], aliases)
        for event in _events(cal, entry["id"]):
            if not DUPLICATE_MARK.match(event.get("summary", "")):
                continue
            if code := _marked_code(event, person, by_showing):
                codes.add(code)
            else:
                print(
                    f"note: can't tell which ticket '{event.get('summary', '')}' in {title} is; "
                    "re-run once so its event carries a reservation code, then mark it again",
                    file=sys.stderr,
                )
    return codes


def _hex(hue: float) -> str:
    """Calendar colors are per-calendar (not per-event), so any RGB works — no palette matching needed."""
    r, g, b = colorsys.hls_to_rgb(hue, 0.45, 0.6)
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def _event_body(t: dict) -> dict:
    end_time = t["end_time"] or t["start_time"] + timedelta(minutes=FALLBACK_LENGTH_MIN)
    return {
        "summary": t["movie"],
        "location": t["address"],
        "description": f'Section: {t["section"]}<br><br><a href="{t["drive_link"]}">Ticket</a>',
        "start": {"dateTime": t["start_time"].isoformat(), "timeZone": TIME_ZONE},
        "end": {"dateTime": end_time.isoformat(), "timeZone": TIME_ZONE},
        # Survives the title being edited, so a DUPLICATE-marked event still names its ticket.
        "extendedProperties": {"private": {"reservation_code": t["reservation_code"]}},
    }


def create_calendars(rows: list, name: str, credentials_path: Path, token_path: Path) -> dict[str, str]:
    """One calendar per person, each in its own color. Returns {person: calendar url}."""
    creds = get_credentials(credentials_path, token_path)
    cal = build("calendar", "v3", credentials=creds)

    by_person: dict[str, list] = {}
    for t in rows:
        by_person.setdefault(t["issued_to"], []).append(t)

    print(f"Syncing calendars for '{name}'...")
    existing, stale = _partition(
        _owned_calendars(cal), name, {f"{name}{SEPARATOR}{person}" for person in by_person}
    )
    for entry in stale:
        print(f"  deleting {entry['summary']}")
        execute(cal.calendars().delete(calendarId=entry["id"]), "calendar delete")

    hues = person_hues(rows)
    urls = {}
    for person, tickets in by_person.items():
        title = f"{name}{SEPARATOR}{person}"
        entry = existing.get(title)
        if entry:
            print(f"Reusing calendar '{title}'...")
            calendar_id = entry["id"]
        else:
            print(f"Creating calendar '{title}'...")
            created = execute(
                cal.calendars().insert(body={"summary": title, "timeZone": TIME_ZONE}), "calendar create"
            )
            calendar_id = created["id"]
            # Creating calendars is rationed far more tightly than writing events, and
            # going over is a cooldown rather than a momentary throttle, so don't race.
            time.sleep(CREATE_PAUSE_S)

        color = _hex(hues[person])
        if not entry or entry.get("backgroundColor") != color:
            execute(
                cal.calendarList().patch(
                    calendarId=calendar_id,
                    colorRgbFormat=True,
                    body={"backgroundColor": color, "foregroundColor": "#ffffff"},
                ),
                "calendar color",
            )

        if _share(cal, calendar_id):
            print("  shared: anyone with the link can view")

        added, updated, removed = _sync_events(cal, calendar_id, tickets)
        print(f"  {len(tickets)} showings: {added} added, {updated} updated, {removed} removed")

        urls[person] = "https://calendar.google.com/calendar/r?cid=" + quote(calendar_id)

    return urls
