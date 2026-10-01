"""Boucle de réconciliation pure (désiré vs observé) — `executor/reconcile.py`.

Vérifie l'idempotence (aucune action si l'empreinte de `spec` observée
correspond déjà au désiré — sûr à rappeler au redémarrage du control
plane), la création/mise à jour/suppression du Deployment de lecteur, et
qu'un Job déjà observé n'est jamais réappliqué (immutabilité des Jobs).
"""

from __future__ import annotations

from quadringent_control_plane.v2.executor.manifests import with_spec_hash
from quadringent_control_plane.v2.executor.reconcile import (
    ACTION_CREATE,
    ACTION_DELETE,
    ACTION_UPDATE,
    reconcile_deployment,
    reconcile_jobs,
)

MANIFEST = {
    "metadata": {"name": "qdt-reader-abc", "namespace": "quadringent", "annotations": {}},
    "spec": {"replicas": 1, "tables": ["tbl-1"]},
}


def test_reconcile_creates_when_nothing_observed() -> None:
    desired = with_spec_hash(MANIFEST)
    action = reconcile_deployment(desired, observed=None)
    assert action.kind == ACTION_CREATE
    assert action.name == "qdt-reader-abc"
    assert action.manifest == desired


def test_reconcile_is_a_noop_when_spec_hash_already_matches() -> None:
    desired = with_spec_hash(MANIFEST)
    observed = {
        "metadata": {
            "name": "qdt-reader-abc",
            "namespace": "quadringent",
            "annotations": dict(desired["metadata"]["annotations"]),
        }
    }
    assert reconcile_deployment(desired, observed) is None


def test_reconcile_updates_when_spec_hash_differs() -> None:
    desired = with_spec_hash({**MANIFEST, "spec": {"replicas": 1, "tables": ["tbl-1", "tbl-2"]}})
    observed = {
        "metadata": {
            "name": "qdt-reader-abc",
            "namespace": "quadringent",
            "annotations": {"quadringent.io/spec-sha256": "stale"},
        }
    }
    action = reconcile_deployment(desired, observed)
    assert action.kind == ACTION_UPDATE
    assert action.manifest == desired


def test_reconcile_deletes_reader_when_no_table_is_live_anymore() -> None:
    observed = {"metadata": {"name": "qdt-reader-abc", "namespace": "quadringent"}}
    action = reconcile_deployment(None, observed)
    assert action.kind == ACTION_DELETE
    assert action.name == "qdt-reader-abc"


def test_reconcile_noop_when_nothing_desired_and_nothing_observed() -> None:
    assert reconcile_deployment(None, None) is None


def test_reconcile_is_idempotent_across_repeated_calls() -> None:
    desired = with_spec_hash(MANIFEST)
    first = reconcile_deployment(desired, observed=None)
    assert first.kind == ACTION_CREATE
    # Simule l'observé après application de `first` : plus aucune action.
    observed_after = {
        "metadata": {
            "name": first.name,
            "namespace": first.namespace,
            "annotations": dict(first.manifest["metadata"]["annotations"]),
        }
    }
    assert reconcile_deployment(desired, observed_after) is None


def test_reconcile_jobs_creates_only_unknown_jobs() -> None:
    desired = [
        {"metadata": {"name": "qdt-copy-a", "namespace": "quadringent"}},
        {"metadata": {"name": "qdt-copy-b", "namespace": "quadringent"}},
    ]
    observed_by_name = {"qdt-copy-a": {"metadata": {"name": "qdt-copy-a"}}}
    actions = reconcile_jobs(desired, observed_by_name)
    assert [a.name for a in actions] == ["qdt-copy-b"]
    assert actions[0].kind == ACTION_CREATE


def test_reconcile_jobs_never_reapplies_an_observed_job_even_if_content_would_differ() -> None:
    desired = [{"metadata": {"name": "qdt-copy-a", "namespace": "quadringent"}, "spec": {"x": 1}}]
    observed_by_name = {"qdt-copy-a": {"metadata": {"name": "qdt-copy-a"}, "spec": {"x": 999}}}
    assert reconcile_jobs(desired, observed_by_name) == []
