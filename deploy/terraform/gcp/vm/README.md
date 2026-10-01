# Module `gcp/vm`

Crée une VM Compute Engine avec k3s (startup-script) pour le mode par défaut
`--target vm`. Ne crée ni réseau VPC ni sous-réseau : `network` et
`subnetwork` doivent référencer un réseau existant, idéalement privé.

## Ressources créées

- Instance Compute Engine (Ubuntu 24.04 LTS), k3s installé par
  startup-script (`--disable=traefik`), Shielded VM (secure boot, vTPM,
  integrity monitoring) activé.
- Règle de pare-feu SSH ciblant uniquement le tag de cette VM et restreinte à la plage fixe d'IAP TCP forwarding
  (`35.235.240.0/20`) plus les plages additionnelles explicitement
  déclarées (`allowed_ssh_ranges`, vide par défaut).
- **Aucune IP publique par défaut** (`assign_public_ip = false`).
- Compte de service repris de `gcp/base` (`service_account_email`), avec le
  scope `cloud-platform` (le périmètre effectif reste borné par les rôles
  IAM du compte de service, pas par ce scope).

## Accès à l'UI : IAP TCP forwarding par défaut

### Accès sortant requis pour l'installation

Le startup-script télécharge k3s et les conteneurs depuis Internet. Avec
`assign_public_ip = false`, le sous-réseau existant doit fournir cet accès
sortant, par exemple via Cloud NAT. Ce module ne crée pas de NAT ; le tunnel
IAP sert à l'administration et ne fournit pas cet accès sortant.

Sur une VM de test dédiée sans NAT, on peut explicitement demander une IP
publique éphémère avant `quadringent install` :

```sh
export TF_VAR_assign_public_ip=true
# Dimensionnement facultatif de la VM et du disque :
export TF_VAR_machine_type=e2-standard-2
export TF_VAR_boot_disk_size_gb=20
```

Terraform hérite de ces variables lors de l'exécution réelle. Le dry-run
de l'installateur ne calcule pas leurs effets : revoir le plan Terraform
avant confirmation. L'adresse publique n'ouvre aucun port
d'administration supplémentaire ; la règle SSH reste restreinte à IAP.
L'adresse, la VM et son disque sont facturables jusqu'à leur destruction.

Sans IP publique, l'accès à l'API k3s passe par SSH sur IAP au port 22, puis
par transfert local. L'UI est ensuite exposée uniquement via
`kubectl port-forward` dans un second terminal. Aucune règle 6443 ou 8844
n'est nécessaire :

```sh
quadringent vm-tunnel --name <site>
# Dans un second terminal, lancer la commande kubectl port-forward affichée.
```

Puis ouvrir `http://localhost:8844`. Le compte appelant doit avoir le rôle
`roles/iap.tunnelResourceAccessor` sur l'instance.

## Récupération du kubeconfig

Le CLI `quadringent install` récupère le kubeconfig k3s
(`/etc/rancher/k3s/k3s.yaml`) via `gcloud compute ssh --tunnel-through-iap`
plutôt que par SSH direct, pour ne dépendre d'aucune ingress publique. Voir
`docs/product/install-default.md`.

La VM reçoit le compte de service `gcp/base` limité au bucket du site ; les
pods k3s utilisent son serveur de métadonnées. Cette VM doit être dédiée à
Quadringent : toute charge hébergée sur le même nœud peut accéder à cette
identité. Sur une VM DEV temporaire, l'installation depuis wheel et sdist,
le tunnel SSH IAP, la santé API/UI, PostgreSQL et l'accès GCS avec cette
identité ont été vérifiés. La recopie IBM i et la restauration ne l'ont pas été.

## Variables principales

| Variable | Description | Défaut |
|---|---|---|
| `project_id`, `region`, `zone` | Localisation | — obligatoires |
| `name` | Nom court du site | — obligatoire |
| `network`, `subnetwork` | Réseau existant | — obligatoires |
| `service_account_email` | Sortie `gcp/base` | — obligatoire |
| `machine_type` | Type de machine | `e2-medium` |
| `boot_disk_size_gb` | Taille du disque d'état | `40` |
| `assign_public_ip` | IP publique | `false` |
| `allowed_ssh_ranges` | Plages SSH additionnelles | `[]` |
| `k3s_channel` | Canal de version k3s | `stable` |

## Validation locale (sans backend, sans accès cloud)

```sh
terraform init -backend=false
terraform validate
terraform fmt -check
```
