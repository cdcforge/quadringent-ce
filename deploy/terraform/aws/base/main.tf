# Module aws/base — stockage brut, état des checkpoints et identité IAM
# bornée pour un site Quadringent. Aucune ressource applicative (VM, cluster) :
# voir les modules aws/vm et aws/eks-addon pour l'exécution.

locals {
  tags = merge(
    {
      purpose          = "quadringent"
      quadringent-site = var.name
    },
    var.tags,
  )

  bucket_name = "${var.name}-quadringent-raw"
  table_name  = "${var.name}-quadringent-checkpoints"
  role_name   = "${var.name}-quadringent-runtime"
  policy_name = "${var.name}-quadringent-runtime"

  # --existing-bucket/--existing-checkpoint-table de l'installateur : réutilise
  # un bucket/une table déjà créés (par ex. par une plateforme data existante)
  # au lieu d'en créer de nouveaux. Le module ne prend alors la main ni sur
  # leur configuration (versioning, chiffrement, cycle de vie) ni sur leur
  # cycle de vie Terraform : seule l'identité IAM bornée est calculée.
  create_bucket = var.existing_bucket_name == ""
  create_table  = var.existing_checkpoint_table_name == ""

  raw_bucket_id   = local.create_bucket ? aws_s3_bucket.raw[0].id : data.aws_s3_bucket.existing_raw[0].id
  raw_bucket_arn  = local.create_bucket ? aws_s3_bucket.raw[0].arn : data.aws_s3_bucket.existing_raw[0].arn
  raw_bucket_name = local.create_bucket ? aws_s3_bucket.raw[0].bucket : data.aws_s3_bucket.existing_raw[0].bucket

  checkpoint_table_name = local.create_table ? aws_dynamodb_table.checkpoints[0].name : data.aws_dynamodb_table.existing_checkpoints[0].name
  checkpoint_table_arn  = local.create_table ? aws_dynamodb_table.checkpoints[0].arn : data.aws_dynamodb_table.existing_checkpoints[0].arn
}

data "aws_caller_identity" "current" {}

# --- Stockage brut (S3) -----------------------------------------------------

resource "aws_s3_bucket" "raw" {
  count  = local.create_bucket ? 1 : 0
  bucket = local.bucket_name
  tags   = local.tags
}

data "aws_s3_bucket" "existing_raw" {
  count  = local.create_bucket ? 0 : 1
  bucket = var.existing_bucket_name
}

# La configuration ci-dessous (versioning, chiffrement, blocage public, cycle
# de vie) n'est appliquée que sur un bucket créé par ce module : un bucket
# existant reste sous la configuration et la gouvernance de son propriétaire
# actuel, jamais modifiées en silence par cette installation.

resource "aws_s3_bucket_versioning" "raw" {
  count  = local.create_bucket ? 1 : 0
  bucket = aws_s3_bucket.raw[0].id
  versioning_configuration {
    status = var.bucket_versioning ? "Enabled" : "Suspended"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "raw" {
  count  = local.create_bucket ? 1 : 0
  bucket = aws_s3_bucket.raw[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "raw" {
  count                   = local.create_bucket ? 1 : 0
  bucket                  = aws_s3_bucket.raw[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "raw" {
  count  = local.create_bucket && var.lifecycle_expiration_days > 0 ? 1 : 0
  bucket = aws_s3_bucket.raw[0].id

  rule {
    id     = "expiration"
    status = "Enabled"

    filter {}

    expiration {
      days = var.lifecycle_expiration_days
    }

    noncurrent_version_expiration {
      noncurrent_days = var.lifecycle_expiration_days
    }
  }
}

# --- État des checkpoints (DynamoDB) ---------------------------------------

resource "aws_dynamodb_table" "checkpoints" {
  count        = local.create_table ? 1 : 0
  name         = local.table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = var.checkpoint_table_point_in_time_recovery
  }

  server_side_encryption {
    enabled = true
  }

  tags = local.tags
}

data "aws_dynamodb_table" "existing_checkpoints" {
  count = local.create_table ? 0 : 1
  name  = var.existing_checkpoint_table_name
}

# --- Identité IAM bornée -----------------------------------------------------
# Politique restreinte au bucket et à la table de ce site uniquement :
# lecture/écriture/liste sur le préfixe du bucket, lecture/écriture sur la
# table de checkpoints. Aucun droit de suppression d'objet ou de table.

data "aws_iam_policy_document" "runtime" {
  statement {
    sid    = "S3RawObjects"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:ListBucket",
    ]
    resources = [
      local.raw_bucket_arn,
      "${local.raw_bucket_arn}/*",
    ]
  }

  statement {
    sid    = "DynamoDbCheckpoints"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:Query",
    ]
    resources = [local.checkpoint_table_arn]
  }
}

resource "aws_iam_policy" "runtime" {
  name        = local.policy_name
  description = "Accès borné au bucket brut et à la table de checkpoints du site ${var.name}."
  policy      = data.aws_iam_policy_document.runtime.json
  tags        = local.tags
}

# Rôle IAM générique : le document de confiance (IRSA sur EKS, profil
# d'instance EC2) est fourni par les modules aws/eks-addon ou aws/vm, qui
# référencent cette politique. Ce module expose un rôle « vide » utilisable
# directement en tant que profil d'instance, avec une confiance EC2 par
# défaut ; aws/eks-addon crée son propre rôle avec confiance OIDC.

data "aws_iam_policy_document" "ec2_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "runtime" {
  name               = local.role_name
  assume_role_policy = data.aws_iam_policy_document.ec2_trust.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "runtime" {
  role       = aws_iam_role.runtime.name
  policy_arn = aws_iam_policy.runtime.arn
}

resource "aws_iam_instance_profile" "runtime" {
  name = local.role_name
  role = aws_iam_role.runtime.name
  tags = local.tags
}
