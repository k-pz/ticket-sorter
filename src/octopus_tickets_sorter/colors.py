"""Pastel color assignment shared by the Sheet (row background) and Calendar (event color)."""
import colorsys


def showing_key(t: dict) -> tuple:
    return (t["start_time"], t["movie"], t["address"])


def showing_indices(rows: list) -> dict[tuple, int]:
    """Stable index per distinct showing, in first-seen order."""
    index: dict[tuple, int] = {}
    for t in rows:
        index.setdefault(showing_key(t), len(index))
    return index


def showing_hues(rows: list) -> dict[tuple, float]:
    """Hue (0-1), evenly spaced, per distinct showing found in rows."""
    index = showing_indices(rows)
    n = len(index)
    return {key: i / n for key, i in index.items()}


def _pastel(hue: float) -> dict:
    return dict(zip("red green blue".split(), colorsys.hls_to_rgb(hue, 0.85, 0.5)))


def showing_colors(rows: list) -> dict[tuple, dict]:
    """Pastel RGB color (0-1 floats) per distinct showing found in rows."""
    return {key: _pastel(hue) for key, hue in showing_hues(rows).items()}
