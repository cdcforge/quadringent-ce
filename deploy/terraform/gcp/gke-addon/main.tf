# Module gcp/gke-addon — liaison Workload Identity entre les ServiceAccounts
# Kubernetes Quadringent (control plane et capture) et le compte de service
# borné produit par gcp/base, pour un cluster GKE existant (Workload Identity
# déjà activé sur le cluster ; ce module ne crée ni ne modifie le cluster).
#
# Une seule GSA runtime liée aux deux KSA (control plane et capture) : gcp/base
# ne produit qu'une identité (objectCreator + objectViewer + remplacement
# borné aux préfixes d'état — voir gcp/base/main.tf). Séparer les deux
# identités (un GSA en lecture seule pour le control plane, un GSA en
# lecture/écriture pour la capture) exigerait de dupliquer les liaisons IAM du
# bucket dans gcp/base ; hors périmètre de ce correctif, qui répare un défaut
# plus pressant (les pods de capture tournaient jusqu'ici sous le
# ServiceAccount `default`, sans aucun droit GCS — voir
# chart/templates/serviceaccount.yaml).

locals {
  member         = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/${var.kubernetes_service_account_name}]"
  capture_member = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/${var.capture_kubernetes_service_account_name}]"
}

resource "google_service_account_iam_member" "workload_identity" {
  service_account_id = "projects/${var.project_id}/serviceAccounts/${var.service_account_email}"
  role               = "roles/iam.workloadIdentityUser"
  member             = local.member
}

resource "google_service_account_iam_member" "workload_identity_capture" {
  service_account_id = "projects/${var.project_id}/serviceAccounts/${var.service_account_email}"
  role               = "roles/iam.workloadIdentityUser"
  member             = local.capture_member
}
