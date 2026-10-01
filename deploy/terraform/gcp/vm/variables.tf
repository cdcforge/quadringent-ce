variable "project_id" {
  description = "Projet GCP, identique à celui passé à gcp/base."
  type        = string
}

variable "name" {
  description = "Nom court du site Quadringent, identique à celui passé à gcp/base."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,40}$", var.name))
    error_message = "name doit être en minuscules, commencer par une lettre, 2 à 41 caractères, tirets autorisés."
  }
}

variable "region" {
  description = "Région GCP de la VM."
  type        = string
}

variable "zone" {
  description = "Zone GCP de la VM (ex. europe-west1-b)."
  type        = string
}

variable "network" {
  description = "Réseau VPC existant (le module ne crée pas de réseau)."
  type        = string
}

variable "subnetwork" {
  description = "Sous-réseau existant, idéalement privé (aucune IP publique par défaut)."
  type        = string
}

variable "service_account_email" {
  description = "E-mail du compte de service produit par gcp/base (sortie service_account_email), attaché à la VM."
  type        = string
}

variable "machine_type" {
  description = "Type de machine Compute Engine (ex. e2-medium, t2a-standard-2 pour Arm)."
  type        = string
  default     = "e2-medium"
}

variable "boot_disk_size_gb" {
  description = "Taille du disque de démarrage (Go), qui porte aussi l'état k3s/Postgres embarqué."
  type        = number
  default     = 40

  validation {
    condition     = var.boot_disk_size_gb >= 20
    error_message = "boot_disk_size_gb doit être au moins 20 Go."
  }
}

variable "assign_public_ip" {
  description = "Attribue une IP publique à la VM. false par défaut : accès recommandé par IAP TCP forwarding, aucune ingress publique."
  type        = bool
  default     = false
}

variable "allowed_ssh_ranges" {
  description = "Plages CIDR autorisées en entrée SSH (22/tcp) en plus de la plage IAP (35.235.240.0/20, toujours autorisée pour permettre `gcloud compute ssh --tunnel-through-iap`)."
  type        = list(string)
  default     = []
}

variable "k3s_channel" {
  description = "Canal de version k3s (ex. stable, latest). Voir https://update.k3s.io/v1-release/channels."
  type        = string
  default     = "stable"
}

variable "labels" {
  description = "Étiquettes additionnelles fusionnées avec purpose=quadringent."
  type        = map(string)
  default     = {}
}
