"""Calendar syncing: the pure decisions, then every request the module actually sends."""
from pathlib import Path

import pytest
from conftest import bodies, uris

from octopus_tickets_sorter import calendar_upload as cu

CREDS = (Path("creds.json"), Path("token.json"))  # never opened: fake_google patches the auth away


def service(fake_google, responses):
    http = fake_google(cu, responses)  # patches cu.build, so the service must be built after
    return cu.build("calendar", "v3", credentials=None), http


def sent(http):
    """(method, event id) per request; the id is "" for the collection endpoints."""
    return [(m, u.split("/events")[1].split("?")[0].lstrip("/")) for u, m, *_ in http.request_sequence]


# --- pure helpers ------------------------------------------------------------------


def test_partition_keeps_one_per_wanted_title_and_drops_our_leftovers():
    keep, drop = cu._partition(
        [
            {"summary": "Octopus – Anna", "id": "1"},
            {"summary": "Octopus – Anna", "id": "2"},          # same-title leftover
            {"summary": "Octopus – Ania", "id": "3"},        # merged away into "Anna"
            {"summary": "Octopus", "id": "4"},                  # the old shared calendar
            {"summary": "Octopus Shorts – Blok 1", "id": "5"},  # not ours: no "Octopus – " prefix
            {"summary": "Holidays", "id": "6"},
        ],
        "Octopus",
        {"Octopus – Anna"},
    )
    assert keep == {"Octopus – Anna": {"summary": "Octopus – Anna", "id": "1"}}
    assert [e["id"] for e in drop] == ["2", "3", "4"]


@pytest.mark.parametrize("length_min, end", [
    (103, "2026-08-11T23:13:00"),
    (None, "2026-08-11T23:00:00"),  # an unknown runtime falls back to 90 minutes
])
def test_event_body(make_ticket, length_min, end):
    assert cu._event_body(make_ticket(length_min=length_min, drive_link="https://d/x")) == {
        "summary": "Motor City",
        "location": "Kino Żak, Gdańsk, ul. Wajdeloty 12",
        "description": 'Section: Nocne Szaleństwo<br><br><a href="https://d/x">Ticket</a>',
        "start": {"dateTime": "2026-08-11T21:30:00", "timeZone": "Europe/Warsaw"},
        "end": {"dateTime": end, "timeZone": "Europe/Warsaw"},
        "extendedProperties": {"private": {"reservation_code": "ABC123"}},
    }


def test_differs(make_ticket):
    body = cu._event_body(make_ticket(drive_link="https://d/x"))
    # Google echoes start/end back with a UTC offset the body doesn't carry.
    same = body | {
        "start": {"dateTime": "2026-08-11T21:30:00+02:00"},
        "end": {"dateTime": "2026-08-11T23:13:00+02:00"},
    }
    assert not cu._differs(same, body)
    assert cu._differs(same | {"summary": "DUPLICATE Motor City"}, body)  # an edited title is put back
    assert cu._differs(same | {"start": {"dateTime": "2026-08-11T22:00:00+02:00"}}, body)


@pytest.mark.parametrize("summary, expected", [
    ("Motor City", None),
    ("DUPLICATE Motor City", "DEF456"),
    ("duplicate: Motor City", "DEF456"),
    ("DUPLICATE – Motor City", "DEF456"),
    ("Duplicates of Vice", None),  # \b, so only the whole word counts
])
def test_marked_code(summary, expected):
    tagged = {"summary": summary, "extendedProperties": {"private": {"reservation_code": "DEF456"}}}
    assert cu._marked_code(tagged, "Anna", {}) == expected


def test_marked_code_falls_back_to_the_showing():
    by_showing = {("Anna", "Motor City", "2026-08-11T21:30"): "ABC123"}
    untagged = {"summary": "DUPLICATE Motor City", "start": {"dateTime": "2026-08-11T21:30:00+02:00"}}
    assert cu._marked_code(untagged, "Anna", by_showing) == "ABC123"
    assert cu._marked_code(untagged, "Someone Else", by_showing) is None


def test_is_public_reader():
    # "writer" on the public scope isn't an option to check for — Google rejects it outright.
    assert cu._is_public_reader({"role": "reader", "scope": {"type": "default"}})
    assert not cu._is_public_reader({"role": "freeBusyReader", "scope": {"type": "default"}})
    assert not cu._is_public_reader({"role": "reader", "scope": {"type": "user", "value": "a@b.c"}})
    assert not cu._is_public_reader({"role": "owner", "scope": {"type": "user", "value": "me@b.c"}})


# --- listing -----------------------------------------------------------------------


def test_owned_calendars_pages_and_skips_subscriptions(fake_google):
    cal, http = service(fake_google, [
        {"items": [
            {"id": "mine", "summary": "A", "accessRole": "owner"},
            {"id": "theirs", "summary": "B", "accessRole": "reader"},  # only subscribed to
        ], "nextPageToken": "p2"},
        {"items": [{"id": "mine2", "summary": "C", "accessRole": "owner"}]},
    ])
    assert [e["id"] for e in cu._owned_calendars(cal)] == ["mine", "mine2"]
    assert "pageToken=p2" in uris(http)[1]


def test_events_pages(fake_google):
    cal, http = service(fake_google, [
        {"items": [{"id": "e1"}], "nextPageToken": "p2"},
        {"items": [{"id": "e2"}]},
    ])
    assert [e["id"] for e in cu._events(cal, "cal1")] == ["e1", "e2"]
    assert "pageToken=p2" in uris(http)[1]


# --- sharing -----------------------------------------------------------------------


def test_share_skips_the_write_when_already_public(fake_google):
    cal, http = service(fake_google, [{"items": [{"role": "reader", "scope": {"type": "default"}}]}])
    assert cu._share(cal, "cal1") is False
    assert len(http.request_sequence) == 1


def test_share_inserts_the_public_rule(fake_google):
    cal, http = service(fake_google, [{"items": [{"role": "owner", "scope": {"type": "user"}}]}, {}])
    assert cu._share(cal, "cal1") is True
    assert bodies(http)[1] == {"role": "reader", "scope": {"type": "default"}}


# --- syncing -----------------------------------------------------------------------


def test_sync_events(fake_google, make_ticket):
    unchanged = make_ticket(reservation_code="ABC123", drive_link="https://d/1")
    moved = make_ticket(reservation_code="B7QQQ1", movie="Vice", drive_link="https://d/2")
    fresh = make_ticket(reservation_code="C3ZZZ9", movie="Sirat", drive_link="https://d/3")

    body = cu._event_body(unchanged)
    cal, http = service(fake_google, [
        {"items": [
            # the offset Google adds is all that separates this one from what we'd write
            {"id": "ev1", **body, "start": {"dateTime": "2026-08-11T21:30:00+02:00"},
             "end": {"dateTime": "2026-08-11T23:13:00+02:00"}},
            {"id": "ev2", **cu._event_body(moved | {"movie": "Vice (35mm)"})},
            {"id": "ev3", **cu._event_body(make_ticket(reservation_code="GONE01", drive_link="https://d/4"))},
            {"id": "ev4", "summary": "Motor City"},  # predates tagging: no reservation code
        ]},
        {},  # ev2 update
        {},  # insert for the ticket with no event
        {},  # ev4 delete
        {},  # ev3 delete
    ])

    assert cu._sync_events(cal, "cal1", [unchanged, moved, fresh]) == (1, 1, 2)
    assert sent(http) == [
        ("GET", ""), ("PUT", "ev2"), ("POST", ""), ("DELETE", "ev4"), ("DELETE", "ev3")
    ]
    assert bodies(http)[2]["summary"] == "Sirat"


# --- duplicate marks ---------------------------------------------------------------


def test_read_duplicate_marks(fake_google, make_ticket, capsys):
    tickets = [make_ticket(issued_to="Anna")]
    fake_google(cu, [
        {"items": [  # only the two "Octopus – " calendars are read
            {"id": "c1", "summary": "Octopus – Anna", "accessRole": "owner"},
            {"id": "c2", "summary": "Octopus – Ania", "accessRole": "owner"},
            {"id": "c3", "summary": "Holidays", "accessRole": "owner"},
        ]},
        {"items": [  # c1
            {"id": "e1", "summary": "DUPLICATE Motor City",
             "extendedProperties": {"private": {"reservation_code": "DEF456"}}},
            {"id": "e2", "summary": "Sirat"},
            {"id": "e3", "summary": "DUPLICATE Unknown Film"},  # no code, no matching showing
        ]},
        {"items": [  # c2, titled with a spelling since merged into "Anna"
            {"id": "e4", "summary": "DUPLICATE Motor City", "start": {"dateTime": "2026-08-11T21:30:00+02:00"}},
        ]},
    ])
    assert cu.read_duplicate_marks("Octopus", tickets, {"Ania": "Anna"}, *CREDS) == {"DEF456", "ABC123"}
    assert "re-run once" in capsys.readouterr().err


# --- create_calendars --------------------------------------------------------------


def test_create_calendars(fake_google, make_ticket):
    rows = [
        make_ticket(issued_to="Anna", drive_link="https://d/1"),
        make_ticket(issued_to="Ola", movie="Sirat", reservation_code="C3ZZZ9", drive_link="https://d/2"),
    ]
    http = fake_google(cu, [
        {"items": [  # the account's calendars
            # "@" in the id pins the quoting of the returned URL; the color is already right
            {"id": "anna@group.calendar.google.com", "summary": "Octopus – Anna",
             "accessRole": "owner", "backgroundColor": "#b82e2e"},
            {"id": "stale1", "summary": "Octopus – Zoe", "accessRole": "owner"},  # dropped out
            {"id": "holidays", "summary": "Holidays", "accessRole": "owner"},  # not ours
        ]},
        {},                                                       # delete Octopus – Zoe
        {"items": [{"role": "reader", "scope": {"type": "default"}}]},  # Anna acl: already shared
        {"items": []},                                            # Anna events
        {},                                                       # Anna event insert
        {"id": "ola1"},                                           # create Octopus – Ola
        {},                                                       # Ola color patch
        {"items": []},                                            # Ola acl
        {},                                                       # Ola sharing rule insert
        {"items": []},                                            # Ola events
        {},                                                       # Ola event insert
    ])
    urls = cu.create_calendars(rows, "Octopus", *CREDS)

    assert urls == {
        "Anna": "https://calendar.google.com/calendar/r?cid=anna%40group.calendar.google.com",
        "Ola": "https://calendar.google.com/calendar/r?cid=ola1",
    }
    stale_uri, method, *_ = http.request_sequence[1]  # the stale calendar goes first
    assert (method, "/calendars/stale1?" in stale_uri) == ("DELETE", True)
    # Only Ola's calendar is recolored: Anna's backgroundColor already matches her hue.
    assert [b for b in bodies(http) if b and "backgroundColor" in b] == [
        {"backgroundColor": "#2eb8b8", "foregroundColor": "#ffffff"}
    ]
    assert [b["summary"] for b in bodies(http) if b and b.get("summary")] == [
        "Motor City", "Octopus – Ola", "Sirat"
    ]


def test_create_calendars_recolors_and_resyncs_a_reused_calendar(fake_google, make_ticket):
    http = fake_google(cu, [
        {"items": [{"id": "c1", "summary": "Octopus – Anna", "accessRole": "owner",
                    "backgroundColor": "#000000"}]},                 # reused, but the wrong color
        {},                                                          # so patch it
        {"items": [{"role": "reader", "scope": {"type": "default"}}]},  # already shared
        {"items": [{"id": "ev1", "summary": "Sirat",                 # a showing that is no longer hers
                    "extendedProperties": {"private": {"reservation_code": "GONE01"}}}]},
        {},                                                          # insert her Motor City event
        {},                                                          # and delete the stale one
    ])
    rows = [make_ticket(issued_to="Anna", drive_link="https://d/1")]
    assert cu.create_calendars(rows, "Octopus", *CREDS) == {
        "Anna": "https://calendar.google.com/calendar/r?cid=c1"
    }

    assert bodies(http)[1] == {"backgroundColor": "#b82e2e", "foregroundColor": "#ffffff"}
    assert [(m, u.split("/calendars/")[1].split("?")[0])
            for u, m, *_ in http.request_sequence if m in ("POST", "DELETE")] == [
        ("POST", "c1/events"), ("DELETE", "c1/events/ev1")
    ]  # no calendars().insert among them: the existing calendar is kept and fixed up
