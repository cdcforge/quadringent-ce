output "role_arn" {
  description = "ARN du rôle IRSA à déclarer dans controlPlane.serviceAccount.roleArn (values de la chart)."
  value       = aws_iam_role.irsa.arn
}

output "service_account_name" {
  description = "Nom du ServiceAccount Kubernetes attendu par ce rôle (doit correspondre à controlPlane.serviceAccount.name)."
  value       = var.service_account_name
}

output "capture_service_account_name" {
  description = "Nom du ServiceAccount Kubernetes de capture attendu par ce rôle (doit correspondre à serviceAccount.name)."
  value       = var.capture_service_account_name
}

output "namespace" {
  description = "Namespace Kubernetes attendu par ce rôle."
  value       = var.namespace
}
