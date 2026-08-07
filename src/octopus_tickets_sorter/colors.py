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
    """Hue (0-1) per distinct showing found in rows. Consecutive showings
    (by first-seen order, i.e. by start time) alternate between the two
    halves of the color wheel — roughly cool vs warm — so neighbors are
    always near-complementary instead of two slivers of the same hue."""
    index = showing_indices(rows)
    n = len(index)
    evens, odds = (n + 1) // 2, n // 2
    hues = {}
    for key, i in index.items():
        pos, half = i // 2, i % 2
        count = evens if half == 0 else odds
        hues[key] = half * 0.5 + (pos / count) * 0.5
    return hues


def _pastel(hue: float) -> dict:
    return dict(zip("red green blue".split(), colorsys.hls_to_rgb(hue, 0.85, 0.5)))


def showing_colors(rows: list) -> dict[tuple, dict]:
    """Pastel RGB color (0-1 floats) per distinct showing found in rows."""
    return {key: _pastel(hue) for key, hue in showing_hues(rows).items()}


if __name__ == "__main__":
    # Worst case (proven by sweeping n up to 200) is 0.25, at n=3 and n=4;
    # it only improves toward 0.5 (complementary) as n grows.
    for n in range(1, 60):
        rows = [{"start_time": i, "movie": "m", "address": "a"} for i in range(n)]
        hues = showing_hues(rows)
        values = [hues[showing_key(t)] for t in rows]
        assert all(0 <= h < 1 for h in values)
        for a, b in zip(values, values[1:]):
            dist = abs(a - b) % 1.0
            assert min(dist, 1.0 - dist) >= 0.25, (n, a, b)
    print("ok")
