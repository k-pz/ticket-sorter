"""Shared fixtures.

Nothing here stands in for a library by hand. Ticket PDFs are real PDFs, written with
pypdf and read back through pypdf, so parsing is exercised end to end. The Google
services are real googleapiclient clients whose transport is googleapiclient's own
HttpMockSequence, so every request is still built and validated against the bundled
static discovery document for the API.
"""
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pypdf
import pytest
from googleapiclient.discovery import build
from googleapiclient.http import HttpMockSequence
from pypdf._codecs import adobe_glyphs
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

GLYPH_NAMES = {char: name for name, char in adobe_glyphs.items()}  # "/lslash" -> "ł", inverted


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Retry backoff and the pause between calendar creations are about pacing live API
    calls; against a mock transport they would only make the suite slow."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)


# --- ticket PDFs -------------------------------------------------------------------


def write_pdf(path: Path, lines: list[str]) -> Path:
    """Write a one-page PDF whose extracted text is `lines`, one per line.

    Anything outside ASCII (Polish titles and names) gets a byte code of its own plus an
    entry in the font's /Differences table naming its Adobe glyph, which is what pypdf
    maps back to the character when extracting.
    """
    codes: dict[str, int] = {}

    def encode(line: str) -> bytes:
        out = bytearray()
        for char in line:
            if char in "\\()":  # the delimiters of a PDF literal string
                out += b"\\"
            out.append(ord(char) if ord(char) < 128 else codes.setdefault(char, 128 + len(codes)))
        return bytes(out)

    text = b") Tj T*\n(".join(encode(line) for line in lines)

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=595, height=842)

    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 10 Tf 50 800 Td 14 TL\n(" + bytes(text) + b") Tj\nET")
    page[NameObject("/Contents")] = writer._add_object(stream)

    differences = ArrayObject()
    for char, code in codes.items():
        differences.append(NumberObject(code))
        differences.append(NameObject(GLYPH_NAMES[char]))
    encoding = DictionaryObject({
        NameObject("/Type"): NameObject("/Encoding"),
        NameObject("/BaseEncoding"): NameObject("/WinAnsiEncoding"),
        NameObject("/Differences"): differences,
    })
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/Encoding"): writer._add_object(encoding),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    })

    path.parent.mkdir(parents=True, exist_ok=True)
    writer.write(str(path))
    return path


def ticket_lines(
    *,
    start="11.08.2026 21:30",
    city="Gdańsk, ul. Wajdeloty 12",
    venue="Kino Żak",
    ticket_code="T-1234567",
    issued_to="Anna Nowak",
    reservation_code="ABC123",
    section="Nocne Szaleństwo",
    movie="Motor City",
) -> list[str]:
    """The line order a biletomat.pl export comes out in. parse_ticket finds the date line
    and reads every other field at a fixed offset from it, so the padding lines matter as
    much as the real ones. The film title runs from +11 to the line before the last."""
    return [
        "Bilet wstępu",
        "biletomat.pl",
        start,                   # +0  the date line everything else is measured from
        "Data i godzina",        # +1
        city,                    # +2  second half of the address
        "Miejsce",               # +3
        "Sala Kinowa",           # +4
        venue,                   # +5  first half of the address
        ticket_code,             # +6
        "Kod biletu",            # +7
        issued_to,               # +8
        reservation_code,        # +9
        "Kod rezerwacji",        # +10
        f"{section} | {movie}",  # +11
        "Organizator: Octopus Film Festival",  # last line, never part of the title
    ]


@pytest.fixture
def make_pdf(tmp_path):
    """Write a ticket PDF into tmp_path; keyword arguments override individual fields,
    or pass `lines=` for a layout of your own."""
    def make(filename="bilety.pdf", *, lines=None, into=None, **fields):
        return write_pdf((into or tmp_path) / filename, lines or ticket_lines(**fields))
    return make


@pytest.fixture
def make_ticket():
    """A parsed ticket, the dict shape parse_ticket returns and everything downstream
    consumes. end_time follows start_time and length_min unless given outright."""
    def make(**overrides):
        start = overrides.pop("start_time", datetime(2026, 8, 11, 21, 30))
        length = overrides.pop("length_min", 103)
        ticket = {
            "start_time": start,
            "length_min": length,
            "end_time": start + timedelta(minutes=length) if length else None,
            "section": "Nocne Szaleństwo",
            "movie": "Motor City",
            "reservation_code": "ABC123",
            "ticket_code": "T-1234567",
            "address": "Kino Żak, Gdańsk, ul. Wajdeloty 12",
            "issued_to": "Anna Nowak",
        }
        return ticket | overrides
    return make


# --- Google APIs -------------------------------------------------------------------


def _response(item):
    """A response for HttpMockSequence: a body on its own is a 200, or pass (status, body).

    Text and bytes go as they are — that's a downloaded file, or a body too malformed to be
    JSON — and anything else is serialized, which is what an API response usually is.
    """
    status, body = item if isinstance(item, tuple) else (200, item)
    return {"status": str(status)}, body if isinstance(body, str | bytes) else json.dumps(body)


@pytest.fixture
def fake_google(monkeypatch):
    """Point a module's `build`/`get_credentials` at googleapiclient's own HttpMockSequence.

    Every service the module builds shares one sequence of responses, consumed in the order
    the code actually sends its requests, and the returned mock's `request_sequence` records
    what went out — (uri, method, body, headers) per call — to assert against.
    """
    def install(module, responses):
        http = HttpMockSequence([_response(r) for r in responses])
        monkeypatch.setattr(module, "get_credentials", lambda *a, **k: None)
        monkeypatch.setattr(
            module, "build", lambda api, version, **kw: build(api, version, http=http, static_discovery=True)
        )
        return http
    return install


def uris(http) -> list[str]:
    """Just the URIs of what a mock transport was asked to send, in order."""
    return [uri for uri, *_ in http.request_sequence]


def bodies(http) -> list:
    """The JSON bodies sent, in order; non-JSON (or bodyless) requests come back as None."""
    out = []
    for _uri, _method, body, _headers in http.request_sequence:
        try:
            out.append(json.loads(body))
        except (TypeError, ValueError):
            out.append(None)
    return out
