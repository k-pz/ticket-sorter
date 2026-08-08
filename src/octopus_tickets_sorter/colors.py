"""Pastel color assignment shared by the Sheet (row background) and Calendar (event color)."""
import colorsys


def showing_key(t: dict) -> tuple:
    return (t["start_time"], t["movie"], t["address"])


def _hues(keys: list) -> dict:
    """Hue (0-1) per distinct key. Consecutive keys (by first-seen order)
    alternate between the two halves of the color wheel — roughly cool vs
    warm — so neighbors are always near-complementary instead of two
    slivers of the same hue."""
    index: dict = {}
    for key in keys:
        index.setdefault(key, len(index))
    n = len(index)
    evens, odds = (n + 1) // 2, n // 2
    hues = {}
    for key, i in index.items():
        pos, half = i // 2, i % 2
        count = evens if half == 0 else odds
        hues[key] = half * 0.5 + (pos / count) * 0.5
    return hues


def showing_hues(rows: list) -> dict[tuple, float]:
    """Hue per distinct showing, used for the Sheet's row backgrounds."""
    return _hues([showing_key(t) for t in rows])


def person_hues(rows: list) -> dict[str, float]:
    """Hue per distinct ticket holder, used for each person's calendar color."""
    return _hues([t["issued_to"] for t in rows])


def _pastel(hue: float) -> dict:
    return dict(zip("red green blue".split(), colorsys.hls_to_rgb(hue, 0.85, 0.5)))


def showing_colors(rows: list) -> dict[tuple, dict]:
    """Pastel RGB color (0-1 floats) per distinct showing found in rows."""
    return {key: _pastel(hue) for key, hue in showing_hues(rows).items()}
