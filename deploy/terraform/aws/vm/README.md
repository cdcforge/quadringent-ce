# Module `aws/vm`

Crée une VM EC2 avec k3s (cloud-init) pour le mode par défaut
`--target vm`. Ne crée ni VPC ni sous-réseau : `vpc_id` et `subnet_id`
doivent référencer un réseau existant, idéalement privé.

## Ressources créées

- Instance EC2 (Amazon Linux 2023 standard, noyau 6.1, résolue via le
  paramètre public AWS afin de disposer de l'agent SSM ; architecture `x86_64` par défaut
  ou Graviton `arm64` si l'instance et les images le permettent), k3s installé par cloud-init (`--disable=traefik`, l'ingress
  reste piloté par la chart Quadringent si nécessaire), `IMDSv2` obligatoire.
- Security group **sans ingress publique par défaut** : `allowed_ssh_cidrs`
  et `allowed_ui_cidrs` sont vides tant que le site ne les renseigne pas
  explicitement. Sortie complète autorisée (registre d'images, Snowflake,
  IBM i, SSM).
- Volume racine EBS chiffré de 40 Go par défaut, portant l'état k3s et les
  PVC local-path du control plane.
- Profil d'instance repris de `aws/base` (`instance_profile_name`) ; le
  module attache `AmazonSSMManagedInstanceCore` au rôle de ce profil.

Une nouvelle AMI publiée peut proposer le remplacement de la VM au prochain
plan Terraform. Examiner ce plan et sauvegarder l'état k3s avant toute
autorisation de destruction ; l'installateur refuse celle-ci par défaut.

## Accès à l'UI : SSM port-forward par défaut

Sans `allowed_ui_cidrs`, l'UI Quadringent n'est pas exposée. L'accès passe
par un tunnel SSM vers l'API k3s, puis par `kubectl port-forward` vers le
service du control plane :

```sh
quadringent vm-tunnel --name <site>
# Dans un second terminal, lancer la commande kubectl affichée.
```

Puis ouvrir `http://127.0.0.1:8844`. Cela exige que l'instance ait une
route sortante vers les points de terminaison SSM (VPC endpoints ou NAT, ou
une route Internet adaptée au réseau choisi). Le kubeconfig client local est
écrit en mode `0600` dans le répertoire privé du site.

`allowed_ssh_cidrs` et `allowed_ui_cidrs` restent disponibles pour les sites
qui préfèrent un accès direct restreint (bastion, VPN) plutôt que SSM.

## Récupération du kubeconfig

Le CLI `quadringent install` récupère le kubeconfig k3s
(`/etc/rancher/k3s/k3s.yaml`) via SSM (`aws ssm send-command` /
`get-command-invocation`) plutôt que par SSH, pour ne dépendre d'aucune
ingress publique. Voir `docs/product/install-default.md`.

## Variables principales

| Variable | Description | Défaut |
|---|---|---|
| `name` | Nom court du site | — obligatoire |
| `vpc_id`, `subnet_id` | Réseau existant | — obligatoires |
| `instance_profile_name` | Sortie `aws/base` | — obligatoire |
| `instance_role_name` | Sortie `aws/base` ; attachement SSM | — obligatoire |
| `architecture` | `arm64` ou `x86_64` | `x86_64` |
| `instance_type` | Type d'instance (cohérent avec l'architecture) | `t3.medium` |
| `root_volume_size_gb` | Taille du volume d'état | `40` |
| `allowed_ssh_cidrs`, `allowed_ui_cidrs` | CIDR d'accès direct optionnels | `[]` |
| `ssh_key_name` | Paire de clés EC2 optionnelle | `""` |
| `k3s_channel` | Canal de version k3s | `stable` |

## Validation locale (sans backend, sans accès cloud)

```sh
terraform init -backend=false
terraform validate
terraform fmt -check
```
