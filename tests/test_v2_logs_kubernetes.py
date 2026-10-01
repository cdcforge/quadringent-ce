"""``KubernetesLogSource`` (``v2/services/logs_kubernetes.py``, chantier
observabilité v2 suite) — sélection par le label posé par l'exécuteur
(``executor/manifests.py::LABEL_PIPELINE``), lecture bornée, redaction
laissée à ``LogsService`` (couche au-dessus, déjà testée dans
``test_v2_pipeline_logs.py``)."""

from __future__ import annotations

from quadringent_control_plane.k8s_pods import KubernetesPodsClient, PodsResponse
from quadringent_control_plane.v2.services.logs import LogsService
from quadringent_control_plane.v2.services.logs_kubernetes import LABEL_PIPELINE, KubernetesLogSource

NAMESPACE = "quadringent-demo"


def _pods_body(names: list[str]) -> bytes:
    import json

    return json.dumps({"items": [{"metadata": {"name": name}} for name in names]}).encode()


class _ScriptedTransport:
    def __init__(self, script: dict[str, PodsResponse]) -> None:
        self._script = script
        self.calls: list[str] = []

    def __call__(self, method: str, path: str) -> PodsResponse:
        self.calls.append(path)
        for key, response in self._script.items():
            if key in path:
                return response
        raise AssertionError(f"chemin non planifié : {path}")


def test_fetch_selects_pods_by_pipeline_label() -> None:
    transport = _ScriptedTransport(
        {
            "/pods?": PodsResponse(200, _pods_body(["job-ppl1-abc"])),
            "/log?": PodsResponse(200, b"2026-09-23T10:00:00.000000000Z ligne un\n"),
        }
    )
    client = KubernetesPodsClient(transport, NAMESPACE)
    source = KubernetesLogSource(client)
    entries = source.fetch("ppl1", since=None)
    assert len(entries) == 1
    assert entries[0].message == "ligne un"
    from urllib.parse import unquote

    list_path = next(path for path in transport.calls if "/pods?" in path)
    assert f"{LABEL_PIPELINE}=ppl1" in unquote(list_path)


def test_fetch_detects_error_level_from_text() -> None:
    transport = _ScriptedTransport(
        {
            "/pods?": PodsResponse(200, _pods_body(["job-ppl1-abc"])),
            "/log?": PodsResponse(200, b"2026-09-23T10:00:00Z ERROR echec de connexion\n"),
        }
    )
    client = KubernetesPodsClient(transport, NAMESPACE)
    source = KubernetesLogSource(client)
    entries = source.fetch("ppl1", since=None)
    assert entries[0].level == "error"


def test_fetch_detects_warning_level_from_text() -> None:
    transport = _ScriptedTransport(
        {
            "/pods?": PodsResponse(200, _pods_body(["job-ppl1-abc"])),
            "/log?": PodsResponse(200, b"2026-09-23T10:00:00Z WARNING retard croissant\n"),
        }
    )
    client = KubernetesPodsClient(transport, NAMESPACE)
    source = KubernetesLogSource(client)
    entries = source.fetch("ppl1", since=None)
    assert entries[0].level == "warning"


def test_fetch_extracts_incident_id_when_present() -> None:
    transport = _ScriptedTransport(
        {
            "/pods?": PodsResponse(200, _pods_body(["job-ppl1-abc"])),
            "/log?": PodsResponse(200, b"2026-09-23T10:00:00Z error incident_id=inc-42 replay bloque\n"),
        }
    )
    client = KubernetesPodsClient(transport, NAMESPACE)
    source = KubernetesLogSource(client)
    entries = source.fetch("ppl1", since=None)
    assert entries[0].incident_id == "inc-42"


def test_fetch_returns_nothing_when_no_pod_matches() -> None:
    transport = _ScriptedTransport({"/pods?": PodsResponse(200, _pods_body([]))})
    client = KubernetesPodsClient(transport, NAMESPACE)
    source = KubernetesLogSource(client)
    assert source.fetch("ppl1", since=None) == ()


def test_fetch_skips_lines_without_a_leading_timestamp() -> None:
    transport = _ScriptedTransport(
        {
            "/pods?": PodsResponse(200, _pods_body(["job-ppl1-abc"])),
            "/log?": PodsResponse(200, b"\n   \n2026-09-23T10:00:00Z ligne valide\n"),
        }
    )
    client = KubernetesPodsClient(transport, NAMESPACE)
    source = KubernetesLogSource(client)
    entries = source.fetch("ppl1", since=None)
    assert len(entries) == 1
    assert entries[0].message == "ligne valide"


def test_fetch_merges_and_sorts_entries_across_multiple_pods() -> None:
    transport = _ScriptedTransport(
        {
            "/pods?": PodsResponse(200, _pods_body(["job-ppl1-a", "job-ppl1-b"])),
        }
    )
    calls = {"n": 0}

    def transport_fn(method: str, path: str) -> PodsResponse:
        transport.calls.append(path)
        if "/pods?" in path:
            return PodsResponse(200, _pods_body(["job-ppl1-a", "job-ppl1-b"]))
        calls["n"] += 1
        if calls["n"] == 1:
            return PodsResponse(200, b"2026-09-23T10:00:02Z seconde\n")
        return PodsResponse(200, b"2026-09-23T10:00:01Z premiere\n")

    client = KubernetesPodsClient(transport_fn, NAMESPACE)
    source = KubernetesLogSource(client)
    entries = source.fetch("ppl1", since=None)
    assert [entry.message for entry in entries] == ["premiere", "seconde"]


def test_end_to_end_through_logs_service_redacts_secrets() -> None:
    transport = _ScriptedTransport(
        {
            "/pods?": PodsResponse(200, _pods_body(["job-ppl1-abc"])),
            "/log?": PodsResponse(200, b"2026-09-23T10:00:00Z error password=hunter2 echec\n"),
        }
    )
    client = KubernetesPodsClient(transport, NAMESPACE)
    source = KubernetesLogSource(client)
    entries = LogsService(source).fetch("ppl1")
    assert len(entries) == 1
    assert "hunter2" not in entries[0].message
    assert entries[0].level == "error"
