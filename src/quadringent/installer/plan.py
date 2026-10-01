"""Construction du plan d'installation : entrées, fichiers générés, étapes.

Le plan est une structure de données pure (aucun effet de bord) : le CLI
(``cli.py``) l'affiche telle quelle en ``--dry-run``, ou l'exécute pas à pas
via un :class:`~quadringent.installer.runner.CommandRunner`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import shlex
from typing import TYPE_CHECKING, Mapping

from .manifest import ReleaseManifest

if TYPE_CHECKING:
    from .runner import CommandRunner

CLOUDS = ("aws", "gcp")
TARGETS = ("vm", "cluster")

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}\Z")
_AWS_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-[0-9]$")

REQUIRED_TOOLS = ("terraform", "helm", "kubectl")

# Compte AWS de remplissage pour le plan --dry-run seulement. L'installation
# réelle transmet la sortie account_id du socle Terraform à
# build_chart_values ; la chart GCS refuse ce champ.
_PLACEHOLDER_AWS_ACCOUNT_ID = "000000000000"

# Même gap, pendant GCP : le vrai compte de service provient de la sortie
# Terraform de gcp/gke-addon (service_account_email, lui-même la sortie
# passthrough de gcp/base) ; ce répertoire n'a pas non plus accès aux
# sorties Terraform d'une étape précédente à ce stade du chantier. Domaine
# de documentation (jamais un projet GCP réel) — même convention que
# 000000000000 côté AWS, et syntaxiquement valide pour que l'étape
# `helm template` du plan continue de réussir.
_PLACEHOLDER_GCP_PROJECT_DOMAIN = "example-project.iam.gserviceaccount.com"


class InvalidInstallInputs(ValueError):
    pass


@dataclass(frozen=True)
class InstallInputs:
    cloud: str
    target: str
    region: str
    name: str
    namespace: str = "quadringent"
    # --existing-bucket / --existing-checkpoint-table : réutilise un stockage
    # déjà créé au lieu d'en créer un nouveau (deploy/terraform/<cloud>/base,
    # variables existing_bucket_name/existing_checkpoint_table_name).
    existing_bucket: str | None = None
    existing_checkpoint_table: str | None = None
    # --project (GCP, obligatoire) : projet réel du site, plus jamais un
    # "<a-completer-par-le-site>" laissé à la charge de l'opérateur dans le
    # tfvars généré. --aws-profile (AWS, optionnel) : profil de credentials
    # local, transmis en AWS_PROFILE aux commandes terraform/aws/kubectl.
    project: str | None = None
    aws_profile: str | None = None
    # Coordonnées du réseau de la VM ou du fournisseur OIDC du cluster EKS
    # existant. Les aperçus peuvent les omettre ; l'exécution réelle les
    # exige avant le premier plan Terraform.
    vpc_id: str | None = None
    subnet_id: str | None = None
    vm_instance_type: str | None = None
    gcp_network: str | None = None
    gcp_subnetwork: str | None = None
    gcp_zone: str | None = None
    image_pull_secret: str | None = None
    eks_oidc_provider_arn: str | None = None
    eks_oidc_provider_url: str | None = None
    # --admin-email : compte du premier admin ; sans lui, aucun lien
    # d'activation n'est émis (l'installation reste valide).
    admin_email: str | None = None

    def __post_init__(self) -> None:
        if self.cloud not in CLOUDS:
            raise InvalidInstallInputs(f"--cloud doit être aws ou gcp (reçu {self.cloud!r})")
        if self.target not in TARGETS:
            raise InvalidInstallInputs(f"--target doit être vm ou cluster (reçu {self.target!r})")
        if not _NAME_RE.match(self.name):
            raise InvalidInstallInputs(
                "--name doit être en minuscules, commencer par une lettre, 2 à 41 caractères, tirets autorisés "
                f"(reçu {self.name!r})"
            )
        if not self.region:
            raise InvalidInstallInputs("--region est obligatoire")
        if self.cloud == "aws" and not _AWS_REGION_RE.match(self.region):
            raise InvalidInstallInputs(f"--region doit être une région AWS valide (reçu {self.region!r})")
        if self.existing_checkpoint_table and self.cloud != "aws":
            raise InvalidInstallInputs(
                "--existing-checkpoint-table n'a de sens que pour --cloud aws (GCP n'a pas de table de "
                "checkpoints séparée : voir storage.checkpointBucket)"
            )
        if self.cloud == "gcp" and not self.project:
            raise InvalidInstallInputs("--project est obligatoire pour --cloud gcp")
        if self.project and self.cloud != "gcp":
            raise InvalidInstallInputs("--project n'a de sens que pour --cloud gcp")
        if self.admin_email is not None and not _EMAIL_RE.match(self.admin_email):
            raise InvalidInstallInputs(f"--admin-email doit être une adresse email simple (reçu {self.admin_email!r})")
        if self.aws_profile and self.cloud != "aws":
            raise InvalidInstallInputs("--aws-profile n'a de sens que pour --cloud aws")
        aws_target_fields = (self.vpc_id, self.subnet_id, self.eks_oidc_provider_arn, self.eks_oidc_provider_url)
        if self.cloud != "aws" and any(aws_target_fields):
            raise InvalidInstallInputs("les paramètres VPC et OIDC ne s'appliquent qu'à AWS")
        if (self.vpc_id or self.subnet_id) and self.target != "vm":
            raise InvalidInstallInputs("--vpc-id et --subnet-id ne s'appliquent qu'à --target vm")
        if self.vm_instance_type and (self.cloud != "aws" or self.target != "vm"):
            raise InvalidInstallInputs("--vm-instance-type ne s'applique qu'à une VM AWS")
        if self.vm_instance_type and not re.fullmatch(r"[a-z][a-z0-9-]*\.[a-z0-9-]+", self.vm_instance_type):
            raise InvalidInstallInputs("--vm-instance-type doit être un type EC2 valide")
        if any((self.gcp_network, self.gcp_subnetwork, self.gcp_zone)) and (self.cloud != "gcp" or self.target != "vm"):
            raise InvalidInstallInputs("--gcp-network, --gcp-subnetwork et --gcp-zone ne s'appliquent qu'à une VM GCP")
        for label, value in (("--gcp-network", self.gcp_network), ("--gcp-subnetwork", self.gcp_subnetwork)):
            if value is not None and not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", value):
                raise InvalidInstallInputs(f"{label} doit être un nom de réseau GCP valide")
        if self.gcp_zone is not None and not re.fullmatch(re.escape(self.region) + r"-[a-z]", self.gcp_zone):
            raise InvalidInstallInputs("--gcp-zone doit appartenir à la région GCP déclarée")
        if self.image_pull_secret and not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", self.image_pull_secret):
            raise InvalidInstallInputs("--image-pull-secret doit être un nom de Secret Kubernetes")
        if (self.eks_oidc_provider_arn or self.eks_oidc_provider_url) and self.target != "cluster":
            raise InvalidInstallInputs("les paramètres OIDC ne s'appliquent qu'à --target cluster")
        if self.vpc_id and not re.fullmatch(r"vpc-[a-f0-9]+", self.vpc_id):
            raise InvalidInstallInputs("--vpc-id doit être un identifiant VPC AWS")
        if self.subnet_id and not re.fullmatch(r"subnet-[a-f0-9]+", self.subnet_id):
            raise InvalidInstallInputs("--subnet-id doit être un identifiant de sous-réseau AWS")
        if self.eks_oidc_provider_arn and not re.fullmatch(
            r"arn:aws:iam::[0-9]{12}:oidc-provider/oidc\.eks\.[a-z0-9-]+\.amazonaws\.com/id/[A-Za-z0-9]+",
            self.eks_oidc_provider_arn,
        ):
            raise InvalidInstallInputs("--eks-oidc-provider-arn doit désigner un fournisseur IAM OIDC EKS")
        if self.eks_oidc_provider_url and not re.fullmatch(
            r"oidc\.eks\.[a-z0-9-]+\.amazonaws\.com/id/[A-Za-z0-9]+", self.eks_oidc_provider_url
        ):
            raise InvalidInstallInputs("--eks-oidc-provider-url doit être l'URL OIDC EKS sans https://")
        if self.eks_oidc_provider_arn and self.eks_oidc_provider_url:
            if self.eks_oidc_provider_arn.split("oidc-provider/", 1)[1] != self.eks_oidc_provider_url:
                raise InvalidInstallInputs("le fournisseur OIDC ARN et son URL ne correspondent pas")
        if self.eks_oidc_provider_url and not self.eks_oidc_provider_url.startswith(f"oidc.eks.{self.region}.amazonaws.com/"):
            raise InvalidInstallInputs("la région du fournisseur OIDC diffère de --region")


@dataclass(frozen=True)
class Step:
    """Une étape du plan. ``kind`` vaut ``command``, ``write_file`` ou
    ``copy_module`` (copie un module Terraform du dépôt vers l'espace de
    travail privé du site, gap (1) : l'état Terraform ne doit jamais
    atterrir dans l'arbre du dépôt)."""

    description: str
    kind: str
    argv: tuple[str, ...] = ()
    cwd: str | None = None
    path: str | None = None
    content: str | None = None
    source: str | None = None

    def render(self) -> str:
        if self.kind == "command":
            cwd_suffix = f"  (cwd={self.cwd})" if self.cwd else ""
            return f"$ {' '.join(self.argv)}{cwd_suffix}"
        if self.kind == "copy_module":
            return f"copier {self.source} -> {self.path}"
        return f"écrire {self.path}"


@dataclass(frozen=True)
class InstallPlan:
    inputs: InstallInputs
    workdir: Path
    manifest: ReleaseManifest
    steps: tuple[Step, ...]
    chart_values: dict
    terraform_dir: str

    def render_text(self) -> str:
        lines = [
            f"Installation Quadringent — cloud={self.inputs.cloud} target={self.inputs.target} "
            f"region={self.inputs.region} name={self.inputs.name}",
            f"Répertoire de travail : {self.workdir}",
            "",
        ]
        for index, step in enumerate(self.steps, start=1):
            lines.append(f"{index}. {step.description}")
            lines.append(f"   {step.render()}")
        return "\n".join(lines)


def _base_module_dir(cloud: str) -> str:
    return f"deploy/terraform/{cloud}/base"


def _vm_module_dir(cloud: str) -> str:
    return f"deploy/terraform/{cloud}/vm"


def _addon_module_dir(cloud: str) -> str:
    return f"deploy/terraform/{cloud}/eks-addon" if cloud == "aws" else f"deploy/terraform/{cloud}/gke-addon"


def _source_path(relative_path: str, assets_dir: Path | None) -> str:
    """Résout un fichier du sdist de release sans dépendre du dossier courant."""

    return str(assets_dir / relative_path) if assets_dir is not None else relative_path


def build_chart_values(
    inputs: InstallInputs,
    manifest: ReleaseManifest,
    *,
    control_plane_role_arn: str,
    control_plane_gcp_service_account: str | None = None,
    aws_account_id: str | None = None,
) -> dict:
    """Construit les values Helm générées par l'installateur.

    Volontairement minimal et sûr par défaut : ``replicaCount: 0`` (aucun
    lecteur IBM i tant qu'une connexion n'est pas déclarée),
    ``controlPlane.launch.enabled: false`` (aucun modèle de Job ni
    catalogue de flotte disponibles à l'installation). Voir
    docs/product/install-default.md pour le détail des écarts avec la
    consigne « launch enabled » du chantier : la chart actuelle ne peut pas
    les satisfaire sans données de site réelles.
    """

    backend = "aws" if inputs.cloud == "aws" else "gcs"
    # --existing-bucket réutilise un bucket déjà créé (deploy/terraform/
    # <cloud>/base, existing_bucket_name) : même nom déclaré côté chart que
    # côté Terraform, sinon le nom généré par défaut.
    raw_bucket = inputs.existing_bucket or f"{inputs.name}-quadringent-raw"
    raw_prefix = f"{inputs.name}/pending"
    site_id = inputs.name[:30]
    # site.awsAccountId et aws.region n'ont de sens que pour storage.backend=aws
    # (gap (c)) : sur GCS, la chart refuse désormais qu'ils soient déclarés.
    site_aws_account_id = (aws_account_id or _PLACEHOLDER_AWS_ACCOUNT_ID) if backend == "aws" else ""
    # En cluster GKE, la GSA est liée par Workload Identity. Sur la VM GCP
    # dédiée, cette même GSA bornée au bucket est attachée à l'instance :
    # les pods k3s utilisent le serveur de métadonnées Compute Engine.
    control_plane_enabled = True
    # Identité de capture (Deployment "capture" + Jobs/Deployments créés par
    # l'exécuteur de pipeline v2) : la même identité runtime bornée que le
    # control plane, produite par deploy/terraform/<cloud>/base et liée aux
    # DEUX ServiceAccounts Kubernetes (capture et control-plane) par
    # deploy/terraform/<cloud>/eks-addon|gke-addon — voir la note de décision
    # dans chart/templates/serviceaccount.yaml. Avant ce correctif,
    # l'installateur publiait {create: False, name: "default"} : les pods de
    # capture tournaient sous le ServiceAccount `default` du namespace, sans
    # aucun droit S3/GCS.
    capture_service_account_enabled = control_plane_enabled
    if backend == "aws":
        control_plane_service_account: dict = {
            "create": True,
            "name": "quadringent-control-plane",
            "roleArn": control_plane_role_arn,
        }
        capture_service_account: dict = {
            "create": capture_service_account_enabled,
            "name": "quadringent-capture",
            "roleArn": control_plane_role_arn,
        }
    else:
        control_plane_service_account = {
            "create": True,
            "name": "quadringent-control-plane",
            "gcpServiceAccount": control_plane_gcp_service_account or "",
        }
        capture_service_account = {
            "create": capture_service_account_enabled,
            "name": "quadringent-capture",
            "gcpServiceAccount": control_plane_gcp_service_account or "",
        }
    aws_region = inputs.region if backend == "aws" else ""

    storage: dict = {
        "backend": backend,
        "rawBucket": raw_bucket,
        "rawPrefix": raw_prefix,
        "streamKey": "pending",
    }
    if backend == "aws":
        # --existing-checkpoint-table : même règle que le bucket ci-dessus.
        storage["checkpointTable"] = inputs.existing_checkpoint_table or f"{inputs.name}-quadringent-checkpoints"
    else:
        storage["checkpointBucket"] = raw_bucket

    values: dict = {
        "image": {
            "repository": manifest.repository,
            "digest": manifest.image_digest,
            "pullPolicy": "IfNotPresent",
            "pullSecret": inputs.image_pull_secret or "",
        },
        "deployment": {
            "environment": "dev",
            "productionPromotionAllowed": False,
        },
        "site": {
            # gap (a) de docs/product/install-default.md, corrigé : aucune
            # connexion IBM i n'est encore déclarée à ce stade de
            # l'installateur (assistant en trois écrans, hors périmètre de ce
            # chantier). La chart accepte désormais ce cas explicitement —
            # tous les champs de connexion ci-dessous restent vides, plutôt
            # que des identifiants fictifs (PENDINGTABLE, ibmi-pending...).
            "connectionDeclared": False,
            "id": site_id,
            "awsAccountId": site_aws_account_id,
            "namespace": inputs.namespace,
            "ibmiHost": "",
            "ibmiUser": "",
            "sourceSchema": "",
            "proofTable": "",
            "journalName": "",
            "rawBucket": raw_bucket,
            "rawPrefixRoot": raw_prefix,
            # Table DynamoDB de checkpoints : ne concerne que storage.backend=aws
            # (gap (c)) — voir chart/templates/configmap-site.yaml et
            # quadringent.site_config. Sur GCS, storage.checkpointBucket porte
            # l'équivalent ; jamais de valeur fictive ici pour un champ sans
            # objet (l'ancien défaut publiait toujours "<name>-quadringent-
            # checkpoints", y compris sur GCS où aucune table n'existe).
            "checkpointTable": storage.get("checkpointTable", ""),
            "destinationDatabase": "",
            "destinationSchema": "",
            "destinationId": "",
            "snowflakeAccount": "",
            "fleetTables": [],
            "keyedTables": [],
            "provisionedStages": [],
            "reservableTables": [],
            "proofKeyColumns": [],
        },
        "serviceAccount": capture_service_account,
        "replicaCount": 0,
        "ibmi": {
            "host": "",
            "user": "",
            "schema": "",
            "table": "",
            "journalLibrary": "",
            "journalName": "",
            "sourceTimeZone": "",
            # Nom et clé cible du Secret credentials, choisis par
            # l'installateur : un pointeur vers l'emplacement que l'assistant
            # de connexion IBM i remplira, jamais une valeur de connexion en
            # elle-même (voir configmap-site.yaml).
            "passwordSecret": {"name": "quadringent-pending-ibmi", "key": "ISERIES_PASSWORD"},
        },
        "as400": {
            "tls": True,
            "allowPlaintext": False,
            "tlsCaFile": "",
            "tlsCaSecret": {"name": "quadringent-pending-ca", "key": "ca.pem"},
        },
        "storage": storage,
        "aws": {"region": aws_region},
        "controlPlane": {
            "enabled": control_plane_enabled,
            "image": {
                "repository": manifest.repository,
                "digest": manifest.control_plane_image_digest,
                "allowedRepositories": [manifest.repository],
            },
            "replicaCount": 1,
            "source": f"live:{site_id}:file:///var/lib/quadringent-fleet/fleet-console.json",
            "serviceAccount": control_plane_service_account,
            "launch": {"enabled": False},
            # Activation v2 + Postgres embarqué par défaut (voir
            # chart/values.yaml postgres.enabled: true, non surchargé ici) :
            # sans eux, aucun compte admin ne peut jamais s'activer (v1 n'a
            # pas de notion d'utilisateur/session) — voir
            # fetch_first_admin_activation_token. N'a d'effet que lorsque
            # controlPlane.enabled=true : le bloc entier de
            # chart/templates/control-plane.yaml, y compris le conteneur v2,
            # est conditionné à ce premier drapeau.
            "v2": {"enabled": control_plane_enabled},
        },
        # Non câblé au flux d'installation par défaut (verification.enabled
        # reste False : voir docs/product/install-default.md), mais le
        # digest doit malgré tout provenir du manifeste de version choisi —
        # jamais du digest par défaut figé dans la chart — pour qu'une
        # activation ultérieure (site.*) utilise l'image réellement publiée
        # pour cette installation.
        "verification": {
            "imageDigest": manifest.verifier_image_digest,
        },
    }
    if backend == "gcs":
        values["gcpIdentityMode"] = "vm-metadata" if inputs.target == "vm" else "gke-workload-identity"
    return values


def _yaml_dump(values: dict) -> str:
    import yaml

    class _ValuesDumper(yaml.SafeDumper):
        pass

    def _represent_string(dumper: yaml.SafeDumper, value: str) -> yaml.nodes.ScalarNode:
        # Helm's YAML parser treats an unquoted 12-digit AWS account ID as a
        # number even when PyYAML considers a leading-zero value a string.
        style = '"' if value.isdecimal() else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)

    _ValuesDumper.add_representer(str, _represent_string)
    return yaml.dump(values, Dumper=_ValuesDumper, sort_keys=False, allow_unicode=True)


def _tfvars_json_dump(values: dict) -> str:
    """Fichier ``*.auto.tfvars.json`` : chargé automatiquement par Terraform
    sans ``-var-file``, JSON valide (contrairement à l'ancien format
    ``clé = valeur`` ad hoc)."""

    return json.dumps(values, indent=2, ensure_ascii=False) + "\n"


def _module_workdir(workdir: Path, module: str) -> Path:
    """Copie privée du module Terraform, dans l'espace de travail du site —
    jamais dans l'arbre du dépôt (gap (1) : l'état Terraform doit rester lié
    au site, pas au checkout du code)."""

    return workdir / "terraform" / module


def base_tfvars_for(inputs: InstallInputs) -> dict:
    tfvars: dict = (
        {"name": inputs.name, "region": inputs.region}
        if inputs.cloud == "aws"
        else {"project_id": inputs.project, "name": inputs.name, "region": inputs.region}
    )
    # --existing-bucket / --existing-checkpoint-table : deploy/terraform/
    # <cloud>/base bascule alors sur un chemin data source (create_bucket/
    # create_table=false côté Terraform) plutôt que de créer le stockage.
    if inputs.existing_bucket:
        tfvars["existing_bucket_name"] = inputs.existing_bucket
    if inputs.existing_checkpoint_table:
        tfvars["existing_checkpoint_table_name"] = inputs.existing_checkpoint_table
    return tfvars


def _base_reuse_suffix(inputs: InstallInputs) -> str:
    notes = []
    if inputs.existing_bucket:
        notes.append(f"bucket {inputs.existing_bucket!r} réutilisé")
    if inputs.existing_checkpoint_table:
        notes.append(f"table de checkpoints {inputs.existing_checkpoint_table!r} réutilisée")
    return f" — {', '.join(notes)}, aucune création" if notes else ""


def vm_tfvars_for(inputs: InstallInputs, base_outputs: dict | None = None) -> dict:
    """``base_outputs`` : sorties Terraform réelles du socle (gap (3)), déjà
    lues via ``terraform output -json``. ``None`` (aperçu ``--dry-run``,
    aucune commande exécutée) : espaces réservés textuels explicites."""

    outputs = base_outputs or {}
    if inputs.cloud == "aws":
        return {
            "name": inputs.name,
            "vpc_id": inputs.vpc_id or "<a-completer-par-le-site>",
            "subnet_id": inputs.subnet_id or "<a-completer-par-le-site>",
            "instance_profile_name": outputs.get(
                "runtime_instance_profile_name", "<sortie-terraform-base:runtime_instance_profile_name>"
            ),
            "instance_role_name": outputs.get(
                "runtime_role_name", "<sortie-terraform-base:runtime_role_name>"
            ),
            "architecture": "x86_64",
            "instance_type": inputs.vm_instance_type or "t3.medium",
        }
    return {
        "project_id": inputs.project,
        "name": inputs.name,
        "region": inputs.region,
        "zone": inputs.gcp_zone or f"{inputs.region}-b",
        "network": inputs.gcp_network or "<a-completer-par-le-site>",
        "subnetwork": inputs.gcp_subnetwork or "<a-completer-par-le-site>",
        "service_account_email": outputs.get("service_account_email", "<sortie-terraform-base:service_account_email>"),
    }


# Nom du ServiceAccount Kubernetes de capture, identique à celui publié par
# build_chart_values (values["serviceAccount"]["name"]) : les modules
# gcp/gke-addon et aws/eks-addon lient l'identité runtime à ce nom en plus du
# ServiceAccount du control plane (voir la note de décision dans
# chart/templates/serviceaccount.yaml — une seule GSA/rôle IAM, deux KSA).
CAPTURE_KUBERNETES_SERVICE_ACCOUNT_NAME = "quadringent-capture"


def addon_tfvars_for(inputs: InstallInputs, base_outputs: dict | None = None) -> dict:
    outputs = base_outputs or {}
    if inputs.cloud == "aws":
        return {
            "name": inputs.name,
            "runtime_policy_arn": outputs.get("runtime_policy_arn", "<sortie-terraform-base:runtime_policy_arn>"),
            "oidc_provider_arn": inputs.eks_oidc_provider_arn or "<a-completer-par-le-site>",
            "oidc_provider_url": inputs.eks_oidc_provider_url or "<a-completer-par-le-site>",
            "namespace": inputs.namespace,
            "capture_service_account_name": CAPTURE_KUBERNETES_SERVICE_ACCOUNT_NAME,
        }
    return {
        "project_id": inputs.project,
        "name": inputs.name,
        "service_account_email": outputs.get("service_account_email", "<sortie-terraform-base:service_account_email>"),
        "namespace": inputs.namespace,
        "capture_kubernetes_service_account_name": CAPTURE_KUBERNETES_SERVICE_ACCOUNT_NAME,
    }


def _terraform_module_steps(
    *, module_label: str, module_source: str, module_workdir: Path, tfvars: dict, apply_note: str = ""
) -> list["Step"]:
    """Séquence complète d'un module Terraform : copie dans l'espace de
    travail privé du site (gap (1)), tfvars auto-chargé, plan résumé avec
    confirmation et refus de destruction implicite (gap (5)), application du
    plan sauvegardé (jamais un ``apply -auto-approve`` en aveugle), lecture
    des sorties réelles (gap (3))."""

    tfvars_path = module_workdir / "site.auto.tfvars.json"
    return [
        Step(
            description=f"Copier le module Terraform {module_label} dans l'espace de travail privé du site",
            kind="copy_module",
            path=str(module_workdir),
            source=module_source,
        ),
        Step(
            description=f"Générer les variables Terraform {module_label}{apply_note}",
            kind="write_file",
            path=str(tfvars_path),
            content=_tfvars_json_dump(tfvars),
        ),
        Step(
            description=f"Initialiser le module Terraform {module_label}",
            kind="command",
            argv=("terraform", "init", "-input=false"),
            cwd=str(module_workdir),
        ),
        Step(
            description=f"Calculer le plan Terraform {module_label}",
            kind="command",
            argv=("terraform", "plan", "-input=false", "-out=tfplan"),
            cwd=str(module_workdir),
        ),
        Step(
            description=(
                f"Résumer le plan {module_label} et demander confirmation "
                "(refusé sans confirmation ni --yes ; toute destruction exige --allow-destroy)"
            ),
            kind="command",
            argv=("terraform", "show", "-json", "tfplan"),
            cwd=str(module_workdir),
        ),
        Step(
            description=f"Appliquer le plan Terraform {module_label}{apply_note}",
            kind="command",
            argv=("terraform", "apply", "-input=false", "tfplan"),
            cwd=str(module_workdir),
        ),
        Step(
            description=f"Lire les sorties Terraform {module_label} (identité réelle, gap 3)",
            kind="command",
            argv=("terraform", "output", "-json"),
            cwd=str(module_workdir),
        ),
    ]


def build_plan(
    inputs: InstallInputs, workdir: Path, manifest: ReleaseManifest, *, assets_dir: Path | None = None
) -> InstallPlan:
    base_dir = _source_path(_base_module_dir(inputs.cloud), assets_dir)
    base_workdir = _module_workdir(workdir, "base")
    steps: list[Step] = []

    base_tfvars = base_tfvars_for(inputs)
    steps.extend(
        _terraform_module_steps(
            module_label="du socle (bucket, checkpoints, identité)",
            module_source=base_dir,
            module_workdir=base_workdir,
            tfvars=base_tfvars,
            apply_note=_base_reuse_suffix(inputs),
        )
    )

    control_plane_role_arn_placeholder = f"arn:aws:iam::{_PLACEHOLDER_AWS_ACCOUNT_ID}:role/{inputs.name}-quadringent-runtime"
    # Même convention côté GCP : le compte de service réel est une sortie
    # Terraform de gcp/gke-addon (service_account_email), non disponible à
    # ce stade — voir _PLACEHOLDER_GCP_PROJECT_DOMAIN. En exécution réelle
    # (apply.py), ces deux valeurs sont remplacées par les sorties Terraform
    # effectivement lues (gap (3)) : jamais un exemple fictif dans une vraie
    # installation.
    control_plane_gcp_service_account_placeholder = f"{inputs.name}-quadringent-runtime@{_PLACEHOLDER_GCP_PROJECT_DOMAIN}"

    if inputs.target == "vm":
        vm_dir = _source_path(_vm_module_dir(inputs.cloud), assets_dir)
        vm_workdir = _module_workdir(workdir, "vm")
        vm_tfvars = vm_tfvars_for(inputs)
        steps.extend(
            _terraform_module_steps(
                module_label="de la VM k3s",
                module_source=vm_dir,
                module_workdir=vm_workdir,
                tfvars=vm_tfvars,
            )
        )
        if inputs.cloud == "aws":
            steps.extend((
                Step(
                    description="Récupérer le kubeconfig k3s depuis la VM via SSM, dans un fichier privé 0600",
                    kind="command",
                    argv=("aws", "ssm", "send-command", "--instance-ids", "<sortie-terraform-vm:instance_id>",
                          "--document-name", "AWS-RunShellScript"),
                ),
                Step(
                    description="Ouvrir un tunnel SSM temporaire vers l'API k3s ; le fermer après l'installation",
                    kind="command",
                    argv=("aws", "ssm", "start-session", "--target", "<sortie-terraform-vm:instance_id>",
                          "--document-name", "AWS-StartPortForwardingSession"),
                ),
            ))
        else:
            steps.extend((
                Step(
                    description="Récupérer le kubeconfig k3s via SSH IAP dans un fichier privé 0600",
                    kind="command",
                    argv=("gcloud", "compute", "ssh", "<sortie-terraform-vm:instance_name>",
                          "--tunnel-through-iap", "--command", "sudo cat /etc/rancher/k3s/k3s.yaml"),
                ),
                Step(
                    description="Ouvrir un tunnel SSH IAP temporaire vers l'API k3s ; le fermer après l'installation",
                    kind="command",
                    argv=("gcloud", "compute", "ssh", "<sortie-terraform-vm:instance_name>",
                          "--tunnel-through-iap", "--", "-N", "-L", "127.0.0.1:<port-local>:127.0.0.1:6443"),
                ),
            ))
    else:
        addon_dir = _source_path(_addon_module_dir(inputs.cloud), assets_dir)
        addon_workdir = _module_workdir(workdir, "addon")
        addon_tfvars = addon_tfvars_for(inputs)
        addon_label = "de la liaison d'identité (IRSA/Workload Identity)"
        steps.extend(
            _terraform_module_steps(
                module_label=addon_label,
                module_source=addon_dir,
                module_workdir=addon_workdir,
                tfvars=addon_tfvars,
            )
        )
        steps.append(
            Step(
                description="Vérifier l'accès au cluster existant (kubeconfig déjà configuré par le site)",
                kind="command",
                argv=("kubectl", "cluster-info"),
            )
        )

    chart_values = build_chart_values(
        inputs,
        manifest,
        control_plane_role_arn=control_plane_role_arn_placeholder,
        control_plane_gcp_service_account=control_plane_gcp_service_account_placeholder,
    )
    values_path = workdir / "chart-values.generated.yaml"
    steps.append(
        Step(
            description="Générer les values Helm (stockage, identité résolue, control plane)",
            kind="write_file",
            path=str(values_path),
            content=_yaml_dump(chart_values),
        )
    )
    steps.append(
        Step(
            description="Valider le rendu de la chart (helm template)",
            kind="command",
            argv=("helm", "template", inputs.name, _source_path("chart", assets_dir), "--namespace", inputs.namespace, "-f", str(values_path)),
        )
    )
    steps.append(
        Step(
            description="Installer la chart Quadringent",
            kind="command",
            argv=(
                "helm",
                "upgrade",
                "--install",
                inputs.name,
                _source_path("chart", assets_dir),
                "--namespace",
                inputs.namespace,
                "--create-namespace",
                "-f",
                str(values_path),
            ),
        )
    )
    v2_enabled = bool(chart_values.get("controlPlane", {}).get("v2", {}).get("enabled", False))
    if v2_enabled:
        control_plane_deployment = f"deployment/{inputs.name}-quadringent-control-plane"
        steps.append(
            Step(
                description="Attendre le déploiement du control plane (kubectl rollout status)",
                kind="command",
                argv=("kubectl", "-n", inputs.namespace, "rollout", "status", control_plane_deployment, "--timeout=180s"),
            )
        )
        steps.append(
            Step(
                description=(
                    "Récupérer le jeton d'activation du premier admin (kubectl exec, "
                    "POST /v2/setup/first-admin depuis l'intérieur du pod)"
                ),
                kind="command",
                # Aperçu abrégé : l'exécution réelle (fetch_first_admin_activation_token)
                # construit le script Python complet elle-même, indépendamment de ce
                # texte de plan purement illustratif.
                argv=(
                    "kubectl", "-n", inputs.namespace, "exec", control_plane_deployment,
                    "-c", _V2_CONTAINER, "--", "python", "-c",
                    f"<POST http://127.0.0.1:{_V2_LOOPBACK_PORT}/v2/setup/first-admin>",
                ),
            )
        )

    return InstallPlan(
        inputs=inputs,
        workdir=workdir,
        manifest=manifest,
        steps=tuple(steps),
        chart_values=chart_values,
        terraform_dir=base_dir,
    )



_V2_CONTAINER = "control-plane-v2"
_V2_LOOPBACK_PORT = 8845

# Script minimal exécuté dans le pod control-plane via `kubectl exec` — le
# seul appel réseau est un loopback vers le conteneur v2 du même Pod, jamais
# un port-forward exposé sur la machine de l'opérateur pour cet appel
# unique. L'image du control plane (docker/control-plane.Dockerfile) n'a
# pas de venv : son interpréteur est ``python`` dans le PATH, aucune
# dépendance supplémentaire (curl, etc.) posée dessus.
def first_admin_script(email: str, idempotency_key: str, *, port: int = _V2_LOOPBACK_PORT) -> str:
    """Script exécuté dans le conteneur v2 : ``POST /v2/setup/first-admin``.
    La clé d'idempotence est déterministe (dérivée du nom d'installation) :
    relancer l'installation renvoie les mêmes métadonnées, sans réémettre le
    jeton d'activation. Un lien perdu exige une réémission explicite. L'email est
    validé en amont (``InstallInputs``) et injecté comme littéral Python."""
    body = json.dumps({"email": email}).encode("utf-8")
    return (
        "import json,urllib.request,urllib.error\n"
        "req = urllib.request.Request(\n"
        f"    'http://127.0.0.1:{int(port)}/v2/setup/first-admin',\n"
        f"    data={body!r},\n"
        f"    headers={{'Content-Type': 'application/json', 'Idempotency-Key': {idempotency_key!r}}},\n"
        "    method='POST',\n"
        ")\n"
        "try:\n"
        "    with urllib.request.urlopen(req, timeout=5) as response:\n"
        "        print(response.read().decode('utf-8'))\n"
        "except urllib.error.HTTPError as error:\n"
        "    print(error.read().decode('utf-8'))\n"
        "    raise SystemExit(1)\n"
    )


_AGENT_SCOPES = ("read", "operate", "admin")


def agent_token_script(label: str, scope: str, *, days: int) -> str:
    """Script exécuté dans le conteneur v2 : émet un jeton d'agent borné dans
    le temps, directement en base (même service que ``POST /v2/agent-tokens``).
    Réservé à qui a ``kubectl exec`` sur le Pod, donc déjà administrateur de
    fait. Le DSN est recomposé comme au démarrage du conteneur."""
    if scope not in _AGENT_SCOPES:
        raise InvalidInstallInputs(f"--scope doit être read, operate ou admin (reçu {scope!r})")
    if not 1 <= int(days) <= 90:
        raise InvalidInstallInputs("--days doit être compris entre 1 et 90")
    if not re.match(r"^[A-Za-z0-9._-]{1,64}\Z", label):
        raise InvalidInstallInputs("--label : 1 à 64 caractères alphanumériques, point, tiret ou souligné")
    return (
        "import json,os\n"
        "from datetime import datetime,timedelta,timezone\n"
        "from quadringent_control_plane.v2 import db\n"
        "from quadringent_control_plane.v2.crypto import load_token_pepper\n"
        "from quadringent_control_plane.v2.entrypoint import resolve_org_id\n"
        "from quadringent_control_plane.v2.services.agent_tokens import AgentTokensService\n"
        "e=os.environ\n"
        "dsn=e.get('QUADRINGENT_V2_DATABASE_URL') or "
        "'postgresql+psycopg://%s:%s@%s:%s/%s'%(e['POSTGRES_USER'],e['POSTGRES_PASSWORD'],"
        "e['POSTGRES_HOST'],e['POSTGRES_PORT'],e['POSTGRES_DB'])\n"
        "engine=db.create_engine_for(dsn)\n"
        "service=AgentTokensService(engine,org_id=resolve_org_id(e),pepper=load_token_pepper(e))\n"
        f"record,value=service.create(name={label!r},scope={scope!r},created_by='kubectl-exec',"
        f"expires_at=datetime.now(timezone.utc)+timedelta(days={int(days)}))\n"
        "from quadringent_control_plane.v2.services.audit import AuditService\n"
        "AuditService(engine,org_id=resolve_org_id(e)).record(actor_kind='human',actor_id='kubectl-exec',"
        "actor_display='Opérateur cluster (kubectl exec)',action='agent_token.create',"
        "resource_type='agent_token',resource_id=record.id,status='succeeded',"
        "after={'name':record.name,'scope':record.scope,'expires_at':str(record.expires_at),'via':'kubectl-exec'})\n"
        "print(json.dumps({'id':record.id,'token':value}))\n"
    )


def fetch_first_admin_activation_token(
    inputs: InstallInputs, runner: "CommandRunner", *, v2_enabled: bool,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Récupère le jeton d'activation à usage unique du premier admin en
    appelant ``POST /v2/setup/first-admin`` **depuis l'intérieur** du pod
    control-plane (``kubectl exec``), en loopback sur le port v2 — jamais de
    port-forward exposé pour cet appel unique, jamais de dépendance réseau
    supplémentaire côté machine de l'opérateur.

    Retourne ``None`` sans lever si le control plane v2 n'est pas activé, si
    la commande échoue (pod pas encore prêt, admin déjà activé — 409 — etc.)
    ou si la réponse n'a pas la forme attendue : l'installateur ne doit
    jamais inventer un jeton, seulement relayer celui réellement émis.
    """

    if not v2_enabled or not inputs.admin_email:
        return None
    deployment = f"deployment/{inputs.name}-quadringent-control-plane"
    result = runner.run(
        (
            "kubectl",
            "-n",
            inputs.namespace,
            "exec",
            deployment,
            "-c",
            _V2_CONTAINER,
            "--",
            "python",
            "-c",
            first_admin_script(inputs.admin_email, f"quadringent-install-{inputs.name}"),
        ),
        env=env,
    )
    if not result.ok:
        return None
    try:
        body = json.loads(result.stdout)
        token = body["after"]["activation_token"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    return token if isinstance(token, str) and token else None


def activation_message(inputs: InstallInputs, *, activation_token: str | None = None, workdir: Path | None = None) -> str:
    port = 8844
    access = (
        f"quadringent vm-tunnel --name {inputs.name}"
        + (f" --workdir {shlex.quote(str(workdir))}" if workdir is not None else "")
        + (f" --aws-profile {inputs.aws_profile}" if inputs.aws_profile else "")
        + " ; puis, dans un autre terminal, exécuter la commande kubectl port-forward affichée"
        if inputs.target == "vm"
        else f"kubectl -n {inputs.namespace} port-forward deployment/{inputs.name}-quadringent-control-plane {port}:{port}"
    )
    if activation_token:
        # Lien en forme de hash (``#/wizard/activate?token=...``) plutôt que
        # de chemin (``/activate?token=...``) : le routeur UI
        # (``ui/src/router.ts``) est entièrement basé sur ``location.hash``
        # et sait déjà servir cette route (``WizardActivate.tsx``) — aucun
        # fragment n'est envoyé au serveur, donc le navigateur charge
        # simplement la racine (``index.html``, déjà servie par le serveur
        # v1 en fallback SPA) puis résout la route côté client. Solution
        # choisie plutôt que d'apprendre au routeur à reconnaître
        # ``location.pathname === '/activate'`` : un seul mécanisme de
        # routage, pas deux (tâche « auth-login »).
        activation_line = (
            f"Lien d'activation admin : http://127.0.0.1:{port}/#/wizard/activate?token={activation_token}"
        )
    else:
        # Jamais de lien fictif : soit le control plane v2 n'est pas encore
        # actif, soit la première émission a déjà eu lieu. Le rejeu n'est pas
        # une récupération de secret ; ne pas recommander une boucle install.
        activation_line = (
            "Lien d'activation admin : non émis (v2 indisponible, --admin-email absent, première émission "
            "déjà faite ou admin actif). Le rejeu ne réaffiche jamais le jeton. Si le lien initial est perdu "
            "et l'admin non activé, un opérateur authentifié admin peut retrouver son identifiant via "
            "`quadringent users list`, puis demander `quadringent users reissue-activation <user_id>`. "
            "Cette action explicite invalide les anciens liens ; sinon se connecter avec l'admin existant."
        )
    return (
        f"URL (après tunnel) : http://127.0.0.1:{port}\n"
        f"Tunnel : {access}\n"
        f"{activation_line}\n"
        "Étape suivante hors chantier 6 : connecter votre IBM i depuis l'assistant "
        "d'activation (l'installateur ne déclare pas encore la source)."
    )
