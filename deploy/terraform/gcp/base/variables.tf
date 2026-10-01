variable "project_id" {
  description = "Projet GCP de déploiement (aucune valeur par défaut : déclaré explicitement par le site)."
  type        = string
}

variable "name" {
  description = "Nom court du site Quadringent (préfixe des ressources créées)."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,40}$", var.name))
    error_message = "name doit être en minuscules, commencer par une lettre, 2 à 41 caractères, tirets autorisés."
  }
}

variable "region" {
  description = "Région GCP de déploiement."
  type        = string
}

variable "bucket_versioning" {
  description = "Active le versioning du bucket GCS brut (recommandé pour la voie de preuve)."
  type        = bool
  default     = true
}

variable "labels" {
  description = "Étiquettes additionnelles fusionnées avec purpose=quadringent."
  type        = map(string)
  default     = {}
}

variable "existing_bucket_name" {
  description = "Nom d'un bucket GCS déjà créé à réutiliser (--existing-bucket de l'installateur) au lieu d'en créer un nouveau. Vide (défaut) : le module crée son propre bucket. Un bucket existant n'est ni configuré (versioning) ni géré en cycle de vie Terraform par ce module — seule l'identité de service lui est bornée."
  type        = string
  default     = ""
}
