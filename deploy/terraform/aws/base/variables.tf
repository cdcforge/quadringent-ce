variable "name" {
  description = "Nom court du site Quadringent (préfixe des ressources créées, ex. quadringent-demo)."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,40}$", var.name))
    error_message = "name doit être en minuscules, commencer par une lettre, 2 à 41 caractères, tirets autorisés."
  }
}

variable "region" {
  description = "Région AWS de déploiement (aucune valeur par défaut : déclarée explicitement par le site)."
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[0-9]$", var.region))
    error_message = "region doit être une région AWS valide, ex. eu-west-3."
  }
}

variable "bucket_versioning" {
  description = "Active le versioning du bucket S3 brut (recommandé pour la voie de preuve)."
  type        = bool
  default     = true
}

variable "lifecycle_expiration_days" {
  description = "Expiration optionnelle des objets bruts en jours. 0 désactive la règle de cycle de vie."
  type        = number
  default     = 0

  validation {
    condition     = var.lifecycle_expiration_days >= 0
    error_message = "lifecycle_expiration_days doit être positif ou nul."
  }
}

variable "checkpoint_table_point_in_time_recovery" {
  description = "Active la récupération ponctuelle (PITR) de la table DynamoDB de checkpoints."
  type        = bool
  default     = true
}

variable "existing_bucket_name" {
  description = "Nom d'un bucket S3 déjà créé à réutiliser (--existing-bucket de l'installateur) au lieu d'en créer un nouveau. Vide (défaut) : le module crée son propre bucket. Un bucket existant n'est ni configuré (versioning, chiffrement, cycle de vie) ni géré en cycle de vie Terraform par ce module — seule l'identité IAM lui est bornée."
  type        = string
  default     = ""
}

variable "existing_checkpoint_table_name" {
  description = "Nom d'une table DynamoDB déjà créée à réutiliser (--existing-checkpoint-table de l'installateur) au lieu d'en créer une nouvelle. Vide (défaut) : le module crée sa propre table."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Étiquettes additionnelles fusionnées avec purpose=quadringent."
  type        = map(string)
  default     = {}
}
