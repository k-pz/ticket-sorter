"""Name collapsing, including the interactive merge loop.

merge_names talks to the terminal, so it is driven by a real stdin and read back through
capsys — nothing about it is stubbed out beyond stdin.
"""
import io
import sys
from collections import Counter

import pytest
from octopus_tickets_sorter.names import _tokens, collapse, look_alikes, merge_names, resolve


@pytest.fixture
def answers(monkeypatch):
    """Answer the prompts from stdin: input() falls back to sys.stdin.readline() when stdin
    has no file descriptor, and a spent StringIO raises EOFError the way a closed one does —
    which merge_names treats as "done", so no trailing blank answer is needed."""
    def feed(*lines):
        monkeypatch.setattr(sys, "stdin", io.StringIO("".join(f"{line}\n" for line in lines)))
    return feed


def printed(capsys):
    """Printed lines with runs of spaces collapsed, so column padding doesn't matter."""
    return [" ".join(line.split()) for line in capsys.readouterr().out.splitlines()]


# --- resolve / _tokens -------------------------------------------------------------


def test_resolve_falls_back_to_the_name_itself():
    assert resolve("Ala", {"Ala": "Ala Kot"}) == "Ala Kot"
    assert resolve("Ola", {"Ala": "Ala Kot"}) == "Ola"


def test_tokens_drops_short_parts():
    assert _tokens("J. Li Kowalski") == {"kowalski"}


def test_tokens_normalises_polish():
    assert _tokens("PAWEL ZAK") == _tokens("PAWEŁ ŻAK") == {"pawel", "zak"}
    assert _tokens("Anna Wisniewska") == _tokens("Anna Wiśniewska")


# --- look_alikes -------------------------------------------------------------------


def test_look_alikes_groups_transitively():
    # a name bridging two groups merges them
    assert look_alikes(["Ala Kot", "Ola Pies", "Ala Pies"]) == [["Ala Kot", "Ala Pies", "Ola Pies"]]


def test_look_alikes_leaves_out_names_sharing_nothing():
    assert look_alikes(["Jane", "Jane Doe", "Jan Kowalski"]) == [["Jane", "Jane Doe"]]


@pytest.mark.parametrize("pair, strict_groups", [
    (["PAWEŁ ŻAK", "PAWEŁ ZAK"], 1),
    (["Joe", "Joe “Rocket” Doe"], 1),
    (["Janina Kowalska", "Janina Kowalska-WIŚNIEWSKA"], 1),
    (["Jan Nowak", "Anna Nowak"], 0),  # a shared surname alone is too weak
])
def test_look_alikes_strict_keeps_only_containments(pair, strict_groups):
    assert len(look_alikes(pair)) == 1
    assert len(look_alikes(pair, strict=True)) == strict_groups


# --- collapse ----------------------------------------------------------------------


def test_collapse_never_maps_the_kept_name_to_itself():
    assert collapse({}, {"Ala", "Ala Kot"}, "Ala Kot") == {"Ala": "Ala Kot"}


def test_collapse_pulls_in_known_names_and_rewires_instead_of_chaining():
    a = collapse({}, {"Joe"}, "Joe Doe", known=["Joe", "Joe Doe"])
    assert a == {"Joe": "Joe Doe"}
    # merging again points the first alias straight at the new name, not through the old one
    b = collapse(a, {"Joe Doe", "J. D."}, "J. D.", known=[*a, "J. D."])
    assert b == {"Joe": "J. D.", "Joe Doe": "J. D."}


# --- merge_names -------------------------------------------------------------------


def test_merge_names_without_tickets_never_prompts(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _text: pytest.fail("prompted with no tickets"))
    aliases = {"Ala": "Ala Kot"}
    assert merge_names(Counter(), aliases) == aliases
    assert "No tickets to merge names from." in capsys.readouterr().out


@pytest.mark.parametrize("lines", [[""], []], ids=["blank", "eof"])
def test_merge_names_ends_on_blank_or_closed_stdin(lines, answers):
    answers(*lines)
    aliases = {"Ala": "Ala Kot"}
    assert merge_names(Counter({"Ala Kot": 2, "Ola": 1}), aliases) == aliases


@pytest.mark.parametrize("answer", ["1", "1 3", "0 1"])
def test_merge_names_rejects_a_pick_that_is_not_two_names_in_range(answer, answers, capsys):
    answers(answer)
    assert merge_names(Counter({"Ala": 1, "Ola": 1}), {}) == {}
    assert "pick two or more numbers between 1 and 2." in capsys.readouterr().out


def test_merge_names_keeps_by_number(answers, capsys):
    answers("1 2", "2")
    assert merge_names(Counter({"Ala Kot": 2, "Ala": 1}), {}) == {"Ala": "Ala Kot"}
    out = printed(capsys)
    assert "1 Ala 1" in out and "2 Ala Kot 2" in out
    assert "maybe the same person: 1+2" in out
    assert "1 Ala Kot 3" in out  # next round, with the two counts added up


def test_merge_names_keeps_a_name_typed_out(answers):
    answers("1 2", "Aleksandra Kot")
    aliases = merge_names(Counter({"Ala Kot": 2, "Ala": 1}), {})
    assert aliases == {"Ala": "Aleksandra Kot", "Ala Kot": "Aleksandra Kot"}


def test_merge_names_rejects_a_keep_number_out_of_range(answers, capsys):
    answers("1 2", "3")
    assert merge_names(Counter({"Ala Kot": 2, "Ala": 1}), {}) == {}
    assert "pick a number between 1 and 2." in capsys.readouterr().out


def test_merge_names_blank_keep_cancels_the_round(answers, capsys):
    answers("1 2", "")
    assert merge_names(Counter({"Ala Kot": 2, "Ala": 1}), {}) == {}
    out = printed(capsys)
    assert out.count("2 Ala Kot 2") == 2  # both names still listed separately on the next round
    assert not [line for line in out if line.startswith("collapsed")]
