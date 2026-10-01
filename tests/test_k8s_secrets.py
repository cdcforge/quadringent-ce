"""Client Secrets Kubernetes minimal (chantier 4, tâche 3) — `k8s_secrets.py`.

Aucune connexion réelle : un transport factice enregistre les appels et
rejoue des réponses planifiées, à l'image de `tests/test_k8s_jobs.py`.
Vérifie l'encodage base64 (jamais de valeur en clair dans le corps JSON),
la création puis le repli sur un PATCH en cas de conflit 409.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

import pytest

from quadringent_control_plane.k8s_secrets import (
    CODE_NOT_CONFIGURED,
    CODE_REJECTED,
    CODE_UNAUTHORIZED,
    KubernetesSecretsClient,
    SecretsApiError,
)

NAMESPACE = "quadringent-demo"


@dataclass
class _Response:
    status: int
    body: object = None


class _Recorder:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, bytes | None, str]] = []

    def __call__(self, method: str, path: str, body: bytes | None, content_type: str) -> _Response:
        self.calls.append((method, path, body, content_type))
        return self.responses.pop(0)


def test_upsert_creates_a_secret_when_absent() -> None:
    recorder = _Recorder([_Response(201, {})])
    client = KubernetesSecretsClient(recorder, NAMESPACE)

    client.upsert_secret("qdt-source-src1", {"ISERIES_PASSWORD": "un-mot-de-passe-tres-secret"})

    method, path, body, content_type = recorder.calls[0]
    assert method == "POST"
    assert path == f"/api/v1/namespaces/{NAMESPACE}/secrets"
    payload = json.loads(body)
    assert payload["metadata"]["name"] == "qdt-source-src1"
    assert payload["type"] == "Opaque"
    # La valeur n'apparaît jamais en clair dans le corps envoyé.
    assert "un-mot-de-passe-tres-secret" not in body.decode("utf-8")
    decoded = base64.b64decode(payload["data"]["ISERIES_PASSWORD"]).decode("utf-8")
    assert decoded == "un-mot-de-passe-tres-secret"


def test_upsert_falls_back_to_patch_on_conflict() -> None:
    recorder = _Recorder([_Response(409, {}), _Response(200, {})])
    client = KubernetesSecretsClient(recorder, NAMESPACE)

    client.upsert_secret("qdt-source-src1", {"ISERIES_PASSWORD": "nouveau-mot-de-passe"})

    assert len(recorder.calls) == 2
    method, path, body, content_type = recorder.calls[1]
    assert method == "PATCH"
    assert path == f"/api/v1/namespaces/{NAMESPACE}/secrets/qdt-source-src1"
    assert content_type == "application/merge-patch+json"


def test_upsert_rejects_empty_data() -> None:
    client = KubernetesSecretsClient(_Recorder([]), NAMESPACE)
    with pytest.raises(SecretsApiError) as excinfo:
        client.upsert_secret("qdt-source-src1", {})
    assert excinfo.value.code == CODE_NOT_CONFIGURED


def test_upsert_rejects_invalid_secret_name() -> None:
    client = KubernetesSecretsClient(_Recorder([]), NAMESPACE)
    with pytest.raises(SecretsApiError):
        client.upsert_secret("Not_A_Valid_Name!", {"a": "b"})


def test_unauthorized_response_maps_to_a_safe_code() -> None:
    recorder = _Recorder([_Response(403, {})])
    client = KubernetesSecretsClient(recorder, NAMESPACE)
    with pytest.raises(SecretsApiError) as excinfo:
        client.upsert_secret("qdt-source-src1", {"a": "b"})
    assert excinfo.value.code == CODE_UNAUTHORIZED


def test_rejected_response_maps_to_a_safe_code() -> None:
    recorder = _Recorder([_Response(400, {})])
    client = KubernetesSecretsClient(recorder, NAMESPACE)
    with pytest.raises(SecretsApiError) as excinfo:
        client.upsert_secret("qdt-source-src1", {"a": "b"})
    assert excinfo.value.code == CODE_REJECTED


def test_invalid_namespace_is_rejected_at_construction() -> None:
    with pytest.raises(SecretsApiError):
        KubernetesSecretsClient(_Recorder([]), "Not_Valid")


def test_delete_secret_sends_a_delete_request() -> None:
    recorder = _Recorder([_Response(200, {})])
    client = KubernetesSecretsClient(recorder, NAMESPACE)

    client.delete_secret("qdt-probe-secret-abc")

    method, path, body, content_type = recorder.calls[0]
    assert method == "DELETE"
    assert path == f"/api/v1/namespaces/{NAMESPACE}/secrets/qdt-probe-secret-abc"


def test_delete_secret_is_idempotent_on_missing_secret() -> None:
    recorder = _Recorder([_Response(404, None)])
    client = KubernetesSecretsClient(recorder, NAMESPACE)

    client.delete_secret("qdt-probe-secret-abc")  # ne lève pas


def test_delete_secret_rejects_unsafe_name() -> None:
    client = KubernetesSecretsClient(_Recorder([]), NAMESPACE)
    with pytest.raises(SecretsApiError) as captured:
        client.delete_secret("-bad")
    assert captured.value.code == CODE_NOT_CONFIGURED
