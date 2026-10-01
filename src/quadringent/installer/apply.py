"""Exécution réelle de l'installation (hors ``--dry-run``).

``plan.py`` reste l'aperçu textuel imprimé par ``--dry-run`` : entièrement
statique, construit sans toucher au disque ni au réseau. L'exécution réelle
ne peut pas être aussi statique — les sorties Terraform du socle ne sont
connues qu'après l'avoir réellement appliqué (gap (3)), et chaque module
Terraform doit tourner dans une copie privée de l'espace de travail du site,
jamais dans l'arbre du dépôt (gap (1)). Ce module porte donc sa propre
orchestration, avec le même ``CommandRunner`` injectable que le reste de
l'installateur (aucune commande externe n'est jamais exécutée pendant la
suite de tests : ``RecordingRunner`` script les réponses).
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
from typing import Callable, Mapping, TextIO

from .plan import (
    InstallInputs,
    _addon_module_dir,
    _base_module_dir,
    _module_workdir,
    _source_path,
    _tfvars_json_dump,
    _vm_module_dir,
    _yaml_dump,
    activation_message,
    addon_tfvars_for,
    base_tfvars_for,
    build_chart_values,
    fetch_first_admin_activation_token,
    vm_tfvars_for,
)
from .manifest import ReleaseManifest
from .runner import CommandRunner
from .vm_access import VmAccessError, connect_aws_vm, connect_gcp_vm

ConfirmFn = Callable[[str], bool]


def _default_confirm(prompt: str) -> bool:
    try:
        answer = input(f"{prompt} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes", "o", "oui")


class TerraformApplyRefused(RuntimeError):
    """Le plan Terraform a été refusé : destruction sans --allow-destroy,
    ou confirmation interactive déclinée. Jamais une exception silencieuse —
    toujours un message actionnable pour l'opérateur."""


def _summarize_plan(show_json_stdout: str) -> tuple[int, int, int]:
    """Compte les ajouts/modifications/suppressions d'un ``terraform show
    -json <planfile>``. Une action de remplacement (``delete, create``) est
    comptée à la fois en suppression et en création — elle détruit une
    ressource existante."""

    try:
        payload = json.loads(show_json_stdout or "{}")
    except json.JSONDecodeError:
        return (0, 0, 0)
    creates = updates = deletes = 0
    for change in payload.get("resource_changes", []):
        actions = change.get("change", {}).get("actions", [])
        if "create" in actions:
            creates += 1
        if "update" in actions:
            updates += 1
        if "delete" in actions:
            deletes += 1
    return (creates, updates, deletes)


def apply_terraform_module(
    *,
    module_label: str,
    source_dir: str,
    module_workdir: Path,
    tfvars: dict,
    runner: CommandRunner,
    stdout: TextIO,
    env: Mapping[str, str] | None = None,
    yes: bool = False,
    allow_destroy: bool = False,
    confirm: ConfirmFn | None = None,
) -> dict:
    """Copie ``source_dir`` (module du dépôt) dans ``module_workdir``
    (espace de travail privé du site, gap (1) : l'état Terraform ne doit
    jamais vivre dans l'arbre du dépôt), écrit les variables en
    ``site.auto.tfvars.json`` (auto-chargé, pas de ``-var-file``), calcule un
    plan sauvegardé, en affiche le résumé, refuse toute destruction sans
    ``--allow-destroy`` et exige une confirmation sauf ``--yes`` (gap (5)),
    applique le plan sauvegardé (jamais un ``apply -auto-approve`` en
    aveugle sur des variables non revues), puis relit les sorties réelles
    (gap (3)).

    Lève :class:`TerraformApplyRefused` si le plan est refusé — jamais un
    ``apply`` silencieux sur une destruction ou sans confirmation.
    """

    confirm_fn = confirm or _default_confirm
    module_workdir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, module_workdir, dirs_exist_ok=True)
    tfvars_path = module_workdir / "site.auto.tfvars.json"
    tfvars_path.write_text(_tfvars_json_dump(tfvars), encoding="utf-8")

    cwd = str(module_workdir)
    print(f"-> Initialiser le module Terraform {module_label}", file=stdout)
    result = runner.run(("terraform", "init", "-input=false"), cwd=cwd, env=env)
    if not result.ok:
        print(result.stdout, file=stdout)
        print(result.stderr, file=stdout)
        raise TerraformApplyRefused(f"terraform init a échoué pour le module {module_label}")

    print(f"-> Calculer le plan Terraform {module_label}", file=stdout)
    result = runner.run(("terraform", "plan", "-input=false", "-out=tfplan"), cwd=cwd, env=env)
    if not result.ok:
        print(result.stdout, file=stdout)
        print(result.stderr, file=stdout)
        raise TerraformApplyRefused(f"terraform plan a échoué pour le module {module_label}")

    result = runner.run(("terraform", "show", "-json", "tfplan"), cwd=cwd, env=env)
    if not result.ok:
        raise TerraformApplyRefused(f"terraform show -json a échoué pour le module {module_label}")
    creates, updates, deletes = _summarize_plan(result.stdout)
    print(
        f"   Plan {module_label} : {creates} ajout(s), {updates} modification(s), {deletes} suppression(s)",
        file=stdout,
    )
    if deletes > 0 and not allow_destroy:
        raise TerraformApplyRefused(
            f"le plan {module_label} détruit {deletes} ressource(s) : relancer avec --allow-destroy pour "
            "confirmer explicitement, ou corriger les variables si ce n'est pas voulu"
        )
    if not yes and not confirm_fn(f"Appliquer le plan Terraform {module_label} ?"):
        raise TerraformApplyRefused(f"application du module {module_label} annulée (pas de confirmation)")

    print(f"-> Appliquer le plan Terraform {module_label}", file=stdout)
    result = runner.run(("terraform", "apply", "-input=false", "tfplan"), cwd=cwd, env=env)
    if not result.ok:
        print(result.stdout, file=stdout)
        print(result.stderr, file=stdout)
        raise TerraformApplyRefused(f"terraform apply a échoué pour le module {module_label}")

    result = runner.run(("terraform", "output", "-json"), cwd=cwd, env=env)
    if not result.ok:
        raise TerraformApplyRefused(f"terraform output -json a échoué pour le module {module_label}")
    try:
        raw_outputs = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as error:
        raise TerraformApplyRefused(f"sorties Terraform illisibles pour le module {module_label}") from error
    return {key: value.get("value") for key, value in raw_outputs.items()}


def _control_plane_identity(inputs: InstallInputs, base_outputs: dict, addon_outputs: dict) -> tuple[str, str | None]:
    """Résout l'identité réelle du control plane depuis les sorties
    Terraform effectivement lues (gap (3)) — jamais un exemple fictif dans
    une vraie installation : lève si l'information attendue est absente.
    """

    if inputs.cloud == "aws":
        role_arn = (addon_outputs.get("role_arn") if inputs.target == "cluster" else None) or base_outputs.get(
            "runtime_role_arn"
        )
        if not role_arn:
            raise TerraformApplyRefused(
                "identité IAM introuvable dans les sorties Terraform (role_arn de aws/eks-addon ou "
                "runtime_role_arn de aws/base) : rien à publier dans controlPlane.serviceAccount.roleArn"
            )
        return role_arn, None
    gcp_service_account = (
        addon_outputs.get("service_account_email") if inputs.target == "cluster" else None
    ) or base_outputs.get("service_account_email")
    if not gcp_service_account:
        raise TerraformApplyRefused(
            "compte de service GCP introuvable dans les sorties Terraform (service_account_email de "
            "gcp/gke-addon ou gcp/base) : rien à publier dans controlPlane.serviceAccount.gcpServiceAccount"
        )
    return "", gcp_service_account


def execute_install(
    inputs: InstallInputs,
    manifest: ReleaseManifest,
    workdir: Path,
    runner: CommandRunner,
    stdout: TextIO,
    *,
    yes: bool = False,
    allow_destroy: bool = False,
    confirm: ConfirmFn | None = None,
    vm_connector=connect_aws_vm,
    gcp_vm_connector=connect_gcp_vm,
    assets_dir: Path | None = None,
) -> int:
    """Exécute réellement l'installation (jamais appelé en ``--dry-run``).
    Retourne le code de sortie du CLI (0 = succès)."""

    if inputs.cloud == "aws" and inputs.target == "cluster" and not (
        inputs.eks_oidc_provider_arn and inputs.eks_oidc_provider_url
    ):
        print("EKS : --eks-oidc-provider-arn et --eks-oidc-provider-url sont obligatoires", file=stdout)
        return 2
    if inputs.cloud == "aws" and inputs.target == "vm" and not (inputs.vpc_id and inputs.subnet_id):
        print("VM AWS : --vpc-id et --subnet-id sont obligatoires", file=stdout)
        return 2
    if inputs.cloud == "gcp" and inputs.target == "vm" and not (inputs.gcp_network and inputs.gcp_subnetwork):
        print("VM GCP : --gcp-network et --gcp-subnetwork sont obligatoires", file=stdout)
        return 2

    env: dict[str, str] | None = None
    if inputs.cloud == "aws":
        # The requested installation region wins over a profile's default
        # region, including for Terraform, AWS CLI, and kubectl exec plugins.
        env = {"AWS_REGION": inputs.region, "AWS_DEFAULT_REGION": inputs.region}
        if inputs.aws_profile:
            env["AWS_PROFILE"] = inputs.aws_profile

    base_workdir = _module_workdir(workdir, "base")
    try:
        base_outputs = apply_terraform_module(
            module_label="du socle (bucket, checkpoints, identité)",
            source_dir=_source_path(_base_module_dir(inputs.cloud), assets_dir),
            module_workdir=base_workdir,
            tfvars=base_tfvars_for(inputs),
            runner=runner,
            stdout=stdout,
            env=env,
            yes=yes,
            allow_destroy=allow_destroy,
            confirm=confirm,
        )
    except TerraformApplyRefused as error:
        print(f"Échec du socle Terraform : {error}", file=stdout)
        return 1
    account_id = base_outputs.get("account_id") if inputs.cloud == "aws" else None
    if inputs.cloud == "aws" and (not isinstance(account_id, str) or not (account_id.isdigit() and len(account_id) == 12)):
        print("Échec du socle Terraform : sortie account_id AWS absente ou invalide", file=stdout)
        return 1
    if inputs.cloud == "aws" and inputs.target == "cluster":
        assert inputs.eks_oidc_provider_arn is not None
        if inputs.eks_oidc_provider_arn.split("::", 1)[1].split(":", 1)[0] != account_id:
            print("EKS : le fournisseur OIDC ne dépend pas du compte AWS du socle", file=stdout)
            return 1

    addon_outputs: dict = {}
    if inputs.target == "vm":
        vm_workdir = _module_workdir(workdir, "vm")
        try:
            vm_outputs = apply_terraform_module(
                module_label="de la VM k3s",
                source_dir=_source_path(_vm_module_dir(inputs.cloud), assets_dir),
                module_workdir=vm_workdir,
                tfvars=vm_tfvars_for(inputs, base_outputs),
                runner=runner,
                stdout=stdout,
                env=env,
                yes=yes,
                allow_destroy=allow_destroy,
                confirm=confirm,
            )
        except TerraformApplyRefused as error:
            print(f"Échec de la VM Terraform : {error}", file=stdout)
            return 1
        print("-> Ouvrir un tunnel temporaire vers k3s", file=stdout)
        try:
            if inputs.cloud == "aws":
                connection = vm_connector(vm_outputs.get("instance_id"), workdir, runner, env or {})
            else:
                expected_zone = inputs.gcp_zone or f"{inputs.region}-b"
                if vm_outputs.get("zone") != expected_zone:
                    raise VmAccessError("la zone de la VM GCP diffère de la zone demandée")
                assert inputs.project is not None
                connection = gcp_vm_connector(
                    vm_outputs.get("instance_name"), expected_zone, inputs.project, workdir, runner, env or {},
                )
            with connection as vm_env:
                result = runner.run(("kubectl", "cluster-info"), env=vm_env)
                if not result.ok:
                    print("Échec de l'accès à k3s via le tunnel privé", file=stdout)
                    return 1
                return _deploy_chart(inputs, manifest, workdir, runner, stdout, base_outputs, {}, account_id, vm_env,
                                     assets_dir=assets_dir)
        except VmAccessError as error:
            print(f"Échec de l'accès VM : {error}", file=stdout)
            return 1
    else:
        addon_workdir = _module_workdir(workdir, "addon")
        try:
            addon_outputs = apply_terraform_module(
                module_label="de la liaison d'identité (IRSA/Workload Identity)",
                source_dir=_source_path(_addon_module_dir(inputs.cloud), assets_dir),
                module_workdir=addon_workdir,
                tfvars=addon_tfvars_for(inputs, base_outputs),
                runner=runner,
                stdout=stdout,
                env=env,
                yes=yes,
                allow_destroy=allow_destroy,
                confirm=confirm,
            )
        except TerraformApplyRefused as error:
            print(f"Échec de la liaison d'identité Terraform : {error}", file=stdout)
            return 1
        print("-> Vérifier l'accès au cluster existant", file=stdout)
        result = runner.run(("kubectl", "cluster-info"), env=env)
        if not result.ok:
            print(result.stdout, file=stdout)
            print(result.stderr, file=stdout)
            print("Échec de l'accès au cluster existant", file=stdout)
            return 1

    return _deploy_chart(inputs, manifest, workdir, runner, stdout, base_outputs, addon_outputs, account_id, env,
                         assets_dir=assets_dir)


def _deploy_chart(
    inputs: InstallInputs,
    manifest: ReleaseManifest,
    workdir: Path,
    runner: CommandRunner,
    stdout: TextIO,
    base_outputs: dict,
    addon_outputs: dict,
    account_id: str | None,
    env: Mapping[str, str] | None,
    *,
    assets_dir: Path | None = None,
) -> int:

    try:
        role_arn, gcp_service_account = _control_plane_identity(inputs, base_outputs, addon_outputs)
    except TerraformApplyRefused as error:
        print(f"Échec de la résolution d'identité : {error}", file=stdout)
        return 1

    chart_values = build_chart_values(
        inputs, manifest, control_plane_role_arn=role_arn,
        control_plane_gcp_service_account=gcp_service_account, aws_account_id=account_id,
    )
    values_path = workdir / "chart-values.generated.yaml"
    values_path.write_text(_yaml_dump(chart_values), encoding="utf-8")
    print("-> Générer les values Helm (stockage, identité résolue, control plane)", file=stdout)

    print("-> Valider le rendu de la chart (helm template)", file=stdout)
    result = runner.run(
        ("helm", "template", inputs.name, _source_path("chart", assets_dir), "--namespace", inputs.namespace, "-f", str(values_path)),
        env=env,
    )
    if not result.ok:
        print(result.stdout, file=stdout)
        print(result.stderr, file=stdout)
        print("Échec du rendu de la chart", file=stdout)
        return 1

    print("-> Installer la chart Quadringent", file=stdout)
    result = runner.run(
        (
            "helm", "upgrade", "--install", inputs.name, _source_path("chart", assets_dir), "--namespace", inputs.namespace,
            "--create-namespace", "-f", str(values_path),
        ),
        env=env,
    )
    if not result.ok:
        print(result.stdout, file=stdout)
        print(result.stderr, file=stdout)
        print("Échec de l'installation de la chart", file=stdout)
        return 1

    v2_enabled = bool(chart_values.get("controlPlane", {}).get("v2", {}).get("enabled", False))
    activation_token = None
    if v2_enabled:
        control_plane_deployment = f"deployment/{inputs.name}-quadringent-control-plane"
        print("-> Attendre le déploiement du control plane (kubectl rollout status)", file=stdout)
        result = runner.run(
            ("kubectl", "-n", inputs.namespace, "rollout", "status", control_plane_deployment, "--timeout=180s"),
            env=env,
        )
        if not result.ok:
            print(result.stdout, file=stdout)
            print(result.stderr, file=stdout)
            print(
                "Échec : le control plane n'est pas prêt (voir `kubectl -n "
                f"{inputs.namespace} get pods`). Corriger la cause puis relancer `quadringent install`, "
                "qui est idempotent.",
                file=stdout,
            )
            return 1
        print("-> Récupérer le jeton d'activation du premier admin", file=stdout)
        activation_token = fetch_first_admin_activation_token(inputs, runner, v2_enabled=True, env=env)

    print("", file=stdout)
    print(activation_message(inputs, activation_token=activation_token, workdir=workdir), file=stdout)
    return 0
