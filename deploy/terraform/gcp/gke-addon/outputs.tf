output "service_account_email" {
  description = "E-mail du compte de service GCP à annoter sur le ServiceAccount Kubernetes (iam.gke.io/gcp-service-account)."
  value       = var.service_account_email
}

output "kubernetes_service_account_annotation" {
  description = "Annotation complète à poser dans controlPlane.serviceAccount.annotations."
  value = {
    "iam.gke.io/gcp-service-account" = var.service_account_email
  }
}

output "capture_kubernetes_service_account_annotation" {
  description = "Annotation complète à poser dans serviceAccount.annotations (identité de capture) — même compte de service que le control plane."
  value = {
    "iam.gke.io/gcp-service-account" = var.service_account_email
  }
}
