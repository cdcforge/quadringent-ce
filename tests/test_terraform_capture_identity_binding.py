"""Liaison d'identité cloud des ServiceAccounts de capture.

Avant ce correctif, `deploy/terraform/gcp/gke-addon` et
`deploy/terraform/aws/eks-addon` ne liaient que le ServiceAccount Kubernetes
du control plane à l'identité runtime bornée produite par `<cloud>/base` :
les pods de capture (Deployment "capture", Jobs/Deployments de l'exécuteur
v2) tournaient sous le ServiceAccount `default` du namespace, sans aucun
droit S3/GCS. Les deux modules lient désormais la même identité runtime aux
deux ServiceAccounts (control plane et capture) — voir la note de décision
dans chart/templates/serviceaccount.yaml (une seule GSA/rôle IAM, deux KSA).
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GCP_MAIN = ROOT / "deploy/terraform/gcp/gke-addon/main.tf"
GCP_VARS = ROOT / "deploy/terraform/gcp/gke-addon/variables.tf"
AWS_MAIN = ROOT / "deploy/terraform/aws/eks-addon/main.tf"
AWS_VARS = ROOT / "deploy/terraform/aws/eks-addon/variables.tf"


def test_gcp_gke_addon_declares_a_capture_kubernetes_service_account_variable() -> None:
    text = GCP_VARS.read_text()
    assert 'variable "capture_kubernetes_service_account_name"' in text
    assert 'default     = "quadringent-capture"' in text


def test_gcp_gke_addon_binds_both_ksa_to_the_same_runtime_gsa() -> None:
    text = GCP_MAIN.read_text()
    assert 'resource "google_service_account_iam_member" "workload_identity"' in text
    assert 'resource "google_service_account_iam_member" "workload_identity_capture"' in text
    # Les deux liaisons pointent le même compte de service runtime (une seule
    # GSA partagée entre control plane et capture).
    assert text.count('service_account_id = "projects/${var.project_id}/serviceAccounts/${var.service_account_email}"') == 2
    assert "var.capture_kubernetes_service_account_name" in text


def test_aws_eks_addon_declares_a_capture_service_account_variable() -> None:
    text = AWS_VARS.read_text()
    assert 'variable "capture_service_account_name"' in text
    assert 'default     = "quadringent-capture"' in text


def test_aws_eks_addon_trust_policy_admits_both_service_accounts() -> None:
    text = AWS_MAIN.read_text()
    assert "capture_subject" in text
    # StringEquals sur une liste de deux sujets : la confiance IRSA admet
    # indifféremment le control plane ou la capture (OR, jamais AND).
    assert "values   = [local.subject, local.capture_subject]" in text
    # Un seul rôle IAM, jamais un second rôle dédié à la capture (décision :
    # une identité runtime partagée plutôt qu'une séparation complète).
    assert text.count('resource "aws_iam_role" "irsa"') == 1
