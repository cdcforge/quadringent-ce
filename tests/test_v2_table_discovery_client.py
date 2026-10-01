"""Adaptateur worker réel -> ``TableDiscoveryClientProtocol`` (chantier 4, item 3).

``PersistentJavaWorkerTableDiscoveryClient`` doit produire exactement ce que
produirait un appel direct à ``parse_discover_output`` sur la sortie du
worker — aucune logique de classification ici, uniquement le branchement.
"""

from __future__ import annotations

from quadringent.table_discovery import DiscoveredTable, parse_discover_output
from quadringent_control_plane.v2.services.table_discovery_client import (
    PersistentJavaWorkerTableDiscoveryClient,
)


def _row(**overrides) -> str:
    values = dict(
        library="SALES",
        system_name="ORDHDR",
        sql_name="ORDER_HEADER",
        text="En-tête de commande",
        row_count="1200",
        size_bytes="65536",
        has_key="yes",
        key_columns="ORDER_ID,LINE_NO",
        journaled="yes",
        journal_library="SALES",
        journal_name="ORDJRN",
        images="*BOTH",
        omitted="no",
        columns="",
    )
    values.update(overrides)
    return "\t".join(["table", *values.values()])


class _FakeWorker:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[dict[str, object]] = []

    def discover(self, *, libraries=None, limit=500, search=None):
        self.calls.append({"libraries": libraries, "limit": limit, "search": search})
        return self.output


def test_adapter_parses_worker_output_identically_to_direct_parsing() -> None:
    raw = _row()
    worker = _FakeWorker(raw)
    client = PersistentJavaWorkerTableDiscoveryClient(worker)

    result = client.discover(libraries=("SALES",), limit=500, search=None)

    expected = parse_discover_output(raw)
    assert result == expected
    assert isinstance(result[0], DiscoveredTable)
    assert result[0].library == "SALES"
    assert result[0].system_name == "ORDHDR"


def test_adapter_forwards_filters_to_the_worker_unchanged() -> None:
    raw = ""
    worker = _FakeWorker(raw)
    client = PersistentJavaWorkerTableDiscoveryClient(worker)

    client.discover(libraries=("SALES", "PAYSLIB"), limit=42, search="ORD")

    assert worker.calls == [{"libraries": ("SALES", "PAYSLIB"), "limit": 42, "search": "ORD"}]


def test_adapter_propagates_empty_catalogue() -> None:
    worker = _FakeWorker("")
    client = PersistentJavaWorkerTableDiscoveryClient(worker)
    assert client.discover(libraries=None, limit=500, search=None) == ()
