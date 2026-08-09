"""Collapse the several names one person's tickets can be issued to into a single name."""
import re
from collections import Counter
from itertools import combinations


def resolve(name: str, aliases: dict[str, str]) -> str:
    """The name a variant collapses into, or the name itself."""
    return aliases.get(name, name)


def _tokens(name: str) -> set[str]:
    """A name's parts, normalized so "ZAK"/"ŻAK" and "Wisniewska"/"Wiśniewska" match.
    Initials and other one- or two-letter parts are dropped as too weak to match on."""
    from .main import slugify  # deferred: main.py imports this module at load time

    return {t for t in slugify(name).split("-") if len(t) > 2}


def look_alikes(names, strict: bool = False) -> list[list[str]]:
    """Groups of 2+ names that may all be the same person, because they share a
    name part. With strict, only groups where one name's parts are contained in
    another's ("Jane" vs "Jane Doe") — a strong enough hint to raise
    unprompted, unlike a merely shared surname ("Anna" vs "Jan Kowalski")."""
    groups: list[tuple[set, list]] = []
    for name in names:
        tokens, members, rest = _tokens(name), [name], []
        for group_tokens, group_names in groups:
            if group_tokens & tokens:
                tokens |= group_tokens
                members += group_names
            else:
                rest.append((group_tokens, group_names))
        groups = [*rest, (tokens, members)]
    return [
        sorted(members)
        for _, members in groups
        if len(members) > 1
        and (not strict or any(a <= b or b <= a for a, b in combinations(map(_tokens, members), 2)))
    ]


def collapse(aliases: dict[str, str], names: set[str], keep: str, known=()) -> dict[str, str]:
    """Return aliases with every known name that resolves into `names` pointing at `keep`
    instead. Existing entries are rewired rather than chained, so the map stays one hop deep."""
    universe = {*aliases, *known, *names}
    updated = aliases | {n: keep for n in universe if resolve(n, aliases) in names}
    updated.pop(keep, None)  # the name we keep is canonical, never an alias of itself
    return updated


def _prompt(text: str) -> str:
    try:
        return input(text).strip()
    except EOFError:  # non-interactive stdin: same as answering "done"
        return ""


def merge_names(counts: Counter, aliases: dict[str, str]) -> dict[str, str]:
    """Ask which of the parsed names are the same person, one group at a time,
    and return the updated variant -> name map."""
    if not counts:
        print("No tickets to merge names from.")
        return aliases

    while True:
        totals = Counter()
        for raw, n in counts.items():
            totals[resolve(raw, aliases)] += n
        names = sorted(totals)
        width = max(len(n) for n in names)

        print(f"\n{len(names)} names across {sum(totals.values())} tickets:")
        for i, name in enumerate(names, 1):
            print(f"  {i:3}  {name.ljust(width)}  {totals[name]:3}")
        if groups := look_alikes(names):
            hint = "   ".join("+".join(str(names.index(n) + 1) for n in g) for g in groups)
            print(f"\n  maybe the same person:  {hint}")

        answer = _prompt('\nMerge which names? (e.g. "1 2 5", blank when done): ')
        if not answer:
            return aliases

        picked = sorted({int(d) for d in re.findall(r"\d+", answer)})
        if len(picked) < 2 or picked[0] < 1 or picked[-1] > len(names):
            print(f"  pick two or more numbers between 1 and {len(names)}.")
            continue

        selected = [names[i - 1] for i in picked]
        print()
        for i, name in enumerate(selected, 1):
            print(f"  {i:3}  {name}")
        keep = _prompt("Keep which name? (number, or type a different one; blank to cancel): ")
        if not keep:
            continue
        if keep.isdigit():
            if not 1 <= int(keep) <= len(selected):
                print(f"  pick a number between 1 and {len(selected)}.")
                continue
            keep = selected[int(keep) - 1]

        aliases = collapse(aliases, set(selected), keep, counts)
        print(f'  collapsed {len(selected)} names into "{keep}".')
