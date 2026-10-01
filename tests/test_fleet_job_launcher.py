from __future__ import annotations

from dataclasses import replace
import hashlib
import json

import pytest

from quadringent_control_plane.fleet import JournalCheckpoint
from quadringent_control_plane.fleet_history_runtime import HistoryLaunchRequest
from quadringent_control_plane.fleet_job_launcher import (
    ANNOTATION_CHECKPOINT,
    ANNOTATION_MANIFEST,
    ANNOTATION_PREPARE_INTENT,
    LABEL_INTENT,
    LABEL_KIND,
    HISTORY_JOB_PREFIX,
    READER_JOB_PREFIX,
    HistoryJobLauncher,
    JobTemplate,
    LauncherError,
    ReaderJobLauncher,
    TEMPLATE_FORMAT_VERSION,
)
from quadringent_control_plane.fleet_plan import HistoricalLane
from quadringent_control_plane.fleet_prepare_runtime import MANIFEST, ReaderLaunchRequest
from quadringent_control_plane.k8s_jobs import (
    CODE_UNAVAILABLE,
    JobsApiError,
    JobsResponse,
    KubernetesJobsClient,
)


NAMESPACE = "quadringent-demo"
INTENT = "4f2a1c9b8d1e4a7f9c3b5d6e8a0f1c2d"
CHECKPOINT = JournalCheckpoint(receiver="DEMOJRN0100", sequence=250)


def pod_spec() -> dict[str, object]:
    return {
        "restartPolicy": "Never",
        "serviceAccountName": "quadringent",
        "containers": [
            {
                "name": "capture",
                "image": "ghcr.io/quadringent/quadringent@sha256:" + "0" * 64,
                "envFrom": [{"configMapRef": {"name": "quadringent-tuning"}}],
                "env": [
                    {
                        "name": "ISERIES_PASSWORD",
                        "valueFrom": {
                            "secretKeyRef": {"name": "quadringent-ibmi", "key": "password"}
                        },
                    },
                    {"name": "AS400_RAW_PREFIX", "value": "as400/sales/sale"},
                ],
            }
        ],
    }


def template(**changes: object) -> JobTemplate:
    payload: dict[str, object] = {
        "format_version": TEMPLATE_FORMAT_VERSION,
        "container": "capture",
        "pod": pod_spec(),
        "backoff_limit": 0,
        "active_deadline_seconds": 3900,
        "ttl_seconds_after_finished": 86400,
        "reserve_run": True,
    }
    payload.update(changes)
    return JobTemplate.parse(payload)


class FakeCluster:
    """API Jobs simulée : mémorise les Jobs créés par nom."""

    def __init__(self, *, already: dict[str, object] | None = None, create_error: str | None = None) -> None:
        self.jobs = dict(already or {})
        self.create_error = create_error
        self.created: list[dict[str, object]] = []

    def __call__(
        self, method: str, path: str, body: bytes | None, content_type: str = "application/json"
    ) -> JobsResponse:
        if method == "POST":
            if self.create_error is not None:
                raise JobsApiError(self.create_error, "API injoignable")
            payload = json.loads(body or b"{}")
            name = payload["metadata"]["name"]
            if name in self.jobs:
                return JobsResponse(status=409, body=None)
            self.jobs[name] = payload
            self.created.append(payload)
            return JobsResponse(status=201, body=payload)
        name = path.rsplit("/", 1)[-1]
        job = self.jobs.get(name)
        if job is None:
            return JobsResponse(status=404, body=None)
        return JobsResponse(status=200, body=job)


def reader_request(**changes: object) -> ReaderLaunchRequest:
    request = ReaderLaunchRequest(
        intent_id=INTENT,
        reader_kind="multi_object",
        reader_count=1,
        journal_library="JRNLIB1",
        journal_name="DEMOJRN",
        manifest=MANIFEST,
        checkpoint=CHECKPOINT,
    )
    return replace(request, **changes) if changes else request


def launcher(cluster: FakeCluster, **kwargs: object) -> ReaderJobLauncher:
    client = KubernetesJobsClient(cluster, NAMESPACE)
    return ReaderJobLauncher(
        client,
        template(**kwargs),
        raw_prefix_root="as400/sales",
    )


def test_template_requires_the_closed_shape() -> None:
    with pytest.raises(LauncherError):
        JobTemplate.parse({"format_version": TEMPLATE_FORMAT_VERSION, "container": "capture"})
    with pytest.raises(LauncherError):
        template(unknown_field=1)
    with pytest.raises(LauncherError):
        template(format_version="v0")
    with pytest.raises(LauncherError):
        template(backoff_limit=3)
    with pytest.raises(LauncherError):
        template(container="absent")


def test_template_refuses_plaintext_secret() -> None:
    pod = pod_spec()
    pod["containers"][0]["env"].append({"name": "ISERIES_PASSWORD", "value": "hunter2"})

    with pytest.raises(LauncherError) as captured:
        template(pod=pod)
    assert "secret" in captured.value.safe_message.lower()


def test_template_refuses_privileged_and_host_network() -> None:
    pod = pod_spec()
    pod["containers"][0]["securityContext"] = {"privileged": True}
    with pytest.raises(LauncherError):
        template(pod=pod)

    pod = pod_spec()
    pod["hostNetwork"] = True
    with pytest.raises(LauncherError):
        template(pod=pod)

    pod = pod_spec()
    pod["restartPolicy"] = "Always"
    with pytest.raises(LauncherError):
        template(pod=pod)


def test_reader_job_carries_checkpoint_and_never_renews_kubernetes_retries() -> None:
    cluster = FakeCluster()
    receipt = launcher(cluster).launch(reader_request())

    (job,) = cluster.created
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] == 3900
    container = job["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["AS400_BOOTSTRAP_RECEIVER"] == "DEMOJRN0100"
    assert env["AS400_BOOTSTRAP_SEQUENCE"] == "250"
    assert env["AS400_RAW_PREFIX"] == "as400/sales/fleet/runs/" + receipt.reader_id.rsplit("-", 1)[-1]
    assert env["AS400_FLEET_TABLES"] == ",".join(MANIFEST)
    assert env["AS400_FLEET_TABLE_ROOT"] == "as400/sales"
    assert env["AS400_CONSOLE_SNAPSHOT_S3_KEY"] == "as400/sales/fleet/console-snapshot.json"
    assert container["args"][-2:] == ["--reserve-run-id", receipt.reader_id.rsplit("-", 1)[-1]]
    assert receipt.reader_id.startswith(READER_JOB_PREFIX + "-")
    assert receipt.status == "RUNNING"
    assert receipt.checkpoint == CHECKPOINT
    assert receipt.manifest == MANIFEST


def test_reader_job_keeps_secret_reference_untouched() -> None:
    cluster = FakeCluster()
    launcher(cluster).launch(reader_request())

    (job,) = cluster.created
    secret_env = [
        item
        for item in job["spec"]["template"]["spec"]["containers"][0]["env"]
        if item["name"] == "ISERIES_PASSWORD"
    ]
    assert secret_env == [
        {"name": "ISERIES_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "quadringent-ibmi", "key": "password"}}}
    ]


def test_reader_job_journal_position_is_annotated_for_audit() -> None:
    cluster = FakeCluster()
    launcher(cluster).launch(reader_request())

    (job,) = cluster.created
    annotations = job["metadata"]["annotations"]
    assert annotations[ANNOTATION_CHECKPOINT] == "DEMOJRN0100:250"
    assert annotations[ANNOTATION_MANIFEST] == hashlib.sha256(",".join(MANIFEST).encode()).hexdigest()
    assert job["metadata"]["labels"][LABEL_INTENT] == INTENT
    assert job["metadata"]["labels"][LABEL_KIND] == "reader"


def test_regular_reader_without_reserve_run_gets_no_extra_argument() -> None:
    cluster = FakeCluster()
    receipt = launcher(cluster, reserve_run=False).launch(reader_request())

    (job,) = cluster.created
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container.get("args") in (None, [])
    assert receipt.reader_id.startswith(READER_JOB_PREFIX + "-")


def test_same_intent_is_idempotent() -> None:
    cluster = FakeCluster()
    runtime = launcher(cluster)

    first = runtime.launch(reader_request())
    second = runtime.launch(reader_request())

    assert first == second
    assert len(cluster.created) == 1


@pytest.mark.parametrize("condition", ["Complete", "Failed"])
def test_un_job_terminal_ne_se_declare_jamais_en_execution(condition):
    cluster = FakeCluster()
    runtime = launcher(cluster)
    runtime.launch(reader_request())
    cluster.created[0]["status"] = {"conditions": [{"type": condition, "status": "True"}]}
    with pytest.raises(LauncherError) as error:
        runtime.launch(reader_request())
    assert error.value.code == "job_terminal"
    assert len(cluster.created) == 1


def test_lecteur_sans_ttl_recoit_un_nettoyage_borne():
    cluster = FakeCluster()
    launcher(cluster, ttl_seconds_after_finished=None).launch(reader_request())
    assert cluster.created[0]["spec"]["ttlSecondsAfterFinished"] == 60


def test_apres_nettoyage_ttl_le_meme_nom_repart_du_checkpoint_durable():
    cluster = FakeCluster()
    runtime = launcher(cluster)
    initial = runtime.launch(reader_request())
    del cluster.jobs[initial.reader_id]  # effet du contrôleur TTL, hors RBAC produit
    checkpoint = JournalCheckpoint(receiver=CHECKPOINT.receiver, sequence=CHECKPOINT.sequence + 1)
    resumed = runtime.launch(reader_request(checkpoint=checkpoint))
    repeated = runtime.launch(reader_request(checkpoint=checkpoint))
    assert resumed == repeated
    assert resumed.reader_id == initial.reader_id
    assert resumed.checkpoint == checkpoint
    assert len(cluster.created) == 2


def test_existing_job_with_another_position_is_refused() -> None:
    cluster = FakeCluster()
    runtime = launcher(cluster)
    runtime.launch(reader_request())

    (job,) = cluster.created
    job["metadata"]["annotations"][ANNOTATION_CHECKPOINT] = "DEMOJRN0099:1"
    cluster.jobs[job["metadata"]["name"]] = job

    with pytest.raises(LauncherError) as captured:
        runtime.launch(reader_request())
    assert captured.value.code == "needs_recovery"


def test_existing_job_with_another_intent_is_refused() -> None:
    cluster = FakeCluster()
    runtime = launcher(cluster)
    runtime.launch(reader_request())

    (job,) = cluster.created
    job["metadata"]["labels"][LABEL_INTENT] = "a" * 32
    cluster.jobs[job["metadata"]["name"]] = job

    with pytest.raises(LauncherError):
        runtime.launch(reader_request())


def test_ambiguous_create_is_resolved_by_reading_back() -> None:
    cluster = FakeCluster()
    runtime = launcher(cluster)
    runtime.launch(reader_request())
    runtime = launcher(cluster)
    cluster.create_error = CODE_UNAVAILABLE

    receipt = runtime.launch(reader_request())

    assert receipt.status == "RUNNING"


def test_ambiguous_create_without_job_fails_closed() -> None:
    cluster = FakeCluster(create_error=CODE_UNAVAILABLE)
    runtime = launcher(cluster)

    with pytest.raises(LauncherError) as captured:
        runtime.launch(reader_request())
    assert captured.value.code == "launch_failed"


def test_unauthorized_launch_is_not_retried() -> None:
    cluster = FakeCluster()
    cluster.create_error = "jobs_api_unauthorized"
    runtime = launcher(cluster)

    with pytest.raises(LauncherError):
        runtime.launch(reader_request())


def test_run_prefix_must_stay_relative() -> None:
    client = KubernetesJobsClient(FakeCluster(), NAMESPACE)
    for prefix in ("", "/absolute", "a/../b"):
        with pytest.raises(LauncherError):
            ReaderJobLauncher(client, template(), raw_prefix_root=prefix)


def test_intent_with_unsafe_characters_is_refused() -> None:
    cluster = FakeCluster()
    with pytest.raises(LauncherError):
        launcher(cluster).launch(reader_request(intent_id="bad intent/../../etc"))


def history_request() -> HistoryLaunchRequest:
    return HistoryLaunchRequest(
        intent_id=INTENT,
        prepare_intent_id="9c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f",
        reader_id=READER_JOB_PREFIX + "-abcdef123456",
        checkpoint=CHECKPOINT,
        manifest=MANIFEST,
        lanes=(HistoricalLane(slot=1, tables=MANIFEST, row_count=10, data_size=10),),
        max_concurrency=1,
    )


def history_launcher(cluster: FakeCluster, **kwargs: object) -> HistoryJobLauncher:
    client = KubernetesJobsClient(cluster, NAMESPACE)
    return HistoryJobLauncher(client, template(**kwargs), run_prefix_root="as400/sales")


def test_history_job_reuses_the_reader_position_and_lanes() -> None:
    cluster = FakeCluster()
    receipt = history_launcher(cluster).launch(history_request())

    (job,) = cluster.created
    annotations = job["metadata"]["annotations"]
    assert annotations[ANNOTATION_CHECKPOINT] == "DEMOJRN0100:250"
    assert annotations[ANNOTATION_PREPARE_INTENT] == history_request().prepare_intent_id
    assert job["metadata"]["labels"][LABEL_KIND] == "history"
    env = {item["name"]: item.get("value") for item in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["AS400_CONSOLE_SNAPSHOT_S3_KEY"] == "as400/sales/fleet/history/console-snapshot.json"
    assert receipt.orchestrator_id.startswith(HISTORY_JOB_PREFIX + "-")
    assert receipt.lanes == history_request().lanes
    assert receipt.reader_id == history_request().reader_id


def test_history_job_is_idempotent_for_one_intent() -> None:
    cluster = FakeCluster()
    runtime = history_launcher(cluster)

    assert runtime.launch(history_request()) == runtime.launch(history_request())
    assert len(cluster.created) == 1


def test_history_job_refuses_a_different_prepare_intent() -> None:
    cluster = FakeCluster()
    runtime = history_launcher(cluster)
    runtime.launch(history_request())

    (job,) = cluster.created
    job["metadata"]["annotations"][ANNOTATION_PREPARE_INTENT] = "0" * 32
    cluster.jobs[job["metadata"]["name"]] = job

    with pytest.raises(LauncherError) as captured:
        runtime.launch(history_request())
    assert captured.value.code == "needs_recovery"
