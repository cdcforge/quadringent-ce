"""Noms Kubernetes valides dans le rendu de la chart.

``helm template`` ne valide pas ces règles de l'API : un nom de port de plus
de 15 caractères n'échoue qu'à l'installation réelle (constaté sur GKE le
23 septembre 2026 avec ``control-plane-v2``).
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PORT_NAME = re.compile(r"^(?=.*[a-z])[a-z0-9]([a-z0-9-]{0,13}[a-z0-9])?$")
def _cases() -> list[dict]:
    from quadringent.installer.manifest import ReleaseManifest
    from quadringent.installer.plan import InstallInputs, build_chart_values

    manifest = ReleaseManifest(
        repository="ghcr.io/quadringent/quadringent",
        image_digest="sha256:" + "0" * 64,
        control_plane_image_digest="sha256:" + "1" * 64,
        verifier_image_digest="sha256:" + "3" * 64,
        observability_image_digest="sha256:" + "2" * 64,
    )
    gcp = InstallInputs(cloud="gcp", target="cluster", region="europe-west1", name="demo-int", project="example-gcp-project")
    aws = InstallInputs(cloud="aws", target="cluster", region="eu-west-3", name="demo-int")
    return [
        build_chart_values(gcp, manifest, control_plane_role_arn="",
                           control_plane_gcp_service_account="demo-int-quadringent-runtime@example-project.iam.gserviceaccount.com"),
        build_chart_values(aws, manifest, control_plane_role_arn="arn:aws:iam::000000000000:role/demo-int-quadringent-irsa"),
    ]


def _render(values: dict) -> list[dict]:
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump(values, handle)
        path = handle.name
    out = subprocess.run(
        ["helm", "template", "demo-int", str(ROOT / "chart"), "--namespace", "quadringent", "-f", path],
        capture_output=True, text=True, check=True,
    ).stdout
    Path(path).unlink()
    return [doc for doc in yaml.safe_load_all(out) if doc]


def _port_names(node, found: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "ports" and isinstance(value, list):
                found.extend(p["name"] for p in value if isinstance(p, dict) and "name" in p)
            if key in ("port", "targetPort") and isinstance(value, str):
                found.append(value)
            _port_names(value, found)
    elif isinstance(node, list):
        for item in node:
            _port_names(item, found)


@pytest.mark.parametrize("case", [0, 1], ids=["gcp-cluster", "aws-cluster"])
def test_every_port_name_is_a_valid_iana_svc_name(case: int) -> None:
    names: list[str] = []
    for doc in _render(_cases()[case]):
        _port_names(doc, names)
    assert names, "aucun port nommé trouvé : rendu inattendu"
    invalid = sorted({name for name in names if not PORT_NAME.fullmatch(name)})
    assert invalid == [], f"noms de port invalides (≤ 15 caractères, minuscules, tirets) : {invalid}"


@pytest.mark.parametrize("case", [0, 1], ids=["gcp-cluster", "aws-cluster"])
def test_capture_service_account_is_created_and_never_default(case: int) -> None:
    """Avant ce correctif, l'installateur publiait serviceAccount:
    {create: false, name: "default"} : les pods de capture tournaient sous le
    ServiceAccount `default` du namespace, sans aucun droit S3/GCS. La chart
    doit désormais créer un ServiceAccount de capture dédié, annoté de
    l'identité cloud attendue par le backend de stockage déclaré."""
    values = _cases()[case]
    service_account = values["serviceAccount"]
    assert service_account["create"] is True
    assert service_account["name"] == "quadringent-capture"
    assert service_account["name"] != "default"

    docs = _render(values)
    capture_sa = next(
        d for d in docs if d["kind"] == "ServiceAccount" and d["metadata"]["name"] == "quadringent-capture"
    )
    annotations = capture_sa["metadata"]["annotations"]
    if values["storage"]["backend"] == "gcs":
        assert annotations["iam.gke.io/gcp-service-account"] == service_account["gcpServiceAccount"]
        assert "eks.amazonaws.com/role-arn" not in annotations
    else:
        assert annotations["eks.amazonaws.com/role-arn"] == service_account["roleArn"]
        assert "iam.gke.io/gcp-service-account" not in annotations

    control_plane_deployment = next(
        d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-control-plane")
    )
    v2 = next(
        c for c in control_plane_deployment["spec"]["template"]["spec"]["containers"]
        if c["name"] == "control-plane-v2"
    )
    capture_env = next(e for e in v2["env"] if e["name"] == "QUADRINGENT_V2_CAPTURE_SERVICE_ACCOUNT")
    assert capture_env["value"] == "quadringent-capture"


def test_gcs_site_configmap_publishes_checkpoint_bucket_and_no_table() -> None:
    """entrypoint.py::build_pipeline_executor lit QUADRINGENT_CHECKPOINT_BUCKET
    pour storage_backend=gcs (jamais QUADRINGENT_CHECKPOINT_TABLE, qui ne
    concerne que AWS) : sans cette clé dans le ConfigMap -site, l'EvidenceReader
    résolvait toujours un emplacement de checkpoints vide sur GCS."""
    values = _cases()[0]
    assert values["storage"]["backend"] == "gcs"
    docs = _render(values)
    site_configmap = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("-site"))
    assert site_configmap["data"]["QUADRINGENT_CHECKPOINT_BUCKET"] == values["storage"]["checkpointBucket"]
    assert site_configmap["data"]["QUADRINGENT_CHECKPOINT_TABLE"] == ""


def test_aws_site_configmap_has_no_checkpoint_bucket_key() -> None:
    values = _cases()[1]
    assert values["storage"]["backend"] == "aws"
    docs = _render(values)
    site_configmap = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("-site"))
    assert "QUADRINGENT_CHECKPOINT_BUCKET" not in site_configmap["data"]
    assert site_configmap["data"]["QUADRINGENT_CHECKPOINT_TABLE"]


def test_postgres_runs_as_the_image_postgres_user() -> None:
    """``runAsNonRoot`` sans ``runAsUser`` échoue avec l'image officielle,
    qui démarre en root puis bascule vers ``postgres`` (uid 70 en alpine) :
    constaté sur GKE (CreateContainerConfigError)."""
    docs = _render(_cases()[0])
    statefulset = next(d for d in docs if d["kind"] == "StatefulSet" and "postgres" in d["metadata"]["name"])
    pod = statefulset["spec"]["template"]["spec"]["securityContext"]
    assert pod["runAsNonRoot"] is True
    assert (pod["runAsUser"], pod["runAsGroup"], pod["fsGroup"]) == (70, 70, 70)


@pytest.mark.parametrize("values", _cases(), ids=["gcp", "aws"])
def test_control_plane_v2_probes_run_in_the_pod_on_loopback(values: dict) -> None:
    """Le control plane v2 écoute sur 127.0.0.1 : une sonde httpGet (IP du Pod)
    échoue toujours et redémarre le conteneur en boucle (constaté sur GKE)."""
    deployment = next(doc for doc in _render(values)
                      if doc["kind"] == "Deployment" and doc["metadata"]["name"].endswith("-control-plane"))
    v2 = next(c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == "control-plane-v2")
    for probe in ("readinessProbe", "livenessProbe"):
        assert "httpGet" not in v2[probe]
        assert v2[probe]["exec"]["command"] == [
            "python", "-S", "/app/quadringent_healthcheck.py", "8845", "/v2/healthz",
        ]


@pytest.mark.parametrize("values", _cases(), ids=["gcp", "aws"])
def test_control_plane_mounts_its_token_when_v2_drives_kubernetes(values: dict) -> None:
    """Le control plane v2 crée des Jobs/Deployments : sans jeton de
    ServiceAccount monté, il plantait au démarrage (constaté sur GKE)."""
    docs = _render(values)
    deployment = next(doc for doc in docs
                      if doc["kind"] == "Deployment" and doc["metadata"]["name"].endswith("-control-plane"))
    assert deployment["spec"]["template"]["spec"]["automountServiceAccountToken"] is True
    account = next(doc for doc in docs if doc["kind"] == "ServiceAccount"
                   and doc["metadata"]["name"] == deployment["spec"]["template"]["spec"]["serviceAccountName"])
    assert account.get("automountServiceAccountToken") is True
