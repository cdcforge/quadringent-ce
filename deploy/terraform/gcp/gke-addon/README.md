# Module `gcp/gke-addon`

Lie, par Workload Identity, le `ServiceAccount` Kubernetes du control plane
Quadringent au compte de service borné créé par `gcp/base`, pour un cluster
GKE **existant** avec Workload Identity déjà activé. Ce module ne crée ni ne
modifie le cluster : c'est le cas `--target cluster` de l'installateur.

## Ressource créée

- `google_service_account_iam_member` : accorde `roles/iam.workloadIdentityUser`
  sur le compte de service, au membre
  `serviceAccount:<project>.svc.id.goog[<namespace>/<service_account>]`.

## Prérequis côté site

- Cluster GKE avec Workload Identity activé (`workloadIdentityConfig`).
- Le module `gcp/base` déjà appliqué, pour obtenir `service_account_email`.

## Variables principales

| Variable | Description | Défaut |
|---|---|---|
| `project_id` | Projet GCP | — obligatoire |
| `name` | Nom court du site | — obligatoire |
| `service_account_email` | Sortie `gcp/base` | — obligatoire |
| `namespace` | Namespace du ServiceAccount Kubernetes | `quadringent` |
| `kubernetes_service_account_name` | Nom du ServiceAccount Kubernetes | `quadringent-control-plane` |

## Sortie

`kubernetes_service_account_annotation` : à reporter dans
`controlPlane.serviceAccount.annotations` (values de la chart), qui fusionne
cette annotation avec celle gérée pour IRSA côté AWS.

## Étape complémentaire (hors Terraform)

Après application, annoter le ServiceAccount Kubernetes lui-même (la chart
Quadringent le fait si `controlPlane.serviceAccount.annotations` est
renseigné) :

```sh
kubectl annotate serviceaccount quadringent-control-plane \
  --namespace quadringent \
  iam.gke.io/gcp-service-account=<service_account_email>
```

## Validation locale (sans backend, sans accès cloud)

```sh
terraform init -backend=false
terraform validate
terraform fmt -check
```
