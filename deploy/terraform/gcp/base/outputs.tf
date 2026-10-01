output "raw_bucket_name" {
  description = "Nom du bucket GCS brut (AS400_RAW_BUCKET et AS400_CHECKPOINT_BUCKET côté runtime : même bucket, préfixes distincts) — créé par ce module, ou --existing-bucket réutilisé tel quel."
  value       = local.raw_bucket_name
}

output "service_account_email" {
  description = "E-mail du compte de service borné à ce bucket, à réutiliser par gcp/vm et gcp/gke-addon."
  value       = google_service_account.runtime.email
}

output "service_account_name" {
  description = "Nom pleinement qualifié du compte de service (projects/<project>/serviceAccounts/<email>)."
  value       = google_service_account.runtime.name
}
