# Module `gcp/base`

Crée le socle de stockage et d'identité d'un site Quadringent sur GCP, sans
créer de calcul (VM ou cluster). Utilisé par le CLI `quadringent install
--cloud gcp` avant `gcp/vm` ou `gcp/gke-addon`.

## Ressources créées

- Bucket GCS brut (`<name>-quadringent-raw`) : accès uniforme au niveau
  bucket (`uniform_bucket_level_access`), prévention d'accès public
  appliquée (`public_access_prevention = enforced`), versioning activable.
- Compte de service (`<name>-quadringent-runtime` pour les noms de site de
  dix caractères ou moins ; identifiant condensé avec empreinte stable pour
  les noms longs, dans la limite GCP de 30 caractères) avec rôles
  `roles/storage.objectCreator` et `roles/storage.objectViewer` **bornés à
  ce bucket** (aucun rôle projet). Le remplacement des seuls objets sous
  `checkpoints/` et `source-gates/` est autorisé par condition IAM ; les lots
  bruts restent en écriture unique.

## Particularité GCS : un seul bucket, deux usages

Contrairement à AWS (S3 + DynamoDB), la variante GCS n'a pas de service
équivalent à DynamoDB pour les checkpoints. Le runtime
(`AS400_CHECKPOINT_BUCKET`) utilise le **même bucket** que le brut
(`AS400_RAW_BUCKET`), sous des préfixes distincts (`checkpoints/` et
`source-gates/`) — voir `docs/product/install-client.md` §1 et
`src/quadringent/storage_backend.py`. Ce module sort donc une seule
`raw_bucket_name`, à reporter sur les deux variables d'environnement.

## Variables principales

| Variable | Description | Défaut |
|---|---|---|
| `project_id` | Projet GCP | — obligatoire |
| `name` | Nom court du site | — obligatoire |
| `region` | Région GCP (ex. `europe-west1`) | — obligatoire |
| `bucket_versioning` | Versioning du bucket | `true` |
| `labels` | Étiquettes additionnelles | `{}` |

## Sorties

`raw_bucket_name`, `service_account_email`, `service_account_name`.

## Utilisation

```sh
terraform init
terraform apply -var project_id=mon-projet -var name=demo-int -var region=europe-west1
```

## Validation locale (sans backend, sans accès cloud)

```sh
terraform init -backend=false
terraform validate
terraform fmt -check
```
