"""Logique pure du point d'entrée de sonde de source (``scripts/quadringent_source_probe_job.py``).

Aucune connexion réseau réelle : le socket et le worker Java (JTOpen) sont
remplacés par des faux injectés — voir la docstring du module sous test
pour le choix des ports et le mécanisme de retour. La sonde n'utilise
jamais ODBC/pyodbc (pilote propriétaire absent de l'image de capture) :
tout le produit parle à l'IBM i via JTOpen, voir
``quadringent.java_worker.PersistentJavaWorker``.
"""

from __future__ import annotations

import ast
import hashlib
import socket
import ssl
import sys
from pathlib import Path

import pytest

import quadringent_source_probe_job as probe_job


def test_module_never_imports_pyodbc_or_odbc() -> None:
    """Garde-fou explicite : la sonde ne doit dépendre d'aucun pilote ODBC,
    propriétaire et absent de l'image de capture — voir la docstring du
    module et ``docs/orchestration.md`` §10."""

    assert "pyodbc" not in sys.modules or "pyodbc" not in probe_job.__dict__

    source = Path(probe_job.__file__).read_text(encoding="utf-8")
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    lowered = {name.lower() for name in names}
    assert not any("pyodbc" in name or name == "odbc" for name in lowered), names


class _FakeSocket:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_tcp_probe_ok_when_connect_succeeds() -> None:
    fake = _FakeSocket()
    outcome = probe_job.tcp_probe("192.0.2.10", 9476, timeout=1.0, connect=lambda addr, timeout: fake)
    assert outcome.ok is True
    assert fake.closed is True


def test_tcp_probe_fails_closed_on_os_error() -> None:
    def connect(addr, timeout):
        raise OSError("connection refused")

    outcome = probe_job.tcp_probe("192.0.2.10", 9476, timeout=1.0, connect=connect)
    assert outcome.ok is False
    assert "injoignable" in outcome.detail or "refusée" in outcome.detail


class _FakeTlsSocket:
    def __init__(self, der_certificate: bytes | None) -> None:
        self._der = der_certificate

    def getpeercert(self, binary_form: bool = False) -> bytes | None:
        assert binary_form is True
        return self._der

    def __enter__(self) -> "_FakeTlsSocket":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_tls_probe_reports_the_sha256_fingerprint() -> None:
    der = b"fake-certificate-bytes"
    expected = hashlib.sha256(der).hexdigest()

    def wrap_socket(raw_sock, server_hostname):
        return _FakeTlsSocket(der)

    outcome, fingerprint, trust, pem = probe_job.tls_probe(
        "192.0.2.10",
        9476,
        timeout=1.0,
        ca_file=None,
        tls_trust="system",
        pinned_fingerprint=None,
        connect=lambda addr, timeout: _FakeSocket(),
        wrap_socket=wrap_socket,
    )
    assert outcome.ok is True
    assert fingerprint == expected
    assert trust == "system"
    assert "BEGIN CERTIFICATE" in pem


def test_tls_probe_pinned_accepts_a_matching_fingerprint() -> None:
    der = b"fake-certificate-bytes"
    expected = hashlib.sha256(der).hexdigest()

    outcome, fingerprint, trust, pem = probe_job.tls_probe(
        "192.0.2.10",
        9476,
        timeout=1.0,
        ca_file=None,
        tls_trust="pinned",
        pinned_fingerprint=expected,
        connect=lambda addr, timeout: _FakeSocket(),
        wrap_socket=lambda raw_sock, server_hostname: _FakeTlsSocket(der),
    )
    assert outcome.ok is True
    assert fingerprint == expected
    assert trust == "pinned"
    assert pem is not None


def test_tls_probe_pinned_rejects_a_mismatched_fingerprint() -> None:
    der = b"fake-certificate-bytes"

    outcome, fingerprint, trust, pem = probe_job.tls_probe(
        "192.0.2.10",
        9476,
        timeout=1.0,
        ca_file=None,
        tls_trust="pinned",
        pinned_fingerprint="0" * 64,
        connect=lambda addr, timeout: _FakeSocket(),
        wrap_socket=lambda raw_sock, server_hostname: _FakeTlsSocket(der),
    )
    assert outcome.ok is False
    assert fingerprint is not None  # mesurée quand même, pour diagnostic
    assert pem is not None


def test_tls_probe_fails_closed_without_a_certificate() -> None:
    outcome, fingerprint, trust, pem = probe_job.tls_probe(
        "192.0.2.10",
        9476,
        timeout=1.0,
        ca_file=None,
        tls_trust="system",
        pinned_fingerprint=None,
        connect=lambda addr, timeout: _FakeSocket(),
        wrap_socket=lambda raw_sock, server_hostname: _FakeTlsSocket(None),
    )
    assert outcome.ok is False
    assert fingerprint is None
    assert trust is None
    assert pem is None


def test_tls_probe_reports_an_explicit_error_when_the_declared_ca_file_is_missing(tmp_path) -> None:
    """Reproduit le constat production : ``AS400_TLS_CA_FILE`` posé mais le
    fichier est absent de l'image — plus jamais un ``FileNotFoundError`` non
    rattrapé, une erreur de sonde explicite."""

    missing = tmp_path / "does-not-exist.pem"

    outcome, fingerprint, trust, pem = probe_job.tls_probe(
        "192.0.2.10",
        9476,
        timeout=1.0,
        ca_file=str(missing),
        tls_trust="system",
        pinned_fingerprint=None,
        connect=lambda addr, timeout: _FakeSocket(),
        wrap_socket=lambda raw_sock, server_hostname: _FakeTlsSocket(b"unused"),
    )
    assert outcome.ok is False
    assert "introuvable" in outcome.detail
    assert fingerprint is None and trust is None and pem is None


def test_tls_probe_measures_an_unrecognized_authority_as_unknown_trust() -> None:
    """Autorité privée/inconnue (design objectif B) : la poignée de main
    système échoue à la validation, une seconde tentative non validée
    mesure l'empreinte/le PEM — jamais utilisée pour authentifier — et le
    résultat porte ``trust="unknown"``, jamais accepté silencieusement."""

    der = b"private-ca-certificate-bytes"
    expected = hashlib.sha256(der).hexdigest()
    calls: list[bool] = []

    def wrap_socket(raw_sock, server_hostname):
        # Le premier appel (validation activée) échoue avec une erreur de
        # vérification de certificat ; le second (mesure seule) réussit.
        calls.append(True)
        if len(calls) == 1:
            raise ssl.SSLCertVerificationError("unable to get local issuer certificate")
        return _FakeTlsSocket(der)

    outcome, fingerprint, trust, pem = probe_job.tls_probe(
        "192.0.2.10",
        9476,
        timeout=1.0,
        ca_file=None,
        tls_trust="system",
        pinned_fingerprint=None,
        connect=lambda addr, timeout: _FakeSocket(),
        wrap_socket=wrap_socket,
    )
    assert len(calls) == 2
    assert outcome.ok is True  # mesure réussie, pas une authentification
    assert fingerprint == expected
    assert trust == "unknown"
    assert "BEGIN CERTIFICATE" in pem


def test_tls_probe_other_ssl_errors_never_fall_back_to_an_unverified_measurement() -> None:
    """Seule une erreur de vérification de chaîne déclenche la mesure sans
    validation — un timeout/protocole TLS incompatible reste un échec net,
    jamais mesuré à la place."""

    def wrap_socket(raw_sock, server_hostname):
        raise ssl.SSLError("wrong version number")

    outcome, fingerprint, trust, pem = probe_job.tls_probe(
        "192.0.2.10",
        9476,
        timeout=1.0,
        ca_file=None,
        tls_trust="system",
        pinned_fingerprint=None,
        connect=lambda addr, timeout: _FakeSocket(),
        wrap_socket=wrap_socket,
    )
    assert outcome.ok is False
    assert fingerprint is None and trust is None and pem is None


def test_parse_probe_output_reads_version_and_qtimzon() -> None:
    raw = "version\tV7.R5M0\nqtimzon\tQP0100CET\n"

    version, qtimzon = probe_job.parse_probe_output(raw)

    assert version == "V7.R5M0"
    assert qtimzon == "QP0100CET"


def test_parse_probe_output_treats_an_empty_field_as_unavailable_not_absent() -> None:
    raw = "version\tV7.R5M0\nqtimzon\t\n"

    version, qtimzon = probe_job.parse_probe_output(raw)

    assert version == "V7.R5M0"
    assert qtimzon is None


def test_parse_probe_output_ignores_unrelated_diagnostic_lines() -> None:
    raw = '{"event": "java_worker", "phase": "probe_write"}\nversion\tV7.R5M0\nprobe_done\n'

    version, qtimzon = probe_job.parse_probe_output(raw)

    assert version == "V7.R5M0"
    assert qtimzon is None


class _FakeWorker:
    def __init__(self, raw_output: str | None = None, *, raises: Exception | None = None) -> None:
        self._raw_output = raw_output
        self._raises = raises
        self.closed = False

    def probe(self) -> str:
        if self._raises is not None:
            raise self._raises
        return self._raw_output

    def close(self) -> None:
        self.closed = True


def test_authenticate_and_probe_reports_version_and_qtimzon() -> None:
    worker = _FakeWorker("version\tV7.R5M0\nqtimzon\tQP0100CET\nprobe_done\n")

    authentication, ibmi_version, qtimzon = probe_job.authenticate_and_probe(
        "192.0.2.10", "QSECOFR", "s3cret", timeout=5.0, worker_factory=lambda: worker
    )

    assert authentication.ok is True
    assert ibmi_version == "V7.R5M0"
    assert qtimzon == "QP0100CET"
    assert worker.closed is True


def test_authenticate_and_probe_fails_closed_when_the_worker_raises() -> None:
    worker = _FakeWorker(raises=RuntimeError("IBM i source probe failed:AuthenticationFailedException"))

    authentication, ibmi_version, qtimzon = probe_job.authenticate_and_probe(
        "192.0.2.10", "QSECOFR", "wrong", timeout=5.0, worker_factory=lambda: worker
    )

    assert authentication.ok is False
    assert ibmi_version is None
    assert qtimzon is None
    assert "wrong" not in authentication.detail
    assert worker.closed is True


def test_authenticate_and_probe_fails_closed_when_the_worker_cannot_even_be_constructed() -> None:
    def factory():
        raise RuntimeError("AS400_JAVA_CLASSPATH manquant")

    authentication, ibmi_version, qtimzon = probe_job.authenticate_and_probe(
        "192.0.2.10", "QSECOFR", "s3cret", timeout=5.0, worker_factory=factory
    )

    assert authentication.ok is False
    assert ibmi_version is None
    assert qtimzon is None


def test_main_prints_a_single_result_line_prefixed_and_never_logs_the_password(monkeypatch, capsys) -> None:
    monkeypatch.setenv("ISERIES_PASSWORD", "s3cret-value")
    monkeypatch.setattr(probe_job, "tcp_probe", lambda *a, **k: probe_job.ProbeOutcome(ok=False, detail="down"))

    exit_code = probe_job.main(["--host", "192.0.2.10", "--user", "QSECOFR"])

    out = capsys.readouterr().out.strip()
    assert exit_code == 0
    assert out.startswith(probe_job.RESULT_PREFIX)
    assert "s3cret-value" not in out


def test_main_fails_closed_without_a_password(monkeypatch, capsys) -> None:
    monkeypatch.delenv("ISERIES_PASSWORD", raising=False)

    exit_code = probe_job.main(["--host", "192.0.2.10", "--user", "QSECOFR"])

    assert exit_code == 2


def test_main_always_prints_a_result_line_even_on_an_unexpected_crash(monkeypatch, capsys) -> None:
    """Objectif C : plus jamais un plantage nu sans ligne de résultat — le
    bug constaté en production (``AS400_TLS_CA_FILE`` inexistant levait un
    ``FileNotFoundError`` non rattrapé) ne doit plus jamais laisser
    l'appelant sans signal exploitable."""

    monkeypatch.setenv("ISERIES_PASSWORD", "s3cret-value")

    def boom(*a, **k):
        raise RuntimeError("panne inattendue avec mot de passe s3cret-value")

    monkeypatch.setattr(probe_job, "_run_probe", boom)

    exit_code = probe_job.main(["--host", "192.0.2.10", "--user", "QSECOFR"])

    out = capsys.readouterr().out.strip()
    assert exit_code == 1
    assert out.startswith(probe_job.RESULT_PREFIX)
    payload = out[len(probe_job.RESULT_PREFIX):]
    parsed = __import__("json").loads(payload)
    assert parsed["reachable"] is False
    assert "s3cret-value" not in out


def test_worker_startup_failure_is_not_reported_as_refused_authentication() -> None:
    """Constaté sur GKE : le worker Java ne démarrait pas (variable
    manquante) et la sonde affichait « authentification refusée »."""

    def factory():
        raise RuntimeError("bounded IBM i reader did not become ready")

    authentication, _, _ = probe_job.authenticate_and_probe(
        "192.0.2.10", "QSECOFR", "s3cret", timeout=5.0, worker_factory=factory
    )
    assert authentication.ok is False
    assert "authentification refusée" not in authentication.detail
    assert "indisponible" in authentication.detail


def test_real_authentication_refusal_is_reported_as_such() -> None:
    from quadringent.java_worker import IbmiUserDisabledError, SourceAuthenticationBlockedError

    for error, expected in (
        (SourceAuthenticationBlockedError("IBM i authentication refused"), "authentification refusée"),
        (IbmiUserDisabledError("IBM i profile disabled"), "profil IBM i désactivé"),
    ):
        def factory(error=error):
            raise error

        authentication, _, _ = probe_job.authenticate_and_probe(
            "192.0.2.10", "QSECOFR", "s3cret", timeout=5.0, worker_factory=factory
        )
        assert authentication.detail.startswith(expected)


def test_diagnostic_jobs_no_longer_declare_a_time_zone_workaround() -> None:
    """Le worker de diagnostic Java (``DiagnosticWorker``) n'exige plus
    ``AS400_SOURCE_TIME_ZONE`` : le contournement ``ensure_diagnostic_time_zone``
    (fuseau neutre ``UTC``) est retiré des deux Jobs de diagnostic."""
    import quadringent_table_discovery_job as discovery_job

    for module in (probe_job, discovery_job):
        assert not hasattr(module, "ensure_diagnostic_time_zone")
        assert not hasattr(module, "DIAGNOSTIC_TIME_ZONE")


def test_probe_job_worker_factory_defaults_to_the_diagnostic_worker(monkeypatch) -> None:
    """La sonde de source démarre ``DiagnosticWorker`` par défaut, sans
    bibliothèque ni table fictives — voir docs/orchestration.md §10."""

    monkeypatch.setenv("AS400_JAVA_CLASSPATH", "/fake.jar")
    monkeypatch.delenv("AS400_JAVA_WORKER_CLASS", raising=False)

    worker = probe_job._default_worker_factory("192.0.2.10", "QSECOFR", 5.0)
    assert worker.class_name == "io.quadringent.as400.DiagnosticWorker"
    assert worker.schema is None
    assert worker.table is None


def test_table_discovery_job_worker_defaults_to_the_diagnostic_worker(monkeypatch) -> None:
    import quadringent_table_discovery_job as discovery_job

    monkeypatch.setenv("AS400_JAVA_CLASSPATH", "/fake.jar")
    monkeypatch.delenv("AS400_JAVA_WORKER_CLASS", raising=False)
    args = discovery_job.parse_args(["--host", "192.0.2.10", "--user", "QSECOFR"])
    worker = discovery_job.build_worker(args)
    assert worker.class_name == "io.quadringent.as400.DiagnosticWorker"
    assert worker.schema is None
    assert worker.table is None


def test_diagnostic_jobs_ignore_the_capture_worker_class_of_the_image(monkeypatch) -> None:
    """Constaté sur GKE : l'image capture fixe ``AS400_JAVA_WORKER_CLASS`` au
    worker de capture ; les diagnostics le reprenaient et échouaient
    (« ISERIES_TABLE is required »). Ils utilisent toujours DiagnosticWorker."""
    import argparse

    import quadringent.java_worker as java_worker
    import quadringent_table_discovery_job as discovery_job

    captured: list[str] = []

    class _Recorder:
        def __init__(self, **kwargs) -> None:
            captured.append(kwargs["class_name"])

        def probe(self) -> str:
            return "version\t7.5\nqtimzon\tQP0100CET\n"

        def close(self) -> None:
            pass

    monkeypatch.setenv("AS400_JAVA_WORKER_CLASS", "io.quadringent.as400.PersistentJournalWorker")
    monkeypatch.setenv("AS400_JAVA_CLASSPATH", "/app/probe.jar")
    monkeypatch.setattr(java_worker, "PersistentJavaWorker", _Recorder)
    monkeypatch.setattr(discovery_job, "PersistentJavaWorker", _Recorder)

    probe_job.authenticate_and_probe("192.0.2.10", "QUSER", "s3cret", timeout=5.0)
    discovery_job.build_worker(argparse.Namespace(host="192.0.2.10", user="QUSER"))
    assert captured == ["io.quadringent.as400.DiagnosticWorker"] * 2
