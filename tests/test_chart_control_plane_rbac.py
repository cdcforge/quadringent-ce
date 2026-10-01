"""RBAC dédié à la sonde de source et à la découverte de tables v2
(Role/RoleBinding ``*-control-plane-v2-diagnostics``) rendu par la chart —
chantier « prod-wiring ».

Un Role séparé du Role de lancement de Jobs de la flotte v1
(``*-control-plane-jobs``, ``tests/test_control_plane_jobs_chart.py``) :
chaque consommateur au strict nécessaire, jamais un droit de plus
(v1 ne supprime jamais de Job lui-même ; v2 nettoie systématiquement ses
Jobs/Secrets éphémères — voir ``executor/diagnostic_jobs.py``).
"""

from __future__ import annotations

from test_chart_kubernetes_names import _cases, _render


def _rules_by_resource(role: dict) -> dict[str, dict]:
    by_resource: dict[str, dict] = {}
    for rule in role["rules"]:
        for api_group in rule["apiGroups"]:
            for resource in rule["resources"]:
                by_resource[f"{api_group or 'core'}/{resource}"] = rule
    return by_resource


def _v2_diagnostics_role(case: int) -> dict:
    docs = _render(_cases()[case])
    roles = [
        doc
        for doc in docs
        if doc.get("kind") == "Role" and doc["metadata"]["name"].endswith("-control-plane-v2-diagnostics")
    ]
    assert len(roles) == 1, "un seul Role control-plane-v2-diagnostics attendu"
    return roles[0]


def test_role_grants_job_create_get_delete_only() -> None:
    role = _v2_diagnostics_role(0)
    rules = _rules_by_resource(role)
    jobs_rule = rules["batch/jobs"]
    assert set(jobs_rule["verbs"]) == {"create", "get", "delete"}


def test_role_grants_secrets_create_get_delete_only() -> None:
    role = _v2_diagnostics_role(0)
    rules = _rules_by_resource(role)
    secrets_rule = rules["core/secrets"]
    assert set(secrets_rule["verbs"]) == {"create", "get", "delete"}
    assert "list" not in secrets_rule["verbs"] and "watch" not in secrets_rule["verbs"]


def test_role_grants_read_only_pods_and_pod_logs() -> None:
    role = _v2_diagnostics_role(0)
    rules = _rules_by_resource(role)
    pods_rule = rules["core/pods"]
    assert set(pods_rule["verbs"]) == {"get", "list"}
    logs_rule = rules["core/pods/log"]
    assert set(logs_rule["verbs"]) == {"get"}


def test_role_binding_targets_the_control_plane_service_account() -> None:
    docs = _render(_cases()[0])
    binding = next(
        doc
        for doc in docs
        if doc.get("kind") == "RoleBinding" and doc["metadata"]["name"].endswith("-control-plane-v2-diagnostics")
    )
    assert binding["roleRef"]["name"].endswith("-control-plane-v2-diagnostics")
    assert binding["subjects"][0]["kind"] == "ServiceAccount"
