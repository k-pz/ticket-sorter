"""colors.py — one hue per distinct showing/person, neighbors kept far apart on the wheel."""
import colorsys

import pytest

from octopus_tickets_sorter.colors import _hues, person_hues, showing_colors, showing_hues, showing_key


def test_showing_key_ignores_who_holds_the_ticket(make_ticket):
    t = make_ticket()
    assert showing_key(t) == (t["start_time"], t["movie"], t["address"])
    assert showing_key(make_ticket(issued_to="Ktoś Inny", ticket_code="T-2")) == showing_key(t)


def test_hues_follow_first_seen_order_alternating_halves():
    # "c" first even though it sorts last; then halves alternate: 0.0 / 0.5 / 0.25
    assert _hues(["c", "a", "b", "a", "c"]) == {"c": 0.0, "a": 0.5, "b": 0.25}


def test_showing_hues_collapse_tickets_to_the_same_showing(make_ticket):
    together = make_ticket(issued_to="Jan")
    apart = make_ticket(movie="Inny Film")
    hues = showing_hues([make_ticket(), together, apart])
    assert len(hues) == 2
    assert hues[showing_key(together)] != hues[showing_key(apart)]


def test_person_hues_key_on_the_holder(make_ticket):
    rows = [make_ticket(issued_to="Jan"), make_ticket(issued_to="Ewa"), make_ticket(issued_to="Jan")]
    assert len(showing_hues(rows)) == 1  # one showing...
    assert len(person_hues(rows)) == 2  # ...but two people, who must not share a color


@pytest.mark.parametrize("n", range(1, 16))
def test_hues_stay_in_range_and_a_quarter_wheel_apart(n):
    values = list(_hues(range(n)).values())  # the module's own sweep
    assert all(0 <= h < 1 for h in values)
    for a, b in zip(values, values[1:]):
        dist = abs(a - b) % 1.0
        assert min(dist, 1.0 - dist) >= 0.25, (n, a, b)


def test_showing_colors_are_pastel_rgb(make_ticket):
    colors = showing_colors([make_ticket(movie=f"m{i}") for i in range(3)] * 2)
    assert len(colors) == 3
    for c in colors.values():
        assert list(c) == ["red", "green", "blue"]
        assert all(0 <= v <= 1 for v in c.values())
        # pastel means washed out, not just any color: lightness stays at 0.85
        assert colorsys.rgb_to_hls(*c.values())[1] == pytest.approx(0.85)


def test_no_rows():
    assert showing_hues([]) == person_hues([]) == showing_colors([]) == {}
