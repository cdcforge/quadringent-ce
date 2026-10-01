"""Sonde de source et découverte de tables via Jobs Kubernetes éphémères —
``v2/executor/diagnostic_jobs.py`` (chantier « prod-wiring »).

Toujours hors ligne : ``jobs_client``/``pods_client``/``secrets_client`` sont
des faux en mémoire, jamais un vrai cluster.
"""

from __future__ import annotations

import json

import pytest

from quadringent_control_plane.k8s_jobs import JobsApiError
from quadringent_control_plane.k8s_secrets import SecretsApiError
from quadringent_control_plane.v2.executor.diagnostic_jobs import (
    DiagnosticJobConfig,
    DiagnosticJobError,
    KubernetesJobSourceProbe,
    KubernetesJobTableDiscoveryClient,
    SourceProbeUnavailableError,
    TableDiscoveryUnavailableError,
    run_diagnostic_job,
)
from quadringent_control_plane.v2.services.source_probe import SourceProbeRequest


class FakeJobsClient:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self._job: dict | None = None
        self.fail_create = False
        self.fail_read = False

    def create_job(self, manifest: dict) -> dict:
        if self.fail_create:
            raise JobsApiError("jobs_api_rejected", "refus")
        self.created.append(manifest)
        self._job = {
            "metadata": manifest["metadata"],
            "status": {"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]},
        }
        return self._job

    def read_job(self, name: str) -> dict | None:
        if self.fail_read:
            raise JobsApiError("jobs_api_unavailable", "indisponible")
        return self._job

    def delete_job(self, name: str) -> None:
        self.deleted.append(name)


class NeverTerminalJobsClient(FakeJobsClient):
    def create_job(self, manifest: dict) -> dict:
        self.created.append(manifest)
        self._job = {"metadata": manifest["metadata"], "status": {"active": 1}}
        return self._job


class FakePodsClient:
    def __init__(self, log: str | None) -> None:
        self._log = log
        self.pod_names = ("probe-pod-abc",)

    def list_pod_names(self, *, label_selector: str, limit: int) -> tuple[str, ...]:
        return self.pod_names

    def read_pod_log(self, name: str, *, tail_lines: int = 200, **_kwargs) -> str | None:
        return self._log


class FakeSecretsClient:
    def __init__(self) -> None:
        self.upserted: list[tuple[str, dict]] = []
        self.deleted: list[str] = []

    def upsert_secret(self, name: str, string_data: dict) -> None:
        self.upserted.append((name, dict(string_data)))

    def delete_secret(self, name: str) -> None:
        self.deleted.append(name)


RESULT_PREFIX = "quadringent_probe_result="


def _manifest(name: str = "qdt-probe-abc") -> dict:
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name}, "spec": {}}


def test_run_diagnostic_job_returns_the_result_line_and_cleans_up() -> None:
    jobs = FakeJobsClient()
    pods = FakePodsClient(f"2026-09-24T10:00:00Z {RESULT_PREFIX}{{\"ok\":true}}")
    secrets = FakeSecretsClient()

    result = run_diagnostic_job(
        jobs_client=jobs,
        pods_client=pods,
        secrets_client=secrets,
        manifest=_manifest(),
        job_name="qdt-probe-abc",
        secret_name="qdt-probe-secret-abc",
        secret_password="s3cret",
        timeout_seconds=5.0,
        result_prefix=RESULT_PREFIX,
        sleep=lambda _seconds: None,
    )

    assert result == '{"ok":true}'
    assert secrets.upserted == [("qdt-probe-secret-abc", {"ISERIES_PASSWORD": "s3cret"})]
    # Nettoyage systématique, même en succès.
    assert jobs.deleted == ["qdt-probe-abc"]
    assert secrets.deleted == ["qdt-probe-secret-abc"]


def test_run_diagnostic_job_never_puts_the_password_in_the_manifest() -> None:
    jobs = FakeJobsClient()
    pods = FakePodsClient(f"{RESULT_PREFIX}{{}}")
    secrets = FakeSecretsClient()
    manifest = _manifest()

    run_diagnostic_job(
        jobs_client=jobs,
        pods_client=pods,
        secrets_client=secrets,
        manifest=manifest,
        job_name="qdt-probe-abc",
        secret_name="qdt-probe-secret-abc",
        secret_password="s3cret",
        timeout_seconds=5.0,
        result_prefix=RESULT_PREFIX,
        sleep=lambda _seconds: None,
    )

    assert "s3cret" not in json.dumps(jobs.created)


def test_run_diagnostic_job_times_out_and_still_cleans_up() -> None:
    jobs = NeverTerminalJobsClient()
    pods = FakePodsClient(None)
    secrets = FakeSecretsClient()
    clock = {"t": 0.0}

    def monotonic() -> float:
        return clock["t"]

    def sleep(seconds: float) -> None:
        clock["t"] += seconds

    with pytest.raises(DiagnosticJobError) as excinfo:
        run_diagnostic_job(
            jobs_client=jobs,
            pods_client=pods,
            secrets_client=secrets,
            manifest=_manifest(),
            job_name="qdt-probe-abc",
            secret_name="qdt-probe-secret-abc",
            secret_password="s3cret",
            timeout_seconds=3.0,
            result_prefix=RESULT_PREFIX,
            sleep=sleep,
            monotonic=monotonic,
            poll_interval_seconds=1.0,
        )
    assert excinfo.value.code == "timeout"
    assert jobs.deleted == ["qdt-probe-abc"]
    assert secrets.deleted == ["qdt-probe-secret-abc"]


def test_run_diagnostic_job_cleans_up_the_secret_even_when_job_creation_fails() -> None:
    jobs = FakeJobsClient()
    jobs.fail_create = True
    pods = FakePodsClient(None)
    secrets = FakeSecretsClient()

    with pytest.raises(DiagnosticJobError) as excinfo:
        run_diagnostic_job(
            jobs_client=jobs,
            pods_client=pods,
            secrets_client=secrets,
            manifest=_manifest(),
            job_name="qdt-probe-abc",
            secret_name="qdt-probe-secret-abc",
            secret_password="s3cret",
            timeout_seconds=5.0,
            result_prefix=RESULT_PREFIX,
            sleep=lambda _seconds: None,
        )
    assert excinfo.value.code == "executor_unavailable"
    assert secrets.deleted == ["qdt-probe-secret-abc"]


def test_run_diagnostic_job_fails_closed_when_no_result_line_is_present() -> None:
    jobs = FakeJobsClient()
    pods = FakePodsClient("no result here")
    secrets = FakeSecretsClient()

    with pytest.raises(DiagnosticJobError) as excinfo:
        run_diagnostic_job(
            jobs_client=jobs,
            pods_client=pods,
            secrets_client=secrets,
            manifest=_manifest(),
            job_name="qdt-probe-abc",
            secret_name="qdt-probe-secret-abc",
            secret_password="s3cret",
            timeout_seconds=5.0,
            result_prefix=RESULT_PREFIX,
            sleep=lambda _seconds: None,
        )
    assert excinfo.value.code == "executor_unavailable"


def test_kubernetes_job_source_probe_builds_a_full_result() -> None:
    payload = {
        "network": {"ok": True, "detail": "tcp ok"},
        "tls": {"ok": True, "detail": "tls ok", "fingerprint": "aa:bb"},
        "authentication": {"ok": True, "detail": "auth ok"},
        "ibmi_version": "7.5",
        "qtimzon": "QP0100CET",
    }
    jobs = FakeJobsClient()
    pods = FakePodsClient("quadringent_probe_result=" + json.dumps(payload))
    secrets = FakeSecretsClient()
    probe = KubernetesJobSourceProbe(
        jobs_client=jobs,
        pods_client=pods,
        secrets_client=secrets,
        config=DiagnosticJobConfig(namespace="quadringent", image="registry.example/capture:1.0.0"),
        run_id_factory=lambda: "abc123",
    )

    result = probe.probe(
        SourceProbeRequest(ibmi_host="192.0.2.10", ibmi_user="QSECOFR", secret_value="s3cret")
    )

    assert result.reachable()
    assert result.tls_fingerprint == "aa:bb"
    assert result.ibmi_version == "7.5"
    assert result.detected_timezone == "IBM:QP0100CET"
    assert secrets.deleted, "le Secret éphémère doit être nettoyé"


def test_kubernetes_job_source_probe_raises_a_safe_error_on_timeout() -> None:
    jobs = NeverTerminalJobsClient()
    pods = FakePodsClient(None)
    secrets = FakeSecretsClient()
    probe = KubernetesJobSourceProbe(
        jobs_client=jobs,
        pods_client=pods,
        secrets_client=secrets,
        config=DiagnosticJobConfig(
            namespace="quadringent", image="registry.example/capture:1.0.0", probe_timeout_seconds=0.0
        ),
        run_id_factory=lambda: "abc123",
    )

    with pytest.raises(SourceProbeUnavailableError) as excinfo:
        probe.probe(SourceProbeRequest(ibmi_host="192.0.2.10", ibmi_user="QSECOFR", secret_value="s3cret"))
    assert excinfo.value.code == "timeout"


# --- CA épinglé provisionné/nettoyé autour du Job (objectif B) -------------


def test_kubernetes_job_source_probe_never_provisions_a_ca_secret_without_a_pin() -> None:
    payload = {
        "network": {"ok": True, "detail": "tcp ok"},
        "tls": {"ok": True, "detail": "tls ok", "fingerprint": "aa:bb", "trust": "system"},
        "authentication": {"ok": True, "detail": "auth ok"},
        "ibmi_version": "7.5",
        "qtimzon": "QP0100CET",
    }
    jobs = FakeJobsClient()
    pods = FakePodsClient("quadringent_probe_result=" + json.dumps(payload))
    secrets = FakeSecretsClient()
    probe = KubernetesJobSourceProbe(
        jobs_client=jobs,
        pods_client=pods,
        secrets_client=secrets,
        config=DiagnosticJobConfig(namespace="quadringent", image="registry.example/capture:1.0.0"),
        run_id_factory=lambda: "abc123",
    )

    probe.probe(SourceProbeRequest(ibmi_host="192.0.2.10", ibmi_user="QSECOFR", secret_value="s3cret"))

    ca_upserts = [name for name, _ in secrets.upserted if "ca" in name]
    assert ca_upserts == []
    manifest = jobs.created[0]
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert "volumeMounts" not in container


def test_kubernetes_job_source_probe_provisions_and_cleans_up_the_pinned_ca_secret() -> None:
    payload = {
        "network": {"ok": True, "detail": "tcp ok"},
        "tls": {"ok": True, "detail": "empreinte épinglée confirmée", "fingerprint": "aa:bb", "trust": "pinned"},
        "authentication": {"ok": True, "detail": "auth ok"},
        "ibmi_version": "7.5",
        "qtimzon": "QP0100CET",
    }
    jobs = FakeJobsClient()
    pods = FakePodsClient("quadringent_probe_result=" + json.dumps(payload))
    secrets = FakeSecretsClient()
    probe = KubernetesJobSourceProbe(
        jobs_client=jobs,
        pods_client=pods,
        secrets_client=secrets,
        config=DiagnosticJobConfig(namespace="quadringent", image="registry.example/capture:1.0.0"),
        run_id_factory=lambda: "abc123",
    )
    pem = "-----BEGIN CERTIFICATE-----\nPINNED\n-----END CERTIFICATE-----\n"

    result = probe.probe(
        SourceProbeRequest(
            ibmi_host="192.0.2.10",
            ibmi_user="QSECOFR",
            secret_value="s3cret",
            tls_trust="pinned",
            pinned_fingerprint="aa:bb",
            pinned_pem=pem,
        )
    )

    assert result.tls_trust == "pinned"
    ca_upserts = [(name, data) for name, data in secrets.upserted if "ca" in name]
    assert len(ca_upserts) == 1
    ca_name, ca_data = ca_upserts[0]
    assert ca_data == {"ca.pem": pem}
    assert ca_name in secrets.deleted, "le Secret CA éphémère doit être nettoyé comme le mot de passe"
    manifest = jobs.created[0]
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    mount = next(m for m in container["volumeMounts"] if m["name"] == "ibmi-ca")
    assert mount["mountPath"] == "/etc/quadringent/ibmi-ca"
    volume = next(v for v in manifest["spec"]["template"]["spec"]["volumes"] if v["name"] == "ibmi-ca")
    assert volume["secret"]["secretName"] == ca_name
