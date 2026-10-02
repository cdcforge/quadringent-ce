# Installer Quadringent en mode par défaut

Ce guide décrit `quadringent install --cloud aws|gcp --target vm|cluster`
(chantier 6), l'installateur clé en main décrit dans
[le design produit fini](../plans/2026-09-23-produit-fini-design.md) §1. Il
complète le [guide expert](install-client.md), qui reste la voie de
référence pour un site qui gère lui-même sa chart Helm.

Pour les sauvegardes PostgreSQL, la restauration isolée et le retour de version,
voir [Sauvegarder, restaurer et revenir à une version précédente](backup-restore.md).

**Statut de la version 0.2.5** : candidat DEV en préparation. Les preuves
sur des versions précédentes couvrent des installations du control plane sur
GKE, EKS, VM AWS et VM GCP ; elles ne constituent pas une qualification du
nouveau lot. L'installation, la sauvegarde/restauration et le retour de
version doivent être vérifiés avec les artefacts exacts de la release sur
chaque cible annoncée.

La qualification du control plane est distincte de celle du parcours IBM i →
Snowflake. Un pod prêt, une API accessible ou une capture active ne prouvent
pas la relecture du miroir ni la continuité CDC. Aucun maximum de latence de
10 s ni qualification PROD n'est annoncé pour ce candidat. Les essais
utilisent des données synthétiques et les ressources temporaires sont
supprimées après qualification.

### Installation depuis les artefacts de release

Télécharger depuis **la même release** le wheel, le sdist
`quadringent-<version>.tar.gz` et `release-manifest.json`. Le wheel installe le
CLI ; le sdist contient la chart Helm et les modules Terraform nécessaires à
`install`. Les trois images référencées par le manifeste sont publiées
séparément, par digest immuable. Vérifier les empreintes de la release avant
installation. Aucun checkout Git n'est requis.

```sh
python3 -m venv /chemin/prive/quadringent-venv
/chemin/prive/quadringent-venv/bin/python -m pip install quadringent-0.2.5-py3-none-any.whl
tar -xzf quadringent-0.2.5.tar.gz -C /chemin/prive
/chemin/prive/quadringent-venv/bin/quadringent install \
  --cloud gcp --target cluster --region europe-west1 --project mon-projet \
  --name mon-site --release-manifest /chemin/prive/release-manifest.json \
  --assets-dir /chemin/prive/quadringent-0.2.5 --dry-run
```

Retirer `--dry-run` après examen du plan, avec les identifiants cloud et
`kubectl` pointant vers le cluster visé. Le CLI refuse l'installation si la
chart ou les modules manquent. Quand on travaille directement dans le dépôt
source, `--assets-dir` peut être omis.

### Stockage d'un cluster existant

Le mode `--target cluster` utilise un cluster déjà créé. Il configure
l'identité Quadringent ; il n'installe pas le pilote de volumes du cluster.
Avant l'installation, vérifier qu'une StorageClass peut provisionner les
volumes persistants PostgreSQL. La chart utilise celle par défaut, sauf si
`postgres.storage.storageClassName` désigne une autre classe dans les values
du site.

Sur EKS avec des volumes EBS, le pilote Amazon EBS CSI et ses permissions
doivent être prêts. Ces commandes vérifient un cluster existant sans le
modifier :

```sh
aws eks describe-addon --cluster-name mon-cluster \
  --addon-name aws-ebs-csi-driver \
  --query 'addon.{status:status,issues:health.issues}'
kubectl get storageclass
kubectl -n kube-system get deployment ebs-csi-controller
kubectl -n kube-system get daemonset ebs-csi-node
```

Pour le pilote géré comme add-on EKS, attendre son état `ACTIVE`, vérifier
les pods du contrôleur et du pilote sur les nœuds, puis vérifier que la
classe de stockage EBS utilise le provisionneur `ebs.csi.aws.com`.
Un add-on en `CREATE_FAILED` ou un PVC en `Pending` ne constitue pas une
installation fonctionnelle.

Si le compte de service du pilote est géré par l'add-on, créer uniquement
son rôle IAM avec `eksctl create iamserviceaccount --role-only` ; créer aussi
le compte Kubernetes avec eksctl peut provoquer un conflit de propriété
avec l'add-on. Voir les guides AWS sur
[le pilote EBS CSI](https://docs.aws.amazon.com/eks/latest/userguide/ebs-csi.html)
et [les rôles des comptes de service](https://docs.aws.amazon.com/eks/latest/eksctl/iamserviceaccounts.html).

## 1. Ce que fait `quadringent install`

1. Vérifie les prérequis (`terraform`, `helm`, `kubectl` dans le `PATH`, ainsi
   que `aws` et `session-manager-plugin` pour une VM AWS, ou `gcloud` pour GCP ;
   présence — jamais la valeur — d'identifiants cloud). `--check-ibmi <hôte>`
   ajoute une sonde TLS des ports IBM i déclarés (9471, 9476, 9475).
   `--project` est obligatoire pour `--cloud gcp` (aucune valeur de
   remplissage n'est plus posée pour le `project_id` Terraform) ;
   `--aws-profile` est optionnel pour `--cloud aws` (transmis en
   `AWS_PROFILE` à toutes les commandes terraform/aws/kubectl de
   l'installation). En installation AWS réelle, EKS exige l'ARN et l'URL du
   fournisseur OIDC déjà associé au cluster ; la VM exige le VPC et le
   sous-réseau existants. La VM GCP exige `--gcp-network` et
   `--gcp-subnetwork` ; `--gcp-zone` choisit une zone autre que `<region>-b`.
   L'installateur refuse ces champs manquants avant
   tout `terraform apply`.
2. Pour **chaque** module Terraform (socle, puis VM ou liaison
   d'identité) : copie le module du sdist ou du dépôt dans une copie privée de l'espace
   de travail du site (`~/.quadringent/<name>/terraform/<module>/`) — l'état
   Terraform ne vit donc jamais dans l'arbre du dépôt, il reste lié au site,
   pas au checkout du code — puis calcule un plan sauvegardé
   (`terraform plan -out=tfplan`), en affiche le résumé (ajouts/
   modifications/suppressions), **refuse toute destruction sans
   `--allow-destroy`** et **exige une confirmation interactive sauf
   `--yes`**, applique ce plan exact (jamais un `apply -auto-approve` en
   aveugle sur des variables non revues), puis relit ses sorties réelles
   (`terraform output -json`).
3. Le module `deploy/terraform/<cloud>/base` crée (ou réutilise avec
   `--existing-bucket NAME` / AWS `--existing-checkpoint-table NAME`) le
   bucket brut, l'état des checkpoints et une identité IAM/service bornée.
   Un stockage réutilisé bascule sur une source de données Terraform
   (`existing_bucket_name`/`existing_checkpoint_table_name`) plutôt que
   d'être créé et géré en cycle de vie — sa configuration (versioning,
   chiffrement, cycle de vie) reste sous la gouvernance de son propriétaire
   actuel. Ses sorties réelles (rôle IAM ou compte de service) alimentent
   directement l'étape suivante et les *values* Helm : **jamais un exemple
   fictif dans une installation réelle**, l'installateur échoue explicitement
   si l'identité attendue est absente des sorties Terraform. Le véritable
   `account_id` AWS du socle alimente aussi `site.awsAccountId`.
4. Selon `--target` :
   - `vm` sur AWS : applique `deploy/terraform/aws/vm` (VM avec k3s, aucune
     ingress publique par défaut) avec le profil d'instance du socle, attend
     la fin de cloud-init, récupère le kubeconfig par SSM et ouvre un tunnel
     SSM temporaire vers l'API k3s ; `vm` sur GCP crée une VM k3s privée,
     récupère le kubeconfig par SSH IAP et ouvre un tunnel SSH temporaire ;
   - `cluster` : applique `deploy/terraform/<cloud>/eks-addon` (AWS, IRSA)
     ou `gcp/gke-addon` (Workload Identity) sur un cluster **existant**, avec
     l'identité bornée au bucket produite par le socle, puis vérifie l'accès
     (`kubectl cluster-info`). Active le control plane (v1 + v2 + Postgres
     embarqué) avec cette identité réelle : `controlPlane.serviceAccount.
     roleArn` (AWS) ou `.gcpServiceAccount` (GCP).
5. Génère les values Helm avec l'identité réellement résolue, valide leur
   rendu (`helm template`) et installe la chart (`helm upgrade --install`)
   dans le répertoire de travail privé (`~/.quadringent/<name>` par défaut,
   jamais versionné).
6. Si le control plane v2 est actif : attend son rollout
   (`kubectl rollout status`), puis récupère le lien d'activation admin — un
   jeton réel, obtenu en appelant `POST /v2/setup/first-admin` **depuis
   l'intérieur** du pod control plane (`kubectl exec`, loopback sur le port
   v2, jamais un port-forward exposé pour cet appel unique) — et l'affiche
   avec l'URL locale et la commande de tunnel port-forward. Le compte est
   celui passé par `--admin-email` ; sans cette option, aucun lien n'est émis.
   La clé d'idempotence dérive du nom d'installation : relancer l'installation
   réaffiche le même lien. Jamais un lien fictif : si l'appel échoue, le
   message l'indique explicitement. Si le control plane n'est pas prêt à
   l'issue du rollout, l'installation échoue (code de sortie 1) après avoir
   affiché la cause ; elle est idempotente et se relance telle quelle.

Toutes les commandes externes passent par un exécuteur injectable : les
tests de la suite (`tests/test_installer_*.py`) s'exécutent entièrement hors
ligne, sans jamais toucher un compte cloud ou un cluster réel.

## 2. Utilisation

```sh
# Aperçu complet du plan (commandes et fichiers), sans rien exécuter :
quadringent install --cloud aws --target vm --region eu-west-3 --name demo-int --dry-run

# Installation réelle (nécessite des identifiants AWS valides dans l'environnement ;
# --yes évite la confirmation interactive de chaque plan Terraform, à réserver
# à un pipeline non interactif qui a déjà revu le plan) :
quadringent install --cloud aws --target vm --region eu-west-3 --name demo-int \
  --vpc-id vpc-0123456789abcdef0 --subnet-id subnet-0123456789abcdef0 \
  --release-manifest /chemin/prive/release-manifest.json --aws-profile demo-int \
  --vm-instance-type m7i-flex.large --image-pull-secret mon-registre-prive

# Cluster EKS existant :
quadringent install --cloud aws --target cluster --region eu-west-3 --name demo-int \
  --eks-oidc-provider-arn arn:aws:iam::000000000000:oidc-provider/oidc.eks.eu-west-3.amazonaws.com/id/EXAMPLE \
  --eks-oidc-provider-url oidc.eks.eu-west-3.amazonaws.com/id/EXAMPLE

# GCP, cluster GKE existant (--project obligatoire) :
quadringent install --cloud gcp --target cluster --region europe-west1 --name demo-int \
  --project mon-projet-gcp

# GCP, VM dédiée sans IP publique (réseau et sous-réseau préexistants) :
quadringent install --cloud gcp --target vm --region europe-west1 --name demo-int \
  --project mon-projet-gcp --gcp-network mon-vpc --gcp-subnetwork mon-subnet \
  --release-manifest /chemin/prive/release-manifest.json --dry-run

# VM AWS/GCP : maintenir le tunnel privé après l'installation ; la commande
# kubectl port-forward à lancer dans un second terminal est alors affichée.
quadringent vm-tunnel --name demo-int

quadringent status --name demo-int
quadringent uninstall --name demo-int
```

Après une release, télécharger `release-manifest.json` joint à la release
brouillon validée, puis le passer à `--release-manifest`. Ce fichier contient
les trois digests publiés et le dépôt OCI. La release communautaire cible
`ghcr.io/<owner>/quadringent-ce-runtime` ; utiliser l’URL exacte du
manifeste. `--image-repository` permet de
pointer vers un miroir privé qui conserve ces digests. Le gabarit
`deploy/release-manifest.example.json` contient des digests fictifs et ne
doit servir qu'aux essais hors ligne.

`--image-pull-secret` désigne un Secret Kubernetes **déjà présent dans le
namespace cible** si les images de la release sont privées. Le CLI ne crée
pas ce Secret et ne reçoit pas les identifiants du registre. Les images
publiques n'en ont pas besoin. La chart le lie au ServiceAccount de capture
créé par défaut : les lecteurs, chargeurs et Jobs créés plus tard par le
control plane peuvent ainsi tirer leurs images. Avec
`serviceAccount.create=false`, configurer ce Secret sur le ServiceAccount
fourni par le site avant de démarrer un pipeline. Sur une VM AWS,
`vm-tunnel` réutilise le profil
AWS enregistré lors de l'installation, ouvre le tunnel vers k3s et affiche la
commande `kubectl port-forward` pour accéder au cockpit. Le kubeconfig local
contient une clé cliente et est créé en mode `0600` dans le répertoire privé
du site ; protégez ce répertoire et supprimez-le avec le site.

### Exemple de sortie `--dry-run`

```
Installation Quadringent — cloud=aws target=vm region=eu-west-3 name=demo-int
Répertoire de travail : /home/operateur/.quadringent/demo-int

1. Copier le module Terraform du socle (bucket, checkpoints, identité) dans l'espace de travail privé du site
   copier deploy/terraform/aws/base -> /home/operateur/.quadringent/demo-int/terraform/base
2. Générer les variables Terraform du socle (bucket, checkpoints, identité)
   écrire /home/operateur/.quadringent/demo-int/terraform/base/site.auto.tfvars.json
3. Initialiser le module Terraform du socle (bucket, checkpoints, identité)
   $ terraform init -input=false  (cwd=/home/operateur/.quadringent/demo-int/terraform/base)
4. Calculer le plan Terraform du socle (bucket, checkpoints, identité)
   $ terraform plan -input=false -out=tfplan  (cwd=/home/operateur/.quadringent/demo-int/terraform/base)
5. Résumer le plan du socle (bucket, checkpoints, identité) et demander confirmation (refusé sans confirmation ni --yes ; toute destruction exige --allow-destroy)
   $ terraform show -json tfplan  (cwd=/home/operateur/.quadringent/demo-int/terraform/base)
6. Appliquer le plan Terraform du socle (bucket, checkpoints, identité)
   $ terraform apply -input=false tfplan  (cwd=/home/operateur/.quadringent/demo-int/terraform/base)
7. Lire les sorties Terraform du socle (bucket, checkpoints, identité) (identité réelle, gap 3)
   $ terraform output -json  (cwd=/home/operateur/.quadringent/demo-int/terraform/base)
8. Copier le module Terraform de la VM k3s dans l'espace de travail privé du site
   copier deploy/terraform/aws/vm -> /home/operateur/.quadringent/demo-int/terraform/vm
9. Générer les variables Terraform de la VM k3s
   écrire /home/operateur/.quadringent/demo-int/terraform/vm/site.auto.tfvars.json
10. Initialiser le module Terraform de la VM k3s
   $ terraform init -input=false  (cwd=/home/operateur/.quadringent/demo-int/terraform/vm)
11. Calculer le plan Terraform de la VM k3s
   $ terraform plan -input=false -out=tfplan  (cwd=/home/operateur/.quadringent/demo-int/terraform/vm)
12. Résumer le plan de la VM k3s et demander confirmation (refusé sans confirmation ni --yes ; toute destruction exige --allow-destroy)
   $ terraform show -json tfplan  (cwd=/home/operateur/.quadringent/demo-int/terraform/vm)
13. Appliquer le plan Terraform de la VM k3s
   $ terraform apply -input=false tfplan  (cwd=/home/operateur/.quadringent/demo-int/terraform/vm)
14. Lire les sorties Terraform de la VM k3s (identité réelle, gap 3)
   $ terraform output -json  (cwd=/home/operateur/.quadringent/demo-int/terraform/vm)
15. Récupérer le kubeconfig k3s via SSM (aws ssm send-command)
   $ aws ssm send-command --document-name AWS-RunShellScript
16. Générer les values Helm (stockage, identité résolue, control plane)
   écrire /home/operateur/.quadringent/demo-int/chart-values.generated.yaml
17. Valider le rendu de la chart (helm template)
   $ helm template demo-int chart --namespace quadringent -f /home/operateur/.quadringent/demo-int/chart-values.generated.yaml
18. Installer la chart Quadringent
   $ helm upgrade --install demo-int chart --namespace quadringent --create-namespace -f /home/operateur/.quadringent/demo-int/chart-values.generated.yaml
19. Attendre le déploiement du control plane (kubectl rollout status)
   $ kubectl -n quadringent rollout status deployment/demo-int-quadringent-control-plane --timeout=180s
20. Récupérer le jeton d'activation du premier admin (kubectl exec, POST /v2/setup/first-admin depuis l'intérieur du pod)
   $ kubectl -n quadringent exec deployment/demo-int-quadringent-control-plane -c control-plane-v2 -- python -c <POST http://127.0.0.1:8845/v2/setup/first-admin>

(--dry-run : aucune commande exécutée, aucun fichier écrit)
```

### Exemple `--dry-run` — GCP, cluster existant, bucket GCS réutilisé

```
quadringent install --cloud gcp --target cluster --region europe-west1 --name demo-int \
  --project example-gcp-project --existing-bucket preexisting-gcs-bucket --dry-run
```

```
Installation Quadringent — cloud=gcp target=cluster region=europe-west1 name=demo-int
Répertoire de travail : /home/operateur/.quadringent/demo-int

1. Copier le module Terraform du socle (bucket, checkpoints, identité) dans l'espace de travail privé du site
   copier deploy/terraform/gcp/base -> /home/operateur/.quadringent/demo-int/terraform/base
2. Générer les variables Terraform du socle (bucket, checkpoints, identité) — bucket 'preexisting-gcs-bucket' réutilisé, aucune création
   écrire /home/operateur/.quadringent/demo-int/terraform/base/site.auto.tfvars.json
3. Initialiser le module Terraform du socle (bucket, checkpoints, identité)
   $ terraform init -input=false  (cwd=/home/operateur/.quadringent/demo-int/terraform/base)
4. Calculer le plan Terraform du socle (bucket, checkpoints, identité)
   $ terraform plan -input=false -out=tfplan  (cwd=/home/operateur/.quadringent/demo-int/terraform/base)
5. Résumer le plan du socle (bucket, checkpoints, identité) et demander confirmation (refusé sans confirmation ni --yes ; toute destruction exige --allow-destroy)
   $ terraform show -json tfplan  (cwd=/home/operateur/.quadringent/demo-int/terraform/base)
6. Appliquer le plan Terraform du socle (bucket, checkpoints, identité) — bucket 'preexisting-gcs-bucket' réutilisé, aucune création
   $ terraform apply -input=false tfplan  (cwd=/home/operateur/.quadringent/demo-int/terraform/base)
7. Lire les sorties Terraform du socle (bucket, checkpoints, identité) (identité réelle, gap 3)
   $ terraform output -json  (cwd=/home/operateur/.quadringent/demo-int/terraform/base)
8. Copier le module Terraform de la liaison d'identité (IRSA/Workload Identity) dans l'espace de travail privé du site
   copier deploy/terraform/gcp/gke-addon -> /home/operateur/.quadringent/demo-int/terraform/addon
9. Générer les variables Terraform de la liaison d'identité (IRSA/Workload Identity)
   écrire /home/operateur/.quadringent/demo-int/terraform/addon/site.auto.tfvars.json
10. Initialiser le module Terraform de la liaison d'identité (IRSA/Workload Identity)
   $ terraform init -input=false  (cwd=/home/operateur/.quadringent/demo-int/terraform/addon)
11. Calculer le plan Terraform de la liaison d'identité (IRSA/Workload Identity)
   $ terraform plan -input=false -out=tfplan  (cwd=/home/operateur/.quadringent/demo-int/terraform/addon)
12. Résumer le plan de la liaison d'identité (IRSA/Workload Identity) et demander confirmation (refusé sans confirmation ni --yes ; toute destruction exige --allow-destroy)
   $ terraform show -json tfplan  (cwd=/home/operateur/.quadringent/demo-int/terraform/addon)
13. Appliquer le plan Terraform de la liaison d'identité (IRSA/Workload Identity)
   $ terraform apply -input=false tfplan  (cwd=/home/operateur/.quadringent/demo-int/terraform/addon)
14. Lire les sorties Terraform de la liaison d'identité (IRSA/Workload Identity) (identité réelle, gap 3)
   $ terraform output -json  (cwd=/home/operateur/.quadringent/demo-int/terraform/addon)
15. Vérifier l'accès au cluster existant (kubeconfig déjà configuré par le site)
   $ kubectl cluster-info
16. Générer les values Helm (stockage, identité résolue, control plane)
   écrire /home/operateur/.quadringent/demo-int/chart-values.generated.yaml
17. Valider le rendu de la chart (helm template)
   $ helm template demo-int chart --namespace quadringent -f /home/operateur/.quadringent/demo-int/chart-values.generated.yaml
18. Installer la chart Quadringent
   $ helm upgrade --install demo-int chart --namespace quadringent --create-namespace -f /home/operateur/.quadringent/demo-int/chart-values.generated.yaml
19. Attendre le déploiement du control plane (kubectl rollout status)
   $ kubectl -n quadringent rollout status deployment/demo-int-quadringent-control-plane --timeout=180s
20. Récupérer le jeton d'activation du premier admin (kubectl exec, POST /v2/setup/first-admin depuis l'intérieur du pod)
   $ kubectl -n quadringent exec deployment/demo-int-quadringent-control-plane -c control-plane-v2 -- python -c <POST http://127.0.0.1:8845/v2/setup/first-admin>

(--dry-run : aucune commande exécutée, aucun fichier écrit)
```

Le bucket réutilisé n'est ni créé ni reconfiguré (versioning, chiffrement) :
`deploy/terraform/gcp/base` bascule sur `data "google_storage_bucket"`
plutôt que sur `resource "google_storage_bucket"` — voir gap (b) ci-dessous.

`--cloud gcp --target cluster` active désormais `controlPlane.enabled` (v1 +
v2 + Postgres embarqué, comme AWS) avec une identité **Workload Identity** :
`deploy/terraform/gcp/gke-addon` lie le ServiceAccount Kubernetes
`quadringent-control-plane` au compte de service GCP borné au bucket
(produit par `gcp/base`, `roles/iam.workloadIdentityUser`), et son adresse
e-mail est déclarée dans `controlPlane.serviceAccount.gcpServiceAccount`
(annotation `iam.gke.io/gcp-service-account` posée sur le ServiceAccount par
la chart). Ce texte de plan ne change pas (l'identité n'apparaît que dans
les *values* générées, pas dans la liste des étapes) : voir
`tests/test_installer_plan.py::ChartValuesTests::test_gcp_cluster_enables_control_plane_v2_with_workload_identity`.
Lors d'une installation réelle, les sorties Terraform du socle et de la
liaison d'identité alimentent automatiquement les *values* Helm. L'adresse
de documentation (`<nom>-quadringent-runtime@example-project.iam.gserviceaccount.com`)
n'apparaît que dans l'aperçu `--dry-run`. L'installateur échoue si l'identité
réelle manque dans les sorties. La liaison Workload Identity et le déploiement
du control plane ont été vérifiés sur un cluster GKE DEV le 28 septembre 2026.

`--cloud gcp --target vm` active aussi le control plane v2. Le module
`gcp/vm` attache à une VM **dédiée** le compte de service borné au bucket
produit par `gcp/base`. Les pods k3s y obtiennent les identifiants via le
serveur de métadonnées Compute Engine ; la chart ne pose donc pas
l'annotation Workload Identity propre à GKE (`gcpIdentityMode: vm-metadata`).
Tous les pods de cette VM peuvent atteindre l'identité du nœud : ne pas y
héberger de charges non fiables. L'installation, l'accès privé, l'API, l'UI,
PostgreSQL et l'accès GCS ont été vérifiés sur une VM DEV temporaire ; la
recopie IBM i et la restauration sur cette cible restent à qualifier.

### Exemple `--dry-run` — AWS, cluster existant

```
quadringent install --cloud aws --target cluster --region eu-west-3 --name demo-int --dry-run
```

```
Installation Quadringent — cloud=aws target=cluster region=eu-west-3 name=demo-int
Répertoire de travail : /home/operateur/.quadringent/demo-int

1. Copier le module Terraform du socle (bucket, checkpoints, identité) dans l'espace de travail privé du site
   copier deploy/terraform/aws/base -> /home/operateur/.quadringent/demo-int/terraform/base
2. Générer les variables Terraform du socle (bucket, checkpoints, identité)
   écrire /home/operateur/.quadringent/demo-int/terraform/base/site.auto.tfvars.json
3. Initialiser le module Terraform du socle (bucket, checkpoints, identité)
   $ terraform init -input=false  (cwd=/home/operateur/.quadringent/demo-int/terraform/base)
4. Calculer le plan Terraform du socle (bucket, checkpoints, identité)
   $ terraform plan -input=false -out=tfplan  (cwd=/home/operateur/.quadringent/demo-int/terraform/base)
5. Résumer le plan du socle (bucket, checkpoints, identité) et demander confirmation (refusé sans confirmation ni --yes ; toute destruction exige --allow-destroy)
   $ terraform show -json tfplan  (cwd=/home/operateur/.quadringent/demo-int/terraform/base)
6. Appliquer le plan Terraform du socle (bucket, checkpoints, identité)
   $ terraform apply -input=false tfplan  (cwd=/home/operateur/.quadringent/demo-int/terraform/base)
7. Lire les sorties Terraform du socle (bucket, checkpoints, identité) (identité réelle, gap 3)
   $ terraform output -json  (cwd=/home/operateur/.quadringent/demo-int/terraform/base)
8. Copier le module Terraform de la liaison d'identité (IRSA/Workload Identity) dans l'espace de travail privé du site
   copier deploy/terraform/aws/eks-addon -> /home/operateur/.quadringent/demo-int/terraform/addon
9. Générer les variables Terraform de la liaison d'identité (IRSA/Workload Identity)
   écrire /home/operateur/.quadringent/demo-int/terraform/addon/site.auto.tfvars.json
10. Initialiser le module Terraform de la liaison d'identité (IRSA/Workload Identity)
   $ terraform init -input=false  (cwd=/home/operateur/.quadringent/demo-int/terraform/addon)
11. Calculer le plan Terraform de la liaison d'identité (IRSA/Workload Identity)
   $ terraform plan -input=false -out=tfplan  (cwd=/home/operateur/.quadringent/demo-int/terraform/addon)
12. Résumer le plan de la liaison d'identité (IRSA/Workload Identity) et demander confirmation (refusé sans confirmation ni --yes ; toute destruction exige --allow-destroy)
   $ terraform show -json tfplan  (cwd=/home/operateur/.quadringent/demo-int/terraform/addon)
13. Appliquer le plan Terraform de la liaison d'identité (IRSA/Workload Identity)
   $ terraform apply -input=false tfplan  (cwd=/home/operateur/.quadringent/demo-int/terraform/addon)
14. Lire les sorties Terraform de la liaison d'identité (IRSA/Workload Identity) (identité réelle, gap 3)
   $ terraform output -json  (cwd=/home/operateur/.quadringent/demo-int/terraform/addon)
15. Vérifier l'accès au cluster existant (kubeconfig déjà configuré par le site)
   $ kubectl cluster-info
16. Générer les values Helm (stockage, identité résolue, control plane)
   écrire /home/operateur/.quadringent/demo-int/chart-values.generated.yaml
17. Valider le rendu de la chart (helm template)
   $ helm template demo-int chart --namespace quadringent -f /home/operateur/.quadringent/demo-int/chart-values.generated.yaml
18. Installer la chart Quadringent
   $ helm upgrade --install demo-int chart --namespace quadringent --create-namespace -f /home/operateur/.quadringent/demo-int/chart-values.generated.yaml
19. Attendre le déploiement du control plane (kubectl rollout status)
   $ kubectl -n quadringent rollout status deployment/demo-int-quadringent-control-plane --timeout=180s
20. Récupérer le jeton d'activation du premier admin (kubectl exec, POST /v2/setup/first-admin depuis l'intérieur du pod)
   $ kubectl -n quadringent exec deployment/demo-int-quadringent-control-plane -c control-plane-v2 -- python -c <POST http://127.0.0.1:8845/v2/setup/first-admin>

(--dry-run : aucune commande exécutée, aucun fichier écrit)
```

Contrairement au mode VM, aucune ressource de calcul n'est créée (étape 4-6 :
seule l'identité IRSA est liée au cluster existant, `aws/eks-addon`). Cette
installation active `controlPlane.v2.enabled` (et le Postgres embarqué de la
chart, actif par défaut) dans les values générées : à l'étape 10, le message
final tente de récupérer le vrai jeton d'activation admin par
`kubectl exec` — voir « Accès à l'UI après installation » ci-dessous.

### Accès à l'UI après installation

L'installateur n'expose jamais l'UI publiquement. Le message final indique le
tunnel à ouvrir :

- AWS + VM : `aws ssm start-session ... AWS-StartPortForwardingSession`
  (voir `deploy/terraform/aws/vm/README.md`) ;
- GCP + VM : `quadringent vm-tunnel --name <site>` ouvre SSH sur IAP (port 22),
  puis le transfert local de k3s ; lancer la commande `kubectl port-forward`
  affichée dans un second terminal (voir `deploy/terraform/gcp/vm/README.md`) ;
- Cluster existant : `kubectl port-forward`, comme dans le
  [guide expert](install-client.md).

Un seul tunnel suffit, y compris quand le control plane v2 est actif :
le conteneur v1 relaie `/v2/*` et `/mcp` vers le conteneur v2 du même Pod
(voir `docs/api-v2.md`, section « Accès à travers le seul port v1 ») —
l'UI et le lien d'activation `http://127.0.0.1:8844/#/wizard/activate?token=…`
fonctionnent donc sans ouvrir de second tunnel vers le port v2.

Le cookie de session posé par `POST /v2/auth/login` porte l'attribut
`Secure` (toujours, même en local — voir `docs/api-v2.md`, section
« Identité ») : Chrome et Firefox l'acceptent sur `http://localhost` et
`http://127.0.0.1` (exception documentée du spec cookies pour ces deux
origines de boucle locale), mais Safari ne le fait pas de façon fiable.
Utilisez `http://localhost:8844` (pas `127.0.0.1`, certains navigateurs
sont plus permissifs sur ce nom d'hôte précis) avec un navigateur à base
de Chromium ou Firefox pour vous connecter à l'UI après le tunnel.

## 3. Ce que chaque cloud crée (coûts qualitatifs)

| Ressource | AWS | GCP |
|---|---|---|
| Stockage brut | Bucket S3 (volume et requêtes facturés ; configurer une alerte de facturation séparée) | Bucket GCS (volume et opérations facturés selon le site) |
| État des checkpoints | Table DynamoDB `PAY_PER_REQUEST` (coût proportionnel aux écritures, nul à l'arrêt) | Même bucket GCS (pas de coût séparé) |
| Identité | Rôle/politique IAM ou compte de service (gratuit) | idem |
| VM (`--target vm`) | Instance EC2 x86_64 `t3.medium` par défaut (type configurable) + volume racine EBS chiffré portant l'état k3s | Instance Compute Engine `e2-medium` par défaut + disque de démarrage de 40 Go portant k3s ; aucune IP publique par défaut |
| Cluster existant (`--target cluster`) | Le module Terraform ne crée pas de cluster ; les pods installés utilisent sa capacité et peuvent déclencher l'autoscaling | idem |

Aucun coût n'est engagé avant `terraform apply` : `--dry-run` et
`terraform plan` restent gratuits et sans effet.

## 4. Désinstallation

```sh
quadringent uninstall --name demo-int
```

Retire uniquement la release Helm. Pour une VM, la commande retrouve
l'instance du site et passe par SSM (AWS) ou SSH IAP (GCP) ; si l'état du site manque,
elle refuse de toucher au cluster courant. **Les ressources Terraform (bucket, table
de checkpoints, identités, VM) ne sont pas supprimées** : elles peuvent
porter des données de preuve à conserver. Pour les retirer, exécuter
`terraform destroy` dans les copies privées de modules du site
(`~/.quadringent/<name>/terraform/vm`, puis `.../base`), en s'assurant d'abord qu'aucun lecteur
n'est actif et que les données du bucket ne sont plus nécessaires — décision
séparée du propriétaire des données, comme pour le
[guide expert](install-client.md#désinstallation-et-limites).

## 5. Limites connues (chart actuelle)

Le chantier 6 a mis en évidence des écarts entre l'installateur « mode par
défaut » décrit dans le design produit fini et ce que `chart/` peut rendre
aujourd'hui. Ils sont documentés ici précisément plutôt que masqués par un
correctif large et risqué :

- ~~**Déclaration de site obligatoire au rendu, même sans connexion.**~~
  Corrigé : `site.connectionDeclared` (défaut `true`, comportement
  historique inchangé — voir le test doré
  `tests/test_chart_no_connection.py::GoldenRenderUnchangedTests`) bascule à
  `false` les gardes de `chart/templates/configmap-site.yaml` et
  `configmap-tuning.yaml` portant sur les champs de connexion IBM i/Snowflake
  (`site.ibmiHost`, `site.proofTable`, `site.snowflakeAccount`, `ibmi.host`,
  `as400.tlsCaFile`, etc.) : ils restent alors vides, jamais fictifs, et ne
  sont plus exigés. `chart/templates/deployment.yaml` (le lecteur) ne se rend
  plus du tout tant qu'aucune connexion n'est déclarée (`replicaCount` doit
  rester à 0). Les champs déjà connus de l'installateur à ce stade — identité
  du site, stockage (`site.id`, `site.rawBucket`, `site.checkpointTable`,
  `aws.region`...) — restent exigés et publiés normalement : seule
  l'information de connexion IBM i/Snowflake, qui relève de l'assistant en
  trois écrans (design produit fini §2, toujours hors périmètre de ce
  chantier), est omise. L'installateur du mode par défaut ne pose donc plus
  aucune valeur de remplissage fictive (`PENDING...`,
  `ibmi-pending.example.internal`) — seuls les noms de Secrets cibles
  (`quadringent-pending-ibmi`, `quadringent-pending-ca`, pas encore créés)
  restent posés, car ce sont des noms d'objets Kubernetes choisis par
  l'installateur pour l'assistant à venir, pas des données de connexion.
- ~~**`controlPlane.serviceAccount.roleArn` est un ARN IAM AWS, sans
  équivalent GCP.**~~ Corrigé : `chart/templates/control-plane.yaml` exige
  désormais `controlPlane.serviceAccount.roleArn` (IRSA) uniquement quand
  `storage.backend=aws`, et `controlPlane.serviceAccount.gcpServiceAccount`
  (Workload Identity, annotation `iam.gke.io/gcp-service-account`) quand
  `storage.backend=gcs` — les deux champs sont mutuellement exclusifs.
- ~~**`controlPlane.enabled: false` inconditionnel pour `--cloud gcp`, faute
  de compte de service GCP.**~~ Corrigé pour `--target cluster` :
  `deploy/terraform/gcp/gke-addon` lie le ServiceAccount Kubernetes du
  control plane au compte de service borné au bucket produit par `gcp/base`
  (`roles/iam.workloadIdentityUser`) et expose son adresse en sortie
  (`service_account_email`) ; `quadringent install --cloud gcp --target
  cluster` active `controlPlane.enabled`/`controlPlane.v2.enabled` (v1 + v2 +
  Postgres embarqué, comme AWS) avec cette identité. Corrigé également côté
  orchestration : une installation réelle (hors `--dry-run`) relit désormais
  les sorties Terraform du socle et de la liaison d'identité
  (`terraform output -json`) et publie l'adresse **réelle** dans
  `controlPlane.serviceAccount.gcpServiceAccount` — plus jamais un exemple
  fictif dans une installation réelle ; l'installateur échoue explicitement
  si l'identité attendue est absente des sorties. Seul l'aperçu `--dry-run`
  (rien n'est encore appliqué) affiche une adresse de documentation
  (`<nom>-quadringent-runtime@example-project.iam.gserviceaccount.com`) —
  voir « Exemple `--dry-run` — GCP » ci-dessus.
  `--target vm` utilise désormais la GSA attachée à la VM dédiée et son
  serveur de métadonnées Compute Engine ; il ne pose aucune annotation GKE.
  Le lecteur
  (`serviceAccount` racine de la chart, distinct de `controlPlane.
  serviceAccount`) n'est pas concerné par ce changement : `deployment.yaml`
  ne se rend de toute façon pas tant qu'aucune connexion n'est déclarée (gap
  (a)) — sa propre identité Workload Identity, si elle doit être distincte de
  celle du control plane, reste à câbler quand ce Deployment redeviendra
  pertinent.
- ~~**`site.awsAccountId` et `aws.region` sont exigés inconditionnellement**~~
  Corrigé, chart et runtime : les gardes de `configmap-site.yaml` n'exigent et
  ne valident ces deux champs que lorsque `storage.backend=aws` ; ils doivent
  rester vides quand `storage.backend=gcs` (refus explicite sinon).
  L'installateur GCP ne pose donc plus de valeurs de repli sans rapport avec
  le projet réel (`000000000000`, `eu-west-3`). Le runtime
  (`src/quadringent/site_config.py`, nouveau champ `SiteConfig.storage_backend`
  lu depuis `QUADRINGENT_STORAGE_BACKEND`, absente = `aws` pour compatibilité
  ascendante) applique la même règle : `QUADRINGENT_AWS_ACCOUNT_ID`/
  `QUADRINGENT_AWS_REGION` ne sont exigées/validées que pour
  `storage_backend=aws`. Les consommateurs AWS-only refusent proprement
  ailleurs : `s3_snowpipe_notification.py` lève une erreur explicite
  (« storage_backend=aws » requis) plutôt que de construire un ARN vide de
  sens ; `infrastructure_costs.py` retombait déjà sur un statut
  `unavailable` sans jamais planter.
- **`controlPlane.launch.enabled` reste à `false`.** L'activer exige
  `jobTemplate` et `fleetCatalog` réels (modèle de Job et catalogue IBM i du
  site), qu'un installateur ne peut pas produire sans connexion établie —
  contrairement à ce qu'évoquait la consigne initiale du chantier
  (« opinionated : launch enabled »). Ce choix diffère donc volontairement
  de cette consigne, documenté ici plutôt qu'appliqué par une valeur
  inventée qui casserait le contrat de preuve de la chart.
- ~~**Pas de composant Postgres dans la chart.**~~ Corrigé :
  `chart/templates/postgres.yaml` rend un StatefulSet Postgres à une réplique
  (image officielle épinglée par digest, PVC, Service headless, Secret
  d'identifiants `helm.sh/resource-policy: keep` généré une fois via
  `lookup`, NetworkPolicy restreignant l'accès au control plane, CronJob de
  sauvegarde logique optionnel), actif par défaut (`postgres.enabled: true`)
  ou remplacé par un Postgres géré externe (`externalDatabase.url`/
  `existingSecret`) en mode expert. `postgres.enabled` n'est jamais
  surchargé par l'installateur : le défaut de la chart s'applique tel quel,
  Postgres embarqué actif sur toute installation avec control plane.
- ~~**`controlPlane.v2` reste à câbler dans le Deployment de la chart.**~~
  Corrigé côté chart (`controlPlane.v2.enabled`, désactivé par défaut,
  ajoute un second conteneur `control-plane-v2` au même Pod que v1
  — conservée intacte —, probes `/v2/healthz`, secrets Fernet/pepper
  générés une fois et conservés entre upgrades, DSN composé au démarrage
  depuis le Postgres embarqué) **et côté installateur** :
  `quadringent install` pose désormais `controlPlane.v2.enabled: true` dans
  les values générées pour AWS et GCP, VM ou cluster (voir
  `build_chart_values` ; avec Postgres embarqué actif par défaut ci-dessus,
  c'est le seul couple de valeurs qui
  rend `fetch_first_admin_activation_token` opérant — sans lui, aucun compte
  admin ne peut jamais s'activer).

Ces limites n'empêchent pas `quadringent install` de créer l'infrastructure
et un control plane actif sur AWS ou GCP (accessible, sans source
connectée) : elles bornent ce que « installer » signifie aujourd'hui, en
toute franchise.
