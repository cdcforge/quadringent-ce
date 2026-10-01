# Module `aws/base`

Crée le socle de stockage et d'identité d'un site Quadringent sur AWS, sans
créer de calcul (VM ou cluster). Utilisé par le CLI `quadringent install
--cloud aws` avant `aws/vm` ou `aws/eks-addon`.

## Ressources créées

- Bucket S3 brut (`<name>-quadringent-raw`) : versioning (option), SSE-KMS,
  blocage complet de l'accès public, règle de cycle de vie optionnelle.
- Table DynamoDB de checkpoints (`<name>-quadringent-checkpoints`) :
  `PAY_PER_REQUEST`, PITR activable, chiffrement au repos.
- Politique IAM bornée à ce bucket et cette table uniquement (aucun accès
  global S3/DynamoDB, aucun droit de suppression).
- Rôle IAM générique avec confiance EC2 et profil d'instance associé, pour un
  usage direct en profil d'instance sur `aws/vm`. Le module `aws/eks-addon`
  crée son propre rôle avec confiance OIDC et réutilise la politique en
  sortie (`runtime_policy_arn`).

Toutes les ressources portent l'étiquette `purpose = quadringent`.

## Variables principales

| Variable | Description | Défaut |
|---|---|---|
| `name` | Préfixe court du site (ex. `demo-int`) | — obligatoire |
| `region` | Région AWS | — obligatoire |
| `bucket_versioning` | Versioning du bucket brut | `true` |
| `lifecycle_expiration_days` | Expiration des objets bruts (0 = désactivé) | `0` |
| `checkpoint_table_point_in_time_recovery` | PITR DynamoDB | `true` |
| `tags` | Étiquettes additionnelles | `{}` |

Voir `variables.tf` pour le détail des contraintes de validation.

## Sorties

`raw_bucket_name`, `raw_bucket_arn`, `checkpoint_table_name`,
`checkpoint_table_arn`, `runtime_policy_arn`, `runtime_role_name`,
`runtime_role_arn`, `runtime_instance_profile_name`, `account_id`.

## Utilisation

```sh
terraform init
terraform plan -var name=demo-int -var region=eu-west-3
terraform apply -var name=demo-int -var region=eu-west-3
```

Aucun identifiant de compte n'est en dur dans le module : `region` et
`name` sont fournis par l'appelant (CLI ou tfvars générés).

## Validation locale (sans backend, sans accès cloud)

```sh
terraform init -backend=false
terraform validate
terraform fmt -check
```
