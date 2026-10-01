"""CLI ``quadringent`` : installateur mode par défaut (chantier 6).

Sous-commandes : ``install``, ``uninstall``, ``status``. Toute commande
externe passe par un ``CommandRunner`` injectable — voir ``runner.py``. Le
CLI réel (``main``) utilise ``SubprocessRunner`` ; les tests injectent
``RecordingRunner`` via ``run(argv, runner=..., stdout=...)``.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
from typing import Sequence, TextIO

from .apply import execute_install
from .manifest import InvalidReleaseManifest, ReleaseManifest
from .mcp_bridge import add_mcp_subcommand, run_mcp_command
from .plan import (
    agent_token_script,
    InstallInputs,
    InvalidInstallInputs,
    _addon_module_dir,
    _base_module_dir,
    _vm_module_dir,
    build_plan,
)
from .preflight import run_preflight
from .runner import CommandRunner, SubprocessRunner
from .v2_commands import add_v2_subcommands, run_v2_command
from .vm_access import VmAccessError, connect_aws_vm, connect_gcp_vm

DEFAULT_MANIFEST_PATH = Path("deploy/release-manifest.example.json")
STATE_FILENAME = "install-state.json"


def _default_workdir(name: str) -> Path:
    return Path.home() / ".quadringent" / name


def _write_state(workdir: Path, inputs: InstallInputs, status: str) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    state = {
        "cloud": inputs.cloud,
        "target": inputs.target,
        "region": inputs.region,
        "name": inputs.name,
        "namespace": inputs.namespace,
        "status": status,
    }
    if inputs.aws_profile:
        state["aws_profile"] = inputs.aws_profile
    if inputs.project:
        state["project"] = inputs.project
    (workdir / STATE_FILENAME).write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _read_state(workdir: Path) -> dict | None:
    state_path = workdir / STATE_FILENAME
    if not state_path.exists():
        return None
    return json.loads(state_path.read_text(encoding="utf-8"))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quadringent", description="Installateur Quadringent (mode par défaut).")
    subparsers = parser.add_subparsers(dest="command", required=True)

    install = subparsers.add_parser("install", help="Crée un site Quadringent (infra + chart).")
    install.add_argument("--cloud", required=True, choices=("aws", "gcp"))
    install.add_argument("--target", required=True, choices=("vm", "cluster"))
    install.add_argument("--region", required=True)
    install.add_argument("--name", required=True)
    install.add_argument("--namespace", default="quadringent")
    install.add_argument("--workdir", default=None, help="Répertoire privé de travail (défaut : ~/.quadringent/<name>)")
    install.add_argument("--release-manifest", default=str(DEFAULT_MANIFEST_PATH))
    install.add_argument(
        "--assets-dir", default=None,
        help="Racine du sdist de release extrait (contient chart/ et deploy/terraform/ ; défaut : dossier courant)",
    )
    install.add_argument("--image-repository", default=None, help="Surcharge QUADRINGENT_IMAGE_REPOSITORY")
    install.add_argument("--check-ibmi", default=None, metavar="HOST", help="Teste la joignabilité TLS IBM i (9471/9476/9475) avant d'installer")
    install.add_argument(
        "--existing-bucket", default=None, metavar="NAME",
        help="Réutilise un bucket S3/GCS déjà créé au lieu d'en créer un (deploy/terraform/<cloud>/base, existing_bucket_name)",
    )
    install.add_argument(
        "--existing-checkpoint-table", default=None, metavar="NAME",
        help="AWS uniquement : réutilise une table DynamoDB de checkpoints déjà créée au lieu d'en créer une (existing_checkpoint_table_name)",
    )
    install.add_argument(
        "--admin-email", default=None,
        help="Email du premier admin : l'installation affiche son lien d'activation à usage unique",
    )
    install.add_argument("--project", default=None, help="GCP uniquement, obligatoire : projet GCP réel (project_id)")
    install.add_argument(
        "--aws-profile", default=None, metavar="NAME",
        help="AWS uniquement, optionnel : profil de credentials local (AWS_PROFILE, transmis aux commandes terraform/aws/kubectl)",
    )
    install.add_argument("--vpc-id", default=None, help="VM AWS : VPC existant")
    install.add_argument("--subnet-id", default=None, help="VM AWS : sous-réseau existant")
    install.add_argument("--vm-instance-type", default=None, help="VM AWS : type EC2 x86_64 (défaut t3.medium)")
    install.add_argument("--gcp-network", default=None, help="VM GCP : réseau VPC existant")
    install.add_argument("--gcp-subnetwork", default=None, help="VM GCP : sous-réseau existant")
    install.add_argument("--gcp-zone", default=None, help="VM GCP : zone (défaut <region>-b)")
    install.add_argument("--image-pull-secret", default=None, help="Secret Kubernetes existant pour tirer les images d'un registre privé")
    install.add_argument("--eks-oidc-provider-arn", default=None, help="EKS : ARN IAM du fournisseur OIDC existant")
    install.add_argument("--eks-oidc-provider-url", default=None, help="EKS : URL du fournisseur OIDC sans https://")
    install.add_argument("--dry-run", action="store_true", help="Affiche le plan complet sans exécuter aucune commande")
    install.add_argument("--skip-preflight", action="store_true")
    install.add_argument(
        "--yes", action="store_true",
        help="Ne pas demander de confirmation avant d'appliquer chaque plan Terraform (toujours affiché en résumé)",
    )
    install.add_argument(
        "--allow-destroy", action="store_true",
        help="Autorise un plan Terraform qui détruit des ressources existantes (refusé par défaut)",
    )

    uninstall = subparsers.add_parser("uninstall", help="Retire la release Helm (n'efface pas l'infra Terraform).")
    uninstall.add_argument("--name", required=True)
    uninstall.add_argument("--namespace", default="quadringent")
    uninstall.add_argument("--workdir", default=None)
    uninstall.add_argument("--aws-profile", default=None, help="Profil AWS si différent de celui de l'installation")
    uninstall.add_argument("--dry-run", action="store_true")

    tunnel = subparsers.add_parser("vm-tunnel", help="Garde ouvert le tunnel privé vers la VM k3s AWS ou GCP.")
    tunnel.add_argument("--name", required=True)
    tunnel.add_argument("--workdir", default=None)
    tunnel.add_argument("--aws-profile", default=None)

    agent = subparsers.add_parser(
        "agent-token",
        help="Émet un jeton d'agent borné (kubectl exec) et l'écrit dans un fichier 0600, jamais à l'écran.",
    )
    agent.add_argument("--name", required=True)
    agent.add_argument("--namespace", default="quadringent")
    agent.add_argument("--label", required=True, help="Nom du jeton (visible dans l'audit)")
    agent.add_argument("--scope", default="operate", choices=("read", "operate", "admin"))
    agent.add_argument("--days", type=int, default=30, help="Durée de validité (1 à 90 jours)")
    agent.add_argument("--url", default="http://localhost:8844", help="URL du control plane (après tunnel)")
    agent.add_argument("--write-config", required=True, help="Fichier de configuration CLI/agents (créé en 0600)")

    status = subparsers.add_parser("status", help="Affiche l'état du dernier plan connu pour ce site.")
    status.add_argument("--name", required=True)
    status.add_argument("--workdir", default=None)

    # Client /v2 (contrat §5, tâche 19) : sources, destinations, tables,
    # pipelines, confirmations, tokens, users, audit, events, webhooks —
    # toutes sorties en JSON, voir v2_commands.py. QUADRINGENT_URL/
    # QUADRINGENT_TOKEN (ou ~/.quadringent/cli.json) configurent la cible.
    add_v2_subcommands(subparsers)

    # Pont MCP stdio<->streamable HTTP distant (contrat §4/§5, tâche 19).
    add_mcp_subcommand(subparsers)

    from quadringent.qualification.cli import add_parser
    add_parser(subparsers)
    return parser


def _resolve_workdir(name: str, override: str | None) -> Path:
    return Path(override) if override else _default_workdir(name)


def _run_install(args: argparse.Namespace, runner: CommandRunner, stdout: TextIO) -> int:
    try:
        inputs = InstallInputs(
            cloud=args.cloud,
            target=args.target,
            region=args.region,
            name=args.name,
            namespace=args.namespace,
            existing_bucket=args.existing_bucket,
            existing_checkpoint_table=args.existing_checkpoint_table,
            project=args.project,
            aws_profile=args.aws_profile,
            vpc_id=args.vpc_id,
            subnet_id=args.subnet_id,
            vm_instance_type=args.vm_instance_type,
            gcp_network=args.gcp_network,
            gcp_subnetwork=args.gcp_subnetwork,
            gcp_zone=args.gcp_zone,
            image_pull_secret=args.image_pull_secret,
            eks_oidc_provider_arn=args.eks_oidc_provider_arn,
            eks_oidc_provider_url=args.eks_oidc_provider_url,
            admin_email=args.admin_email,
        )
    except InvalidInstallInputs as error:
        print(f"Entrées invalides : {error}", file=stdout)
        return 2

    try:
        manifest = ReleaseManifest.from_file(Path(args.release_manifest), repository_override=args.image_repository)
    except InvalidReleaseManifest as error:
        print(f"Manifeste de version invalide : {error}", file=stdout)
        return 2

    workdir = _resolve_workdir(inputs.name, args.workdir)
    assets_dir = Path(args.assets_dir or ".").expanduser().resolve()
    module_dir = _vm_module_dir(inputs.cloud) if inputs.target == "vm" else _addon_module_dir(inputs.cloud)
    required_assets = (
        assets_dir / "chart" / "Chart.yaml",
        assets_dir / _base_module_dir(inputs.cloud) / "main.tf",
        assets_dir / module_dir / "main.tf",
    )
    missing_assets = [str(path) for path in required_assets if not path.is_file()]
    if missing_assets:
        print(
            "Artefacts d'installation introuvables : " + ", ".join(missing_assets) + ". "
            "Extraire le sdist quadringent-<version>.tar.gz de la même release et passer --assets-dir <racine extraite>.",
            file=stdout,
        )
        return 2
    # Conserver l'aperçu historique depuis le checkout ; une racine explicite
    # issue d'une release fournit des chemins absolus utilisables ailleurs.
    source_root = assets_dir if args.assets_dir is not None else None

    if args.dry_run:
        plan = build_plan(inputs, workdir, manifest, assets_dir=source_root)
        print(plan.render_text(), file=stdout)
        print("", file=stdout)
        print("(--dry-run : aucune commande exécutée, aucun fichier écrit)", file=stdout)
        return 0

    if not args.skip_preflight:
        results = run_preflight(inputs, runner, check_ibmi_host=args.check_ibmi)
        failed = [result for result in results if not result.ok]
        for result in results:
            marker = "OK " if result.ok else "KO "
            print(f"[{marker}] {result.name} : {result.detail}", file=stdout)
        if failed:
            print("", file=stdout)
            print("Pré-vol échoué : corriger les points KO avant de relancer install.", file=stdout)
            _write_state(workdir, inputs, "preflight_failed")
            return 1

    workdir.mkdir(parents=True, exist_ok=True)
    # L'exécution réelle a sa propre orchestration (apply.py) : les sorties
    # Terraform du socle ne sont connues qu'une fois réellement appliquées
    # (gap (3)) et chaque module tourne dans une copie privée de l'espace de
    # travail du site (gap (1)) — `plan.steps` n'est qu'un aperçu textuel
    # statique, imprimé par --dry-run, jamais rejoué tel quel ici.
    code = execute_install(
        inputs, manifest, workdir, runner, stdout,
        yes=args.yes, allow_destroy=args.allow_destroy,
        assets_dir=source_root,
    )
    _write_state(workdir, inputs, "installed" if code == 0 else "failed")
    return code


def _vm_connection(state: dict, workdir: Path, runner: CommandRunner, env: dict[str, str], outputs: dict):
    if state.get("cloud") == "aws":
        return connect_aws_vm(outputs["instance_id"]["value"], workdir, runner, env)
    if state.get("cloud") == "gcp":
        return connect_gcp_vm(
            outputs["instance_name"]["value"], outputs["zone"]["value"], state["project"],
            workdir, runner, env,
        )
    raise VmAccessError("cloud VM inconnu")


def _run_uninstall(args: argparse.Namespace, runner: CommandRunner, stdout: TextIO) -> int:
    workdir = _resolve_workdir(args.name, args.workdir)
    argv = ("helm", "uninstall", args.name, "--namespace", args.namespace)
    if args.dry_run:
        print(f"$ {' '.join(argv)}", file=stdout)
        print(
            "Rappel : les ressources Terraform (bucket, table de checkpoints, identités, VM) ne sont pas "
            "supprimées par cette commande — voir docs/product/install-default.md § Désinstallation.",
            file=stdout,
        )
        return 0
    state = _read_state(workdir)
    if not state or state.get("name") != args.name or state.get("namespace") != args.namespace:
        print("Installation inconnue ou namespace différent : désinstallation refusée.", file=stdout)
        return 2
    env: dict[str, str] = {}
    if state.get("cloud") == "aws":
        env = {"AWS_REGION": state["region"], "AWS_DEFAULT_REGION": state["region"]}
        profile = args.aws_profile or state.get("aws_profile")
        if profile:
            env["AWS_PROFILE"] = profile
    if state.get("target") == "vm":
        result = runner.run(("terraform", "output", "-json"), cwd=str(workdir / "terraform" / "vm"), env=env)
        if not result.ok:
            print("Sorties Terraform de la VM indisponibles : désinstallation refusée.", file=stdout)
            return 1
        try:
            outputs = json.loads(result.stdout)
            with _vm_connection(state, workdir, runner, env, outputs) as vm_env:
                result = runner.run(argv, env=vm_env)
        except (KeyError, TypeError, ValueError, VmAccessError) as error:
            print(f"Accès à la VM impossible : {error}", file=stdout)
            return 1
    else:
        result = runner.run(argv, env=env)
    print(result.stdout, file=stdout)
    if not result.ok:
        print(result.stderr, file=stdout)
        return 1
    state["status"] = "uninstalled"
    (workdir / STATE_FILENAME).write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


def _run_status(args: argparse.Namespace, stdout: TextIO) -> int:
    workdir = _resolve_workdir(args.name, args.workdir)
    state = _read_state(workdir)
    if state is None:
        print(f"Aucun état connu pour {args.name!r} dans {workdir}.", file=stdout)
        return 1
    print(json.dumps(state, indent=2, ensure_ascii=False), file=stdout)
    return 0


def _run_vm_tunnel(args: argparse.Namespace, runner: CommandRunner, stdout: TextIO) -> int:
    workdir = _resolve_workdir(args.name, args.workdir)
    state = _read_state(workdir)
    if not state or state.get("cloud") not in ("aws", "gcp") or state.get("target") != "vm":
        print("Aucune installation VM connue dans ce répertoire de travail.", file=stdout)
        return 2
    env: dict[str, str] = {}
    if state["cloud"] == "aws":
        env = {"AWS_REGION": state["region"], "AWS_DEFAULT_REGION": state["region"]}
        profile = args.aws_profile or state.get("aws_profile")
        if profile:
            env["AWS_PROFILE"] = profile
    result = runner.run(("terraform", "output", "-json"), cwd=str(workdir / "terraform" / "vm"), env=env)
    if not result.ok:
        print("Sorties Terraform de la VM indisponibles.", file=stdout)
        return 1
    try:
        outputs = json.loads(result.stdout)
        with _vm_connection(state, workdir, runner, env, outputs) as vm_env:
            kubeconfig = shlex.quote(vm_env["KUBECONFIG"])
            namespace = shlex.quote(str(state["namespace"]))
            release = shlex.quote(str(state["name"]))
            mode = "SSM" if state["cloud"] == "aws" else "IAP"
            print(f"Tunnel {mode} vers k3s actif. Dans un second terminal :", file=stdout, flush=True)
            print(
                f"KUBECONFIG={kubeconfig} kubectl -n {namespace} port-forward "
                f"deployment/{release}-quadringent-control-plane 8844:8844",
                file=stdout, flush=True,
            )
            print(f"Puis ouvrir http://127.0.0.1:8844 ; Ctrl+C ferme le tunnel {mode}.", file=stdout, flush=True)
            while True:
                time.sleep(1)
    except (KeyError, TypeError, ValueError, VmAccessError) as error:
        print(f"Tunnel VM indisponible : {error}", file=stdout)
        return 1
    except KeyboardInterrupt:
        mode = "SSM" if state["cloud"] == "aws" else "IAP"
        print(f"Tunnel {mode} fermé.", file=stdout)
        return 0


def _write_private_agent_config(path: Path, *, url: str, token: str) -> None:
    """Remplace la configuration après écriture privée, synchronisation et fermeture."""
    if path.is_symlink():
        raise OSError("la destination ne doit pas être un lien symbolique")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1  # Le flux possède désormais le descripteur.
            json.dump({"url": url, "token": token}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        # Revalider après l'écriture ; replace ne suit jamais le lien final.
        if path.is_symlink():
            raise OSError("la destination ne doit pas être un lien symbolique")
        os.replace(temporary_path, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def _run_agent_token(args: argparse.Namespace, runner: CommandRunner, stdout: TextIO) -> int:
    path = Path(args.write_config)
    if path.is_symlink():
        print("Configuration refusée : la destination est un lien symbolique.", file=stdout)
        return 2
    try:
        script = agent_token_script(args.label, args.scope, days=args.days)
    except InvalidInstallInputs as error:
        print(f"Entrées invalides : {error}", file=stdout)
        return 2
    result = runner.run((
        "kubectl", "-n", args.namespace, "exec", f"deployment/{args.name}-quadringent-control-plane",
        "-c", "control-plane-v2", "--",
        # -P : ne pas préfixer le répertoire courant (/app contient un
        # quadringent_control_plane.py qui masquerait le paquet).
        "python", "-P", "-c", script,
    ))
    if not result.ok:
        print("Échec de l'émission du jeton (kubectl exec) :", file=stdout)
        print(result.stderr[-2000:], file=stdout)
        return 1
    try:
        body = json.loads(result.stdout)
        token_id, token = body["id"], body["token"]
    except (ValueError, KeyError, TypeError):
        print("Réponse inattendue du control plane : jeton non enregistré", file=stdout)
        return 1
    try:
        _write_private_agent_config(path, url=args.url, token=token)
    except OSError:
        print("Écriture de la configuration impossible : fichier précédent conservé, jeton non affiché.", file=stdout)
        return 1
    print(json.dumps({"token_id": token_id, "scope": args.scope, "days": args.days, "config": str(path)}), file=stdout)
    return 0


def run(argv: Sequence[str], *, runner: CommandRunner | None = None, stdout: TextIO | None = None) -> int:
    """Point d'entrée testable : injecte le runner et le flux de sortie."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    runner = runner if runner is not None else SubprocessRunner()
    stdout = stdout if stdout is not None else sys.stdout

    if args.command == "qualification":
        from quadringent.qualification.cli import run_native
        return run_native(args.config,args.out_dir,args.phase,stdout=stdout)
    if args.command == "install":
        return _run_install(args, runner, stdout)
    if args.command == "uninstall":
        return _run_uninstall(args, runner, stdout)
    if args.command == "agent-token":
        return _run_agent_token(args, runner, stdout)
    if args.command == "status":
        return _run_status(args, stdout)
    if args.command == "vm-tunnel":
        return _run_vm_tunnel(args, runner, stdout)
    if args.command == "mcp":
        return run_mcp_command(args, stdout=stdout)
    if hasattr(args, "handler"):
        # Sous-commandes /v2 (sources, destinations, tables, pipelines,
        # actions, confirmations, tokens, users, audit, events, webhooks) —
        # voir v2_commands.py::add_v2_subcommands.
        return run_v2_command(args, stdout=stdout)
    return 2


def main() -> None:
    sys.exit(run(sys.argv[1:]))
