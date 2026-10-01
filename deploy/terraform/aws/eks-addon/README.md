# Module `aws/eks-addon`

Lie, par IRSA (IAM Roles for Service Accounts), le `ServiceAccount`
Kubernetes du control plane Quadringent à la politique IAM bornée créée par
`aws/base`, pour un cluster EKS **existant**. Ce module ne crée ni ne modifie
le cluster : c'est le cas `--target cluster` de l'installateur.

## Ressources créées

- Rôle IAM avec confiance OIDC restreinte au `sub` (namespace + nom du
  ServiceAccount) et à l'`aud` (`sts.amazonaws.com`) du fournisseur OIDC
  fourni en entrée.
- Attachement de la politique bornée `runtime_policy_arn` (sortie de
  `aws/base`) à ce rôle.

## Prérequis côté site

- Un cluster EKS existant avec un fournisseur OIDC IAM associé (`aws eks
  describe-cluster --name <cluster> --query cluster.identity.oidc.issuer`,
  puis le fournisseur IAM correspondant via `aws iam list-open-id-connect-providers`).
- Le module `aws/base` déjà appliqué, pour obtenir `runtime_policy_arn`.

## Variables principales

| Variable | Description | Défaut |
|---|---|---|
| `name` | Nom court du site, identique à `aws/base` | — obligatoire |
| `runtime_policy_arn` | ARN de la politique bornée (sortie `aws/base`) | — obligatoire |
| `oidc_provider_arn` | ARN du fournisseur OIDC IAM du cluster EKS | — obligatoire |
| `oidc_provider_url` | URL du fournisseur OIDC, sans `https://` | — obligatoire |
| `namespace` | Namespace du ServiceAccount | `quadringent` |
| `service_account_name` | Nom du ServiceAccount | `quadringent-control-plane` |

## Sortie

`role_arn` : à reporter dans `controlPlane.serviceAccount.roleArn` (values
de la chart) et `controlPlane.serviceAccount.annotations` si le site gère
l'annotation `eks.amazonaws.com/role-arn` hors chart.

## Utilisation

```sh
terraform init
terraform apply \
  -var name=demo-int \
  -var runtime_policy_arn=arn:aws:iam::000000000000:policy/demo-int-quadringent-runtime \
  -var oidc_provider_arn=arn:aws:iam::000000000000:oidc-provider/oidc.eks.eu-west-3.amazonaws.com/id/XXXX \
  -var oidc_provider_url=oidc.eks.eu-west-3.amazonaws.com/id/XXXX
```

## Validation locale (sans backend, sans accès cloud)

```sh
terraform init -backend=false
terraform validate
terraform fmt -check
```
