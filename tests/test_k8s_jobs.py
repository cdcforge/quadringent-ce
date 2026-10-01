from __future__ import annotations

import json
from pathlib import Path

import pytest

from quadringent_control_plane.k8s_jobs import (
    CODE_ALREADY_EXISTS,
    CODE_INVALID_RESPONSE,
    CODE_NOT_CONFIGURED,
    CODE_REJECTED,
    CODE_UNAUTHORIZED,
    CODE_UNAVAILABLE,
    JobAlreadyExists,
    JobsApiError,
    JobsResponse,
    KubernetesJobsClient,
    ServiceAccountContext,
    https_transport,
    is_dns_label,
)


NAMESPACE = "quadringent-demo"


def context(tmp_path: Path, *, token: str = "header.payload.signature") -> ServiceAccountContext:
    (tmp_path / "token").write_text(token + "\n", encoding="utf-8")
    (tmp_path / "ca.crt").write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    (tmp_path / "namespace").write_text(NAMESPACE, encoding="utf-8")
    return ServiceAccountContext.load(tmp_path)


class Recorder:
    def __init__(self, responses: list[JobsResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, bytes | None]] = []

    def __call__(
        self, method: str, path: str, body: bytes | None, content_type: str
    ) -> JobsResponse:
        self.calls.append((method, path, body, content_type))
        if not self.responses:
            raise AssertionError("aucune réponse planifiée")
        return self.responses.pop(0)


def manifest(name: str = "quadringent-reader-abc") -> dict[str, object]:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name},
        "spec": {"backoffLimit": 0},
    }


def test_dns_label_rules() -> None:
    assert is_dns_label("quadringent-demo")
    assert not is_dns_label("-leading")
    assert not is_dns_label("Uppercase")
    assert not is_dns_label("a" * 64)
    assert not is_dns_label(3)


def test_context_load_fails_closed_without_token(tmp_path: Path) -> None:
    (tmp_path / "ca.crt").write_text("x", encoding="utf-8")
    (tmp_path / "namespace").write_text(NAMESPACE, encoding="utf-8")

    with pytest.raises(JobsApiError) as captured:
        ServiceAccountContext.load(tmp_path)
    assert captured.value.code == CODE_NOT_CONFIGURED


def test_context_load_fails_closed_without_ca(tmp_path: Path) -> None:
    (tmp_path / "token").write_text("a.b.c", encoding="utf-8")
    (tmp_path / "namespace").write_text(NAMESPACE, encoding="utf-8")

    with pytest.raises(JobsApiError) as captured:
        ServiceAccountContext.load(tmp_path)
    assert captured.value.code == CODE_NOT_CONFIGURED


def test_context_rejects_invalid_namespace(tmp_path: Path) -> None:
    (tmp_path / "token").write_text("a.b.c", encoding="utf-8")
    (tmp_path / "ca.crt").write_text("x", encoding="utf-8")
    (tmp_path / "namespace").write_text("Bad_Namespace", encoding="utf-8")

    with pytest.raises(JobsApiError) as captured:
        ServiceAccountContext.load(tmp_path)
    assert captured.value.code == CODE_NOT_CONFIGURED


def test_token_never_reaches_the_error_message(tmp_path: Path) -> None:
    (tmp_path / "token").write_text("secret-token-value", encoding="utf-8")
    (tmp_path / "ca.crt").write_text("x", encoding="utf-8")

    context_value = ServiceAccountContext.load(tmp_path, namespace=NAMESPACE)
    recorder = Recorder([JobsResponse(status=403, body=None)])
    client = KubernetesJobsClient(recorder, NAMESPACE)
    with pytest.raises(JobsApiError) as captured:
        client.create_job(manifest())
    assert "secret-token-value" not in str(captured.value)
    assert context_value.token == "secret-token-value"


def test_create_job_posts_into_the_configured_namespace_only() -> None:
    recorder = Recorder([JobsResponse(status=201, body={"metadata": {"name": "quadringent-reader-abc"}})])
    client = KubernetesJobsClient(recorder, NAMESPACE)

    created = client.create_job(manifest())

    method, path, body, content_type = recorder.calls[0]
    assert content_type == "application/json"
    assert method == "POST"
    assert path == f"/apis/batch/v1/namespaces/{NAMESPACE}/jobs"
    assert json.loads(body or b"{}")["metadata"]["name"] == "quadringent-reader-abc"
    assert created["metadata"]["name"] == "quadringent-reader-abc"


def test_create_job_reports_conflict_without_overwriting() -> None:
    recorder = Recorder([JobsResponse(status=409, body=None)])
    client = KubernetesJobsClient(recorder, NAMESPACE)

    with pytest.raises(JobAlreadyExists) as captured:
        client.create_job(manifest())
    assert captured.value.code == CODE_ALREADY_EXISTS
    assert len(recorder.calls) == 1


def test_create_job_maps_refusals_to_safe_codes() -> None:
    for status, code in ((403, CODE_UNAUTHORIZED), (422, CODE_REJECTED), (500, CODE_UNAVAILABLE)):
        recorder = Recorder([JobsResponse(status=status, body=None)])
        client = KubernetesJobsClient(recorder, NAMESPACE)
        with pytest.raises(JobsApiError) as captured:
            client.create_job(manifest())
        assert captured.value.code == code, status


def test_distant_error_body_is_never_propagated() -> None:
    recorder = Recorder(
        [JobsResponse(status=422, body={"message": "host 192.0.2.10 user CDCUSER password=leak"})]
    )
    client = KubernetesJobsClient(recorder, NAMESPACE)

    with pytest.raises(JobsApiError) as captured:
        client.create_job(manifest())
    assert "leak" not in str(captured.value)
    assert "192.0.2.10" not in str(captured.value)


def test_read_job_returns_none_when_absent() -> None:
    recorder = Recorder([JobsResponse(status=404, body=None)])
    client = KubernetesJobsClient(recorder, NAMESPACE)

    assert client.read_job("quadringent-reader-abc") is None
    assert recorder.calls[0][1].endswith("/jobs/quadringent-reader-abc")


def test_read_job_rejects_unsafe_name() -> None:
    client = KubernetesJobsClient(Recorder([]), NAMESPACE)

    with pytest.raises(JobsApiError) as captured:
        client.read_job("../../secrets")
    assert captured.value.code == CODE_NOT_CONFIGURED


def test_non_json_response_is_refused() -> None:
    recorder = Recorder([JobsResponse(status=200, body={"metadata": {"name": "x"}})])
    client = KubernetesJobsClient(recorder, NAMESPACE)
    assert client.read_job("quadringent-reader-abc") == {"metadata": {"name": "x"}}


def test_invalid_json_body_raises_safe_code() -> None:
    from quadringent_control_plane import k8s_jobs

    with pytest.raises(JobsApiError) as captured:
        k8s_jobs._parse_json(b"{not json")
    assert captured.value.code == CODE_INVALID_RESPONSE


def test_https_transport_requires_usable_settings(tmp_path: Path) -> None:
    context_value = context(tmp_path)

    with pytest.raises(JobsApiError):
        https_transport(context_value, host="", port=443)
    with pytest.raises(JobsApiError):
        https_transport(context_value, host="192.0.2.10", port=0)
    with pytest.raises(JobsApiError):
        https_transport(context_value, host="192.0.2.10", port=443, timeout_seconds=0)


def test_client_rejects_invalid_namespace() -> None:
    with pytest.raises(JobsApiError) as captured:
        KubernetesJobsClient(Recorder([]), "Bad_Namespace")
    assert captured.value.code == CODE_NOT_CONFIGURED

def test_set_job_suspend_sends_an_explicit_merge_patch() -> None:
    recorder = Recorder([JobsResponse(status=200, body={"metadata": {"name": "quadringent-reader-abc"}})])
    client = KubernetesJobsClient(recorder, NAMESPACE)

    client.set_job_suspend("quadringent-reader-abc", True)

    method, path, body, content_type = recorder.calls[0]
    assert method == "PATCH"
    assert path == f"/apis/batch/v1/namespaces/{NAMESPACE}/jobs/quadringent-reader-abc"
    assert content_type == "application/merge-patch+json"
    assert json.loads(body or b"{}") == {"spec": {"suspend": True}}


def test_set_job_suspend_can_resume() -> None:
    recorder = Recorder([JobsResponse(status=200, body={"metadata": {"name": "quadringent-reader-abc"}})])
    client = KubernetesJobsClient(recorder, NAMESPACE)

    client.set_job_suspend("quadringent-reader-abc", False)

    assert json.loads(recorder.calls[0][2] or b"{}") == {"spec": {"suspend": False}}


def test_set_job_suspend_reports_an_absent_job_without_guessing() -> None:
    recorder = Recorder([JobsResponse(status=404, body=None)])
    client = KubernetesJobsClient(recorder, NAMESPACE)

    with pytest.raises(JobsApiError) as captured:
        client.set_job_suspend("quadringent-reader-abc", True)
    assert captured.value.code == CODE_REJECTED


def test_set_job_suspend_requires_an_explicit_boolean() -> None:
    client = KubernetesJobsClient(Recorder([]), NAMESPACE)

    with pytest.raises(JobsApiError) as captured:
        client.set_job_suspend("quadringent-reader-abc", "true")  # type: ignore[arg-type]
    assert captured.value.code == CODE_NOT_CONFIGURED


def test_set_job_suspend_never_reuses_an_unsafe_name() -> None:
    client = KubernetesJobsClient(Recorder([]), NAMESPACE)

    with pytest.raises(JobsApiError):
        client.set_job_suspend("../../jobs", True)


def test_delete_job_sends_a_delete_request() -> None:
    recorder = Recorder([JobsResponse(status=200, body={})])
    client = KubernetesJobsClient(recorder, NAMESPACE)

    client.delete_job("quadringent-reader-abc")

    method, path, body, content_type = recorder.calls[0]
    assert method == "DELETE"
    assert path == f"/apis/batch/v1/namespaces/{NAMESPACE}/jobs/quadringent-reader-abc"
    # Constaté sur GKE : sans propagation explicite, l'API supprime le Job
    # et laisse ses pods orphelins.
    import json as _json

    options = _json.loads(body) if isinstance(body, (bytes, str)) else body
    assert options["propagationPolicy"] == "Background"


def test_delete_job_is_idempotent_on_missing_job() -> None:
    recorder = Recorder([JobsResponse(status=404, body=None)])
    client = KubernetesJobsClient(recorder, NAMESPACE)

    client.delete_job("quadringent-reader-abc")  # ne lève pas


def test_delete_job_rejects_unsafe_name() -> None:
    client = KubernetesJobsClient(Recorder([]), NAMESPACE)
    with pytest.raises(JobsApiError) as captured:
        client.delete_job("-bad")
    assert captured.value.code == CODE_NOT_CONFIGURED


def test_delete_job_maps_server_errors_to_a_safe_code() -> None:
    recorder = Recorder([JobsResponse(status=500, body=None)])
    client = KubernetesJobsClient(recorder, NAMESPACE)
    with pytest.raises(JobsApiError) as captured:
        client.delete_job("quadringent-reader-abc")
    assert captured.value.code == CODE_UNAVAILABLE
