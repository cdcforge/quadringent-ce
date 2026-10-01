# Module aws/eks-addon — rôle IRSA liant les ServiceAccounts Kubernetes
# Quadringent (control plane et capture) à la politique IAM bornée produite
# par aws/base, pour un cluster EKS existant (le module ne crée ni ne modifie
# le cluster).
#
# Un seul rôle IRSA assumable par les deux KSA (control plane et capture) :
# aws/base ne produit qu'une politique bornée (accès S3 + DynamoDB du site —
# voir aws/base/main.tf). Séparer les deux identités (un rôle en lecture
# seule pour le control plane, un rôle en lecture/écriture pour la capture)
# exigerait de dupliquer la politique IAM dans aws/base ; hors périmètre de
# ce correctif, qui répare un défaut plus pressant (les pods de capture
# tournaient jusqu'ici sous le ServiceAccount `default`, sans aucun droit
# S3/DynamoDB — voir chart/templates/serviceaccount.yaml). La confiance IRSA
# admet indifféremment l'un ou l'autre subject (StringEquals sur une liste
# vaut OR, pas AND).

locals {
  tags = merge(
    {
      purpose          = "quadringent"
      quadringent-site = var.name
    },
    var.tags,
  )

  role_name       = "${var.name}-quadringent-irsa"
  subject         = "system:serviceaccount:${var.namespace}:${var.service_account_name}"
  capture_subject = "system:serviceaccount:${var.namespace}:${var.capture_service_account_name}"
}

data "aws_iam_policy_document" "irsa_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider_url}:sub"
      values   = [local.subject, local.capture_subject]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider_url}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "irsa" {
  name               = local.role_name
  assume_role_policy = data.aws_iam_policy_document.irsa_trust.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "irsa" {
  role       = aws_iam_role.irsa.name
  policy_arn = var.runtime_policy_arn
}
