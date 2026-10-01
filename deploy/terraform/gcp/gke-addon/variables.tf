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

variable "service_account_email" {
  description = "E-mail du compte de service borné produit par gcp/base (sortie service_account_email)."
  type        = string
}

variable "namespace" {
  description = "Namespace Kubernetes où tourne le ServiceAccount Quadringent (Workload Identity)."
  type        = string
  default     = "quadringent"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.namespace))
    error_message = "namespace doit être un nom Kubernetes valide (DNS-1123 label)."
  }
}

variable "kubernetes_service_account_name" {
  description = "Nom du ServiceAccount Kubernetes du control plane, lié par Workload Identity."
  type        = string
  default     = "quadringent-control-plane"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.kubernetes_service_account_name))
    error_message = "kubernetes_service_account_name doit être un nom Kubernetes valide (DNS-1123 label)."
  }
}

variable "capture_kubernetes_service_account_name" {
  description = "Nom du ServiceAccount Kubernetes de capture (chart.serviceAccount.name), lié par Workload Identity à la même identité runtime que le control plane."
  type        = string
  default     = "quadringent-capture"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.capture_kubernetes_service_account_name))
    error_message = "capture_kubernetes_service_account_name doit être un nom Kubernetes valide (DNS-1123 label)."
  }
}
