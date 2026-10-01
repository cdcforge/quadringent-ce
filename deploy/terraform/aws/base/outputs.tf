output "raw_bucket_name" {
  description = "Nom du bucket S3 brut (AS400_RAW_BUCKET côté runtime) — créé par ce module, ou --existing-bucket réutilisé tel quel."
  value       = local.raw_bucket_name
}

output "raw_bucket_arn" {
  description = "ARN du bucket S3 brut."
  value       = local.raw_bucket_arn
}

output "checkpoint_table_name" {
  description = "Nom de la table DynamoDB de checkpoints (AS400_CHECKPOINT_TABLE côté runtime) — créée par ce module, ou --existing-checkpoint-table réutilisée telle quelle."
  value       = local.checkpoint_table_name
}

output "checkpoint_table_arn" {
  description = "ARN de la table DynamoDB de checkpoints."
  value       = local.checkpoint_table_arn
}

output "runtime_policy_arn" {
  description = "ARN de la politique IAM bornée (bucket + table de ce site), à réutiliser par aws/vm et aws/eks-addon."
  value       = aws_iam_policy.runtime.arn
}

output "runtime_role_name" {
  description = "Nom du rôle IAM générique (confiance EC2), utilisable en profil d'instance."
  value       = aws_iam_role.runtime.name
}

output "runtime_role_arn" {
  description = "ARN du rôle IAM générique."
  value       = aws_iam_role.runtime.arn
}

output "runtime_instance_profile_name" {
  description = "Nom du profil d'instance EC2 attaché au rôle générique, pour aws/vm."
  value       = aws_iam_instance_profile.runtime.name
}

output "account_id" {
  description = "Identifiant du compte AWS courant (pour composer des ARN dans les modules dépendants)."
  value       = data.aws_caller_identity.current.account_id
}
