"""Traduction QTIMZON → règle de capture sur les noms IBM i réels.

Constaté le 24 septembre 2026 sur un IBM i 7.5 réel : QTIMZON valait
``QP0100CET`` (UTC+1) et la table précédente, inventée, ne le connaissait pas
(elle associait à tort ``QN0100CET`` — UTC-1 — à Europe/Paris)."""
from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quadringent_control_plane.v2.services.source_probe import QTIMZON_TO_CAPTURE_ZONE, map_qtimzon_to_capture_zone

NAMES = {
    line.strip()
    for line in (Path(__file__).parent / "fixtures/ibmi_timzon_names.txt").read_text().splitlines()
    if line.strip() and not line.startswith("#")
}


def test_every_mapped_name_is_a_real_ibm_i_time_zone_description() -> None:
    assert set(QTIMZON_TO_CAPTURE_ZONE) <= NAMES


@pytest.mark.parametrize("name,zone", [("QP0100CET", "IBM:QP0100CET"), ("QN0500EST", "America/New_York"),
                                       ("QP0900JST", "Asia/Tokyo"), ("Q0000UTC", "UTC"),
                                       ("Q0000GMT", "UTC")])
def test_common_base_descriptions(name: str, zone: str) -> None:
    assert map_qtimzon_to_capture_zone(name) == zone


def test_sign_and_offset_of_the_name_match_the_iana_zone_in_winter() -> None:
    """``QPhhmm`` = UTC+hh:mm, ``QNhhmm`` = UTC-hh:mm : l'IANA choisi doit avoir
    ce décalage en heure standard (janvier au nord, juillet au sud)."""
    from datetime import datetime

    southern = {"Australia/Sydney", "Australia/Adelaide", "Pacific/Auckland"}
    for name, iana in QTIMZON_TO_CAPTURE_ZONE.items():
        if iana.startswith("IBM:"):
            assert name == "QP0100CET"
            continue  # règle IBM dédiée, vérifiée côté Java aux deux saisons
        if name.startswith("Q0000"):
            expected = 0
        else:
            sign = 1 if name[1] == "P" else -1
            expected = sign * (int(name[2:4]) * 60 + int(name[4:6]))
        month = 7 if iana in southern else 1
        offset = datetime(2026, month, 15, 12, tzinfo=ZoneInfo(iana)).utcoffset().total_seconds() / 60
        assert offset == expected, (name, iana, offset)


@pytest.mark.parametrize("name", ["QP0100CET2", "Q0000GMT2", "QN0500EST3", "QP0300MSK", "INCONNU"])
def test_variants_and_unknown_names_require_confirmation(name: str) -> None:
    assert map_qtimzon_to_capture_zone(name) is None
