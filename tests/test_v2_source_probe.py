"""Sonde IBM i réelle pour ``POST /v2/sources/{id}/test`` (chantier 4, item 2).

Couvre la traduction ``QTIMZON`` -> règle de capture (table connue, valeur inconnue
jamais devinée), l'assemblage du résultat, et le câblage dans
``SourcesService.test`` : sans sonde injectée le comportement historique est
conservé ; avec une sonde factice, les champs détectés sont persistés
seulement si la source est joignable.
"""

from __future__ import annotations

import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.source_probe import (
    ProbeOutcome,
    SourceProbeRequest,
    SourceProbeResult,
    build_probe_result,
    map_qtimzon_to_capture_zone,
)
from quadringent_control_plane.v2.services.sources import SourcesService


# --- Traduction QTIMZON -> règle de capture ---------------------------------


@pytest.mark.parametrize(
    "qtimzon,expected",
    [
        ("QP0100CET", "IBM:QP0100CET"),
        ("qp0100cet", "IBM:QP0100CET"),  # insensible à la casse
        ("QN0500EST", "America/New_York"),
        ("Q0000UTC", "UTC"),
        ("Q0000GMT", "UTC"),
    ],
)
def test_known_qtimzon_values_map_to_capture_zone(qtimzon: str, expected: str) -> None:
    assert map_qtimzon_to_capture_zone(qtimzon) == expected


def test_unknown_qtimzon_is_never_guessed() -> None:
    assert map_qtimzon_to_capture_zone("QN9999ZZZ") is None


def test_build_probe_result_flags_ambiguous_timezone_without_inventing_one() -> None:
    result = build_probe_result(
        network=ProbeOutcome(True, "tcp:9471 ok"),
        tls=ProbeOutcome(True, "chaîne validée"),
        tls_fingerprint="aa:bb:cc",
        authentication=ProbeOutcome(True, "authentifié"),
        ibmi_version="V7R5M0",
        qtimzon="QN9999ZZZ",
    )
    assert result.detected_timezone is None
    assert result.timezone_ambiguous is True
    assert result.reachable() is True


def test_build_probe_result_resolves_known_timezone() -> None:
    result = build_probe_result(
        network=ProbeOutcome(True, "ok"),
        tls=ProbeOutcome(True, "ok"),
        tls_fingerprint="aa:bb:cc",
        authentication=ProbeOutcome(True, "ok"),
        ibmi_version="V7R5M0",
        qtimzon="QP0100CET",
    )
    assert result.detected_timezone == "IBM:QP0100CET"
    assert result.timezone_ambiguous is False


def test_unreachable_source_is_never_marked_reachable() -> None:
    result = build_probe_result(
        network=ProbeOutcome(False, "connexion refusée"),
        tls=ProbeOutcome(False, "non tentée"),
        tls_fingerprint=None,
        authentication=ProbeOutcome(False, "non tentée"),
        ibmi_version=None,
        qtimzon=None,
    )
    assert result.reachable() is False


# --- Câblage dans SourcesService.test ---------------------------------------


class _FakeProbe:
    def __init__(self, result: SourceProbeResult) -> None:
        self.result = result
        self.requests: list[SourceProbeRequest] = []

    def probe(self, request: SourceProbeRequest) -> SourceProbeResult:
        self.requests.append(request)
        return self.result


@pytest.fixture()
def sources_service(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'sources_probe.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "org1", "name": "Client unique"})
    secret_box = SecretBox(SecretBox.generate_key())
    service = SourcesService(engine, secret_box, org_id="org1")
    try:
        yield service
    finally:
        engine.dispose()


def test_without_a_probe_test_keeps_the_pre_chantier4_behaviour(sources_service) -> None:
    created = sources_service.create(
        display_name="Site principal",
        ibmi_host="as400.example.test",
        ibmi_user="QSVCUSER",
        secret_value="un-secret",
    )
    result = sources_service.test(created.id)
    assert result["reachable"] == "unknown"
    assert result["secret_set"] is True


def test_reachable_probe_persists_detected_fields(sources_service) -> None:
    created = sources_service.create(
        display_name="Site principal",
        ibmi_host="as400.example.test",
        ibmi_user="QSVCUSER",
        secret_value="un-secret",
    )
    outcome_ok = ProbeOutcome(True, "ok")
    probe = _FakeProbe(
        build_probe_result(
            network=outcome_ok,
            tls=outcome_ok,
            tls_fingerprint="11:22:33:44",
            authentication=outcome_ok,
            ibmi_version="V7R5M0",
            qtimzon="QP0100CET",
        )
    )

    result = sources_service.test(created.id, probe=probe)

    assert result["reachable"] is True
    assert result["detected_timezone"] == "IBM:QP0100CET"
    fetched = sources_service.get(created.id)
    assert fetched.tls_fingerprint == "11:22:33:44"
    assert fetched.detected_timezone == "IBM:QP0100CET"
    assert fetched.detected_version == "V7R5M0"
    # Le secret déchiffré passe à la sonde mais ne revient jamais dans la réponse HTTP.
    assert probe.requests[0].secret_value == "un-secret"
    assert "un-secret" not in str(result)


def test_unreachable_probe_never_persists_stale_or_guessed_fields(sources_service) -> None:
    created = sources_service.create(
        display_name="Site principal",
        ibmi_host="as400.example.test",
        ibmi_user="QSVCUSER",
        secret_value="un-secret",
    )
    outcome_ko = ProbeOutcome(False, "injoignable")
    probe = _FakeProbe(
        build_probe_result(
            network=outcome_ko,
            tls=ProbeOutcome(False, "non tentée"),
            tls_fingerprint=None,
            authentication=ProbeOutcome(False, "non tentée"),
            ibmi_version=None,
            qtimzon=None,
        )
    )

    result = sources_service.test(created.id, probe=probe)

    assert result["reachable"] is False
    fetched = sources_service.get(created.id)
    assert fetched.detected_timezone is None
    assert fetched.tls_fingerprint is None


def test_corrupted_secret_fails_closed_even_with_a_probe_injected(sources_service, monkeypatch) -> None:
    created = sources_service.create(
        display_name="Site principal",
        ibmi_host="as400.example.test",
        ibmi_user="QSVCUSER",
        secret_value="un-secret",
    )

    def _boom(_self, _ciphertext):
        raise ValueError("clé rotée")

    monkeypatch.setattr(type(sources_service._secret_box), "decrypt", _boom)
    with pytest.raises(ValueError):
        sources_service.test(created.id, probe=_FakeProbe(build_probe_result(
            network=ProbeOutcome(True, "ok"),
            tls=ProbeOutcome(True, "ok"),
            tls_fingerprint="x",
            authentication=ProbeOutcome(True, "ok"),
            ibmi_version=None,
            qtimzon=None,
        )))


# --- Épinglage TLS (objectif B, chantier 2026-09-24) ------------------------


def test_unknown_trust_is_persisted_without_ever_storing_a_pem_before_pinning(sources_service) -> None:
    """Une autorité inconnue mesurée (trust="unknown") est un résultat
    exploitable — jamais silencieusement acceptée comme une racine connue —
    mais son PEM n'est jamais conservé tant que l'opérateur n'a pas épinglé."""

    created = sources_service.create(
        display_name="Site principal",
        ibmi_host="as400.example.test",
        ibmi_user="QSVCUSER",
        secret_value="un-secret",
    )
    probe = _FakeProbe(
        build_probe_result(
            network=ProbeOutcome(True, "ok"),
            tls=ProbeOutcome(True, "autorité non reconnue — mesure seule"),
            tls_fingerprint="aa" * 32,
            authentication=ProbeOutcome(False, "non tentée (confiance non établie)"),
            ibmi_version=None,
            qtimzon=None,
            tls_trust="unknown",
            tls_certificate_pem="-----BEGIN CERTIFICATE-----\nMEASURED\n-----END CERTIFICATE-----\n",
        )
    )

    result = sources_service.test(created.id, probe=probe)

    assert result["tls"]["trust"] == "unknown"
    fetched = sources_service.get(created.id)
    assert fetched.tls_trust == "unknown"


def test_pinning_persists_the_pem_only_when_the_probe_confirms_the_fingerprint(sources_service) -> None:
    created = sources_service.create(
        display_name="Site principal",
        ibmi_host="as400.example.test",
        ibmi_user="QSVCUSER",
        secret_value="un-secret",
    )
    pem = "-----BEGIN CERTIFICATE-----\nPINNED\n-----END CERTIFICATE-----\n"
    fingerprint = "bb" * 32
    probe = _FakeProbe(
        build_probe_result(
            network=ProbeOutcome(True, "ok"),
            tls=ProbeOutcome(True, "empreinte épinglée confirmée"),
            tls_fingerprint=fingerprint,
            authentication=ProbeOutcome(True, "ok"),
            ibmi_version="V7R5M0",
            qtimzon="QP0100CET",
            tls_trust="pinned",
            tls_certificate_pem=pem,
        )
    )

    result = sources_service.test(
        created.id, probe=probe, tls_trust="pinned", pinned_fingerprint=fingerprint, pinned_pem=pem
    )

    assert result["reachable"] is True
    assert probe.requests[0].pinned_pem == pem
    assert probe.requests[0].tls_trust == "pinned"
    fetched = sources_service.get(created.id)
    assert fetched.tls_trust == "pinned"

    # Un second test réutilise le PEM déjà persisté, sans que l'appelant
    # ait besoin de le repasser à chaque fois.
    probe2 = _FakeProbe(
        build_probe_result(
            network=ProbeOutcome(True, "ok"),
            tls=ProbeOutcome(True, "empreinte épinglée confirmée"),
            tls_fingerprint=fingerprint,
            authentication=ProbeOutcome(True, "ok"),
            ibmi_version="V7R5M0",
            qtimzon="QP0100CET",
            tls_trust="pinned",
            tls_certificate_pem=pem,
        )
    )
    sources_service.test(created.id, probe=probe2)
    assert probe2.requests[0].pinned_pem == pem
    assert probe2.requests[0].tls_trust == "pinned"


def test_a_rejected_pin_never_overwrites_the_previously_trusted_pem(sources_service) -> None:
    """Une empreinte épinglée qui ne correspond plus (certificat renouvelé
    côté IBM i, ou tentative divergente) est un échec explicite — jamais un
    remplacement silencieux du PEM déjà en confiance."""

    created = sources_service.create(
        display_name="Site principal",
        ibmi_host="as400.example.test",
        ibmi_user="QSVCUSER",
        secret_value="un-secret",
    )
    mismatch_probe = _FakeProbe(
        build_probe_result(
            network=ProbeOutcome(True, "ok"),
            tls=ProbeOutcome(False, "empreinte du certificat différente de l'empreinte épinglée"),
            tls_fingerprint="cc" * 32,
            authentication=ProbeOutcome(False, "non tentée (réseau/TLS indisponible)"),
            ibmi_version=None,
            qtimzon=None,
            tls_trust="pinned",
            tls_certificate_pem="-----BEGIN CERTIFICATE-----\nOTHER\n-----END CERTIFICATE-----\n",
        )
    )

    result = sources_service.test(
        created.id,
        probe=mismatch_probe,
        tls_trust="pinned",
        pinned_fingerprint="dd" * 32,
        pinned_pem="-----BEGIN CERTIFICATE-----\nOTHER\n-----END CERTIFICATE-----\n",
    )

    assert result["tls"]["ok"] is False
    fetched = sources_service.get(created.id)
    assert fetched.tls_trust is None  # jamais posé sur une empreinte rejetée
