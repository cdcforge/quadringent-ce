from __future__ import annotations

import json
from pathlib import Path

import pytest

import site_fixture
from quadringent_control_plane.fleet import FleetError
from quadringent_control_plane.fleet_action_executor import (
    EXECUTOR_ENVIRONMENT,
    EXECUTOR_FLEET_ID,
    EXECUTOR_PIPELINE_ID,
    FleetActionExecutor,
)
from quadringent_control_plane.fleet_composition import (
    CODE_INCOMPLETE_CONFIGURATION,
    CODE_UNSUPPORTED_PLAN,
    HISTORY_STATE_FILE,
    LEGACY_SITE_ID,
    PREPARE_STATE_FILE,
    FleetLaunchConfig,
    build_fleet_action_executor,
)
from quadringent_control_plane.fleet_job_launcher import TEMPLATE_FORMAT_VERSION
from quadringent_control_plane.fleet_prepare_runtime import PHASE_PREPARED
from quadringent_control_plane.fleet_runtime_store import AtomicJsonStateStore
from quadringent_control_plane.k8s_jobs import KubernetesJobsClient

from test_fleet_job_launcher import FakeCluster, pod_spec
from test_fleet_prepare_runtime import make_plan
from test_fleet_providers import StaticCatalog, snapshot


NAMESPACE = "quadringent-demo"
SITE = site_fixture.build_test_site()


class Invocation:
    def __init__(self, action: str) -> None:
        self.pipeline_id = EXECUTOR_PIPELINE_ID
        self.fleet_id = EXECUTOR_FLEET_ID
        self.environment = EXECUTOR_ENVIRONMENT
        self.action = action


def template_document() -> dict[str, object]:
    return {
        "format_version": TEMPLATE_FORMAT_VERSION,
        "container": "capture",
        "pod": pod_spec(),
        "backoff_limit": 0,
        "active_deadline_seconds": 3900,
        "ttl_seconds_after_finished": 86400,
        "reserve_run": True,
    }


def config(tmp_path: Path, **changes: object) -> FleetLaunchConfig:
    template_path = tmp_path / "job-template.json"
    template_path.write_text(json.dumps(template_document()), encoding="utf-8")
    values: dict[str, object] = {
        "state_directory": tmp_path,
        "raw_prefix_root": "as400/sales/fleet",
        "job_template_path": template_path,
    }
    values.update(changes)
    return FleetLaunchConfig(**values)  # type: ignore[arg-type]


def catalog() -> StaticCatalog:
    return StaticCatalog([snapshot("DEMOJRN0099", 50), snapshot("DEMOJRN0100", 250)])


def executor(tmp_path: Path, cluster: FakeCluster | None = None, **changes: object) -> FleetActionExecutor:
    return build_fleet_action_executor(
        plan=make_plan(),
        config=config(tmp_path, **changes),
        client=KubernetesJobsClient(cluster or FakeCluster(), NAMESPACE),
        catalog=catalog(),
    )


def test_configuration_requires_a_readable_template(tmp_path: Path) -> None:
    with pytest.raises(FleetError) as captured:
        FleetLaunchConfig(
            state_directory=tmp_path,
            raw_prefix_root="as400/sales/fleet",
            job_template_path=tmp_path / "absent.json",
        )
    assert captured.value.code == CODE_INCOMPLETE_CONFIGURATION


def test_composition_refuses_a_non_dev_plan(tmp_path: Path) -> None:
    with pytest.raises(FleetError) as captured:
        build_fleet_action_executor(
            plan="not-a-plan",  # type: ignore[arg-type]
            config=config(tmp_path),
            client=KubernetesJobsClient(FakeCluster(), NAMESPACE),
            catalog=catalog(),
        )
    assert captured.value.code == CODE_UNSUPPORTED_PLAN


def test_prepare_launches_a_reader_job_from_the_fresh_catalog(tmp_path: Path) -> None:
    cluster = FakeCluster()
    runtime = executor(tmp_path, cluster)

    assert runtime.supports(Invocation("prepare")) is True
    stages = runtime.execute(Invocation("prepare"))
    assert runtime.supports(Invocation("prepare")) is False

    (job,) = cluster.created
    container = job["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["AS400_BOOTSTRAP_RECEIVER"] == "DEMOJRN0100"
    assert env["AS400_BOOTSTRAP_SEQUENCE"] == "250"
    # Le site de test ("acme") n'est pas le site legacy : son fichier est
    # préfixé par son site_id, à la différence de example-corp (voir les tests
    # d'isolation dédiés plus bas).
    persisted = AtomicJsonStateStore(tmp_path / f"{SITE.site_id}-{PREPARE_STATE_FILE}").load()
    assert persisted["phase"] == PHASE_PREPARED
    assert persisted["checkpoint"] == {"receiver": "DEMOJRN0100", "sequence": 250}
    assert stages["intent"]["state"] == "recorded"
    assert stages["execution"]["state"] == "completed"
    assert stages["observed_effect"]["state"] == "succeeded"


def test_start_launches_a_history_job_after_prepare(tmp_path: Path) -> None:
    cluster = FakeCluster()
    runtime = executor(tmp_path, cluster)
    runtime.execute(Invocation("prepare"))

    assert runtime.supports(Invocation("start")) is True
    runtime.execute(Invocation("start"))

    kinds = [job["metadata"]["labels"]["quadringent.io/request-kind"] for job in cluster.created]
    assert kinds == ["reader", "history"]
    history = cluster.created[1]
    assert history["metadata"]["annotations"]["quadringent.io/checkpoint"] == "DEMOJRN0100:250"


def test_a_second_prepare_is_not_launched_twice(tmp_path: Path) -> None:
    cluster = FakeCluster()
    runtime = executor(tmp_path, cluster)
    runtime.execute(Invocation("prepare"))

    assert runtime.supports(Invocation("prepare")) is False
    runtime.execute(Invocation("prepare"))

    assert len(cluster.created) == 1


def test_missing_ibmi_identity_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(FleetError) as captured:
        build_fleet_action_executor(
            plan=make_plan(),
            config=config(tmp_path),
            client=KubernetesJobsClient(FakeCluster(), NAMESPACE),
            environ={"AS400_JAVA_CLASSPATH": "/app/probe.jar"},
        )
    assert captured.value.code == CODE_INCOMPLETE_CONFIGURATION


def test_java_catalog_is_built_from_the_dev_environment(tmp_path: Path) -> None:
    built = build_fleet_action_executor(
        plan=make_plan(),
        config=config(tmp_path),
        client=KubernetesJobsClient(FakeCluster(), NAMESPACE),
        environ={
            "AS400_JAVA": "java",
            "AS400_JAVA_CLASSPATH": "/app/probe.jar:/app/lib/*",
            "ISERIES_HOST": "192.0.2.10",
            "ISERIES_USER": "CDCUSER",
        },
    )

    provider = built._checkpoint_provider  # noqa: SLF001 - vérification de câblage
    catalog_value = provider._catalog  # noqa: SLF001
    assert catalog_value.host == "192.0.2.10"
    assert catalog_value.user == "CDCUSER"
    assert catalog_value.journal_library == "JRNLIB1"
    assert catalog_value.journal_name == "DEMOJRN"


def test_legacy_site_keeps_its_historical_bare_state_filenames(tmp_path: Path) -> None:
    """example-corp est en production : ses fichiers ne sont jamais renommés."""
    runtime = build_fleet_action_executor(
        plan=make_plan(),
        config=config(tmp_path),
        client=KubernetesJobsClient(FakeCluster(), NAMESPACE),
        catalog=catalog(),
        site_id=LEGACY_SITE_ID,
    )
    runtime.execute(Invocation("prepare"))
    assert (tmp_path / PREPARE_STATE_FILE).is_file()
    assert (tmp_path / HISTORY_STATE_FILE).is_file() is False  # rien tant qu'aucun start
    assert not (tmp_path / f"{LEGACY_SITE_ID}-{PREPARE_STATE_FILE}").exists()


def test_a_non_legacy_site_gets_state_files_prefixed_by_its_site_id(tmp_path: Path) -> None:
    runtime = build_fleet_action_executor(
        plan=make_plan(),
        config=config(tmp_path),
        client=KubernetesJobsClient(FakeCluster(), NAMESPACE),
        catalog=catalog(),
        site_id="second-site",
    )
    runtime.execute(Invocation("prepare"))
    assert (tmp_path / f"second-site-{PREPARE_STATE_FILE}").is_file()
    assert not (tmp_path / PREPARE_STATE_FILE).exists()


def test_two_sites_composed_on_the_same_state_directory_do_not_collide(
    tmp_path: Path,
) -> None:
    """Deux liaisons sur le même --fleet-state-dir n'écrasent jamais l'état l'une de l'autre."""
    first = build_fleet_action_executor(
        plan=make_plan(),
        config=config(tmp_path),
        client=KubernetesJobsClient(FakeCluster(), NAMESPACE),
        catalog=catalog(),
        site_id="site-alpha",
    )
    second = build_fleet_action_executor(
        plan=make_plan(),
        config=config(tmp_path),
        client=KubernetesJobsClient(FakeCluster(), NAMESPACE),
        catalog=catalog(),
        site_id="site-beta",
    )
    first.execute(Invocation("prepare"))
    assert AtomicJsonStateStore(tmp_path / "site-alpha-fleet-prepare.json").load() is not None
    assert AtomicJsonStateStore(tmp_path / "site-beta-fleet-prepare.json").load() is None
    assert second.supports(Invocation("prepare")) is True


def test_state_filenames_default_to_the_current_site_when_unspecified(tmp_path: Path) -> None:
    """Sans site_id explicite, le site courant (celui de l'environnement) fait foi."""
    runtime = build_fleet_action_executor(
        plan=make_plan(),
        config=config(tmp_path),
        client=KubernetesJobsClient(FakeCluster(), NAMESPACE),
        catalog=catalog(),
    )
    runtime.execute(Invocation("prepare"))
    assert AtomicJsonStateStore(tmp_path / f"{SITE.site_id}-{PREPARE_STATE_FILE}").load() is not None
    assert not (tmp_path / PREPARE_STATE_FILE).exists()
