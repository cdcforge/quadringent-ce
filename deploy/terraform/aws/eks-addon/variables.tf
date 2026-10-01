variable "name" {
  description = "Nom court du site Quadringent, identique à celui passé à aws/base."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,40}$", var.name))
    error_message = "name doit être en minuscules, commencer par une lettre, 2 à 41 caractères, tirets autorisés."
  }
}

variable "runtime_policy_arn" {
  description = "ARN de la politique IAM bornée produite par le module aws/base (sortie runtime_policy_arn)."
  type        = string

  validation {
    condition     = can(regex("^arn:aws:iam::[0-9]{12}:policy/", var.runtime_policy_arn))
    error_message = "runtime_policy_arn doit être un ARN de politique IAM valide."
  }
}

variable "oidc_provider_arn" {
  description = "ARN du fournisseur OIDC du cluster EKS existant (aws eks describe-cluster / IAM OIDC provider)."
  type        = string

  validation {
    condition     = can(regex("^arn:aws:iam::[0-9]{12}:oidc-provider/", var.oidc_provider_arn))
    error_message = "oidc_provider_arn doit être un ARN de fournisseur OIDC IAM valide."
  }
}

variable "oidc_provider_url" {
  description = "URL du fournisseur OIDC du cluster EKS, sans le schéma https:// (ex. oidc.eks.eu-west-3.amazonaws.com/id/XXXX)."
  type        = string

  validation {
    condition     = !can(regex("^https://", var.oidc_provider_url))
    error_message = "oidc_provider_url ne doit pas inclure le schéma https:// (Terraform le retire pour composer la condition de confiance)."
  }
}

variable "namespace" {
  description = "Namespace Kubernetes où tourne le ServiceAccount Quadringent (IRSA)."
  type        = string
  default     = "quadringent"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.namespace))
    error_message = "namespace doit être un nom Kubernetes valide (DNS-1123 label)."
  }
}

variable "service_account_name" {
  description = "Nom du ServiceAccount Kubernetes du control plane, associé au rôle IRSA."
  type        = string
  default     = "quadringent-control-plane"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.service_account_name))
    error_message = "service_account_name doit être un nom Kubernetes valide (DNS-1123 label)."
  }
}

variable "capture_service_account_name" {
  description = "Nom du ServiceAccount Kubernetes de capture (chart.serviceAccount.name), associé au même rôle IRSA que le control plane."
  type        = string
  default     = "quadringent-capture"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.capture_service_account_name))
    error_message = "capture_service_account_name doit être un nom Kubernetes valide (DNS-1123 label)."
  }
}

variable "tags" {
  description = "Étiquettes additionnelles fusionnées avec purpose=quadringent."
  type        = map(string)
  default     = {}
}
