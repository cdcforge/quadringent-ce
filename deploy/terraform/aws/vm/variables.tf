variable "name" {
  description = "Nom court du site Quadringent, identique à celui passé à aws/base."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,40}$", var.name))
    error_message = "name doit être en minuscules, commencer par une lettre, 2 à 41 caractères, tirets autorisés."
  }
}

variable "vpc_id" {
  description = "VPC existant où placer la VM (le module ne crée pas de réseau)."
  type        = string
}

variable "subnet_id" {
  description = "Sous-réseau existant, idéalement privé (aucune IP publique par défaut)."
  type        = string
}

variable "instance_profile_name" {
  description = "Nom du profil d'instance produit par aws/base (sortie runtime_instance_profile_name)."
  type        = string
}

variable "instance_role_name" {
  description = "Nom du rôle EC2 produit par aws/base, utilisé pour attacher la politique SSM minimale."
  type        = string
}

variable "architecture" {
  description = "Architecture du processeur : arm64 (Graviton, recommandé) ou x86_64."
  type        = string
  default     = "x86_64"

  validation {
    condition     = contains(["arm64", "x86_64"], var.architecture)
    error_message = "architecture doit être arm64 ou x86_64."
  }
}

variable "instance_type" {
  description = "Type d'instance EC2. Doit correspondre à l'architecture choisie (ex. t4g.medium pour arm64, t3.medium pour x86_64)."
  type        = string
  default     = "t3.medium"
}

variable "root_volume_size_gb" {
  description = "Taille du volume racine (Go), qui porte aussi l'état k3s/Postgres embarqué."
  type        = number
  default     = 40

  validation {
    condition     = var.root_volume_size_gb >= 20
    error_message = "root_volume_size_gb doit être au moins 20 Go."
  }
}

variable "allowed_ssh_cidrs" {
  description = "CIDR autorisés en entrée SSH (22/tcp). Vide par défaut : aucun accès SSH direct, on passe par SSM Session Manager."
  type        = list(string)
  default     = []
}

variable "allowed_ui_cidrs" {
  description = "CIDR autorisés en entrée directe sur le port de l'UI Quadringent. Vide par défaut : accès uniquement par SSM port-forward (aucune ingress publique)."
  type        = list(string)
  default     = []
}

variable "ui_port" {
  description = "Port TCP de l'UI/control plane Quadringent exposé par k3s."
  type        = number
  default     = 8844
}

variable "ssh_key_name" {
  description = "Nom d'une paire de clés EC2 existante, pour un accès SSH direct optionnel. Vide : pas de clé associée (accès par SSM uniquement)."
  type        = string
  default     = ""
}

variable "k3s_channel" {
  description = "Canal de version k3s (ex. stable, latest). Voir https://update.k3s.io/v1-release/channels."
  type        = string
  default     = "stable"
}

variable "tags" {
  description = "Étiquettes additionnelles fusionnées avec purpose=quadringent."
  type        = map(string)
  default     = {}
}
