"""Client pods/journaux Kubernetes (``k8s_pods.py``) — lecture seule bornée.

Symétrique de ``tests/test_k8s_jobs.py`` : un transport factice enregistre
les appels et rejoue des réponses planifiées, sans jamais toucher un vrai
cluster.
"""

from __future__ import annotations

import json

import pytest

from quadringent_control_plane.k8s_pods import (
    CODE_INVALID_RESPONSE,
    CODE_NOT_CONFIGURED,
    CODE_NOT_FOUND,
    CODE_UNAUTHORIZED,
    KubernetesPodsClient,
    PodsApiError,
    PodsResponse,
)

NAMESPACE = "quadringent-demo"


class Recorder:
    def __init__(self, responses: list[PodsResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, path: str) -> PodsResponse:
        self.calls.append((method, path))
        if not self.responses:
            raise AssertionError("aucune réponse planifiée")
        return self.responses.pop(0)


def _pods_body(names: list[str]) -> bytes:
    return json.dumps({"items": [{"metadata": {"name": name}} for name in names]}).encode("utf-8")


def test_client_rejects_missing_transport() -> None:
    with pytest.raises(PodsApiError) as excinfo:
        KubernetesPodsClient(None, NAMESPACE)
    assert excinfo.value.code == CODE_NOT_CONFIGURED


def test_client_rejects_invalid_namespace() -> None:
    with pytest.raises(PodsApiError):
        KubernetesPodsClient(lambda method, path: PodsResponse(200, b""), "Not A Namespace!")


def test_list_pod_names_returns_names_from_selector() -> None:
    recorder = Recorder([PodsResponse(status=200, body=_pods_body(["job-a-xyz", "job-b-abc"]))])
    client = KubernetesPodsClient(recorder, NAMESPACE)
    names = client.list_pod_names(label_selector="quadringent.io/pipeline-id=ppl1")
    assert names == ("job-a-xyz", "job-b-abc")
    method, path = recorder.calls[0]
    assert method == "GET"
    assert "labelSelector=quadringent.io" in path


def test_list_pod_names_truncates_to_limit() -> None:
    recorder = Recorder([PodsResponse(status=200, body=_pods_body(["a", "b", "c"]))])
    client = KubernetesPodsClient(recorder, NAMESPACE)
    names = client.list_pod_names(label_selector="x=y", limit=2)
    assert names == ("a", "b")


def test_list_pod_names_rejects_empty_selector() -> None:
    client = KubernetesPodsClient(lambda m, p: PodsResponse(200, b""), NAMESPACE)
    with pytest.raises(PodsApiError):
        client.list_pod_names(label_selector="")


def test_list_pod_names_unauthorized_raises_reduced_error() -> None:
    recorder = Recorder([PodsResponse(status=403, body=b"")])
    client = KubernetesPodsClient(recorder, NAMESPACE)
    with pytest.raises(PodsApiError) as excinfo:
        client.list_pod_names(label_selector="x=y")
    assert excinfo.value.code == CODE_UNAUTHORIZED
    assert "403" not in str(excinfo.value)  # jamais le détail distant


def test_list_pod_names_invalid_json_fails_closed() -> None:
    recorder = Recorder([PodsResponse(status=200, body=b"not json")])
    client = KubernetesPodsClient(recorder, NAMESPACE)
    with pytest.raises(PodsApiError) as excinfo:
        client.list_pod_names(label_selector="x=y")
    assert excinfo.value.code == CODE_INVALID_RESPONSE


def test_read_pod_log_returns_decoded_text() -> None:
    recorder = Recorder([PodsResponse(status=200, body="2026-09-23T10:00:00Z ligne un\n".encode())])
    client = KubernetesPodsClient(recorder, NAMESPACE)
    text = client.read_pod_log("job-a-xyz")
    assert text == "2026-09-23T10:00:00Z ligne un\n"
    method, path = recorder.calls[0]
    assert "timestamps=true" in path
    assert "tailLines=200" in path


def test_read_pod_log_requests_since_time_and_container_when_given() -> None:
    recorder = Recorder([PodsResponse(status=200, body=b"")])
    client = KubernetesPodsClient(recorder, NAMESPACE)
    client.read_pod_log("job-a-xyz", container="reader", since_time="2026-09-23T09:00:00Z", tail_lines=50)
    _, path = recorder.calls[0]
    assert "sinceTime=2026-09-23T09%3A00%3A00Z" in path
    assert "container=reader" in path
    assert "tailLines=50" in path


def test_read_pod_log_missing_pod_returns_none() -> None:
    recorder = Recorder([PodsResponse(status=404, body=b"")])
    client = KubernetesPodsClient(recorder, NAMESPACE)
    assert client.read_pod_log("does-not-exist") is None


def test_read_pod_log_rejects_out_of_range_tail_lines() -> None:
    client = KubernetesPodsClient(lambda m, p: PodsResponse(200, b""), NAMESPACE)
    with pytest.raises(PodsApiError):
        client.read_pod_log("pod1", tail_lines=0)


def test_read_pod_log_server_error_raises_reduced_error() -> None:
    recorder = Recorder([PodsResponse(status=500, body=b"stack trace leak")])
    client = KubernetesPodsClient(recorder, NAMESPACE)
    with pytest.raises(PodsApiError) as excinfo:
        client.read_pod_log("pod1")
    assert "stack trace leak" not in str(excinfo.value)


def test_list_pod_names_not_found_raises_pod_not_found_code() -> None:
    recorder = Recorder([PodsResponse(status=404, body=b"")])
    client = KubernetesPodsClient(recorder, NAMESPACE)
    with pytest.raises(PodsApiError) as excinfo:
        client.list_pod_names(label_selector="x=y")
    assert excinfo.value.code == CODE_NOT_FOUND
