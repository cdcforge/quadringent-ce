# Module gcp/base — bucket GCS brut et compte de service borné pour un site
# Quadringent. La variante GCS n'a pas d'équivalent DynamoDB : les
# checkpoints et la garde de sign-on vivent dans le même bucket, sous les
# préfixes checkpoints/ et source-gates/ (voir src/quadringent/gcs_backend.py).

locals {
  labels = merge(
    {
      purpose          = "quadringent"
      quadringent-site = var.name
    },
    var.labels,
  )

  bucket_name = "${var.name}-quadringent-raw"
  # Un compte de service GCP accepte au plus 30 caractères. Garder l'ID
  # historique des noms courts ; condenser les noms longs avec une empreinte
  # stable pour préserver leur unicité sans limiter le nom du site.
  sa_id = length(var.name) <= 10 ? "${var.name}-quadringent-runtime" : "${substr(var.name, 0, 18)}-${substr(sha1(var.name), 0, 10)}"

  # --existing-bucket de l'installateur : réutilise un bucket GCS déjà créé
  # au lieu d'en créer un nouveau. Un bucket existant n'est ni configuré
  # (versioning) ni géré en cycle de vie Terraform par ce module — seule
  # l'identité de service lui est bornée.
  create_bucket   = var.existing_bucket_name == ""
  raw_bucket_name = local.create_bucket ? google_storage_bucket.raw[0].name : data.google_storage_bucket.existing_raw[0].name
}

resource "google_storage_bucket" "raw" {
  count                       = local.create_bucket ? 1 : 0
  name                        = local.bucket_name
  project                     = var.project_id
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = local.labels

  versioning {
    enabled = var.bucket_versioning
  }
}

data "google_storage_bucket" "existing_raw" {
  count = local.create_bucket ? 0 : 1
  name  = var.existing_bucket_name
}

resource "google_service_account" "runtime" {
  project      = var.project_id
  account_id   = local.sa_id
  display_name = "Quadringent runtime (${var.name})"
  description  = "Identité de capture bornée au bucket ${local.raw_bucket_name} : lecture, création, liste ; remplacement limité aux objets d'état."
}

# Rôle borné au bucket de ce site uniquement (pas de rôle projet). objectCreator
# + objectViewer couvrent get/create/list sans droit de suppression ni
# d'écrasement arbitraire (les écritures applicatives sont create-if-absent ou
# compare-and-set par génération, voir GcsObjectStore/GcsCheckpointStore).
resource "google_storage_bucket_iam_member" "runtime_object_creator" {
  bucket = local.raw_bucket_name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_storage_bucket_iam_member" "runtime_object_viewer" {
  bucket = local.raw_bucket_name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

# Checkpoints et garde de sign-on sont des objets remplacés depuis leur
# génération : GCS exige alors storage.objects.delete. Ce droit est borné par
# condition IAM aux seuls préfixes d'état ; les lots bruts restent en écriture
# unique. Les conditions IAM exigent l'accès uniforme au bucket.
resource "google_storage_bucket_iam_member" "runtime_state_writer" {
  bucket = local.raw_bucket_name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.runtime.email}"

  condition {
    title       = "quadringent-state-prefixes"
    description = "Remplacement limité aux objets d'état (checkpoints, garde de sign-on)."
    expression  = "resource.name.startsWith(\"projects/_/buckets/${local.raw_bucket_name}/objects/checkpoints/\") || resource.name.startsWith(\"projects/_/buckets/${local.raw_bucket_name}/objects/source-gates/\")"
  }
}
