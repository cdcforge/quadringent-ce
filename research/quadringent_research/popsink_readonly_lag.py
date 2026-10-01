"""Read-only Popsink target Statistics parser. No API, no secrets, no mutations."""

from __future__ import annotations

import re
from typing import Mapping, Sequence

DEFAULT_MAX_LAG = 1000


def parse_statistics_text(
    text: str, *, library: str
) -> dict[str, dict[str, str]]:
    """Keep the last Statistics block per table of the declared library.

    ``library`` is the declared source schema — the incumbent pipeline names
    its targets ``<LIBRARY>.<TABLE>``. Nothing is pinned to an installation.
    """

    if not isinstance(library, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,29}", library):
        raise ValueError("a declared source library is required")
    pattern = re.compile(
        re.escape(library) + r"\.([A-Z0-9]+) -> \1 Statistics \[(.*)\]"
    )
    seen: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        match = pattern.search(line)
        if match is None:
            continue
        fields: dict[str, str] = {}
        for part in match.group(2).split(","):
            if "=" not in part:
                continue
            key, value = part.strip().split("=", 1)
            fields[key] = value.strip()
        seen[match.group(1)] = fields
    return seen


def evaluate_tail(
    stats: Mapping[str, Mapping[str, str]],
    *,
    watched_tables: Sequence[str],
    max_lag: int = DEFAULT_MAX_LAG,
) -> dict[str, object]:
    """True only when every declared table is present and at or below max_lag."""

    if (
        not isinstance(watched_tables, (tuple, list))
        or not watched_tables
        or any(
            not isinstance(table, str)
            or re.fullmatch(r"[A-Z0-9_]{1,64}", table) is None
            for table in watched_tables
        )
    ):
        raise ValueError("watched tables must be a declared non-empty list")
    watched = tuple(watched_tables)
    lags = {
        table: int(float(stats.get(table, {}).get("topicLag") or 0))
        for table in watched
    }
    calm = all(table in stats for table in watched) and all(
        lags[table] <= max_lag for table in watched
    )
    return {
        "calm": calm,
        "lags": lags,
        "max_lag": max(lags.values()) if lags else None,
        "watched": list(watched),
        "threshold": max_lag,
        "tables_seen": sorted(stats),
    }
