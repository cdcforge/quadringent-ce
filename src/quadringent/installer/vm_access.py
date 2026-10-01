"""Kubeconfig privé et tunnels courts vers les VM k3s AWS/GCP."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
from typing import Iterator, Mapping

import yaml

from .runner import CommandRunner


class VmAccessError(RuntimeError):
    """The VM was provisioned, but its Kubernetes API is not reachable."""


def _aws_json(runner: CommandRunner, argv: tuple[str, ...], env: Mapping[str, str]) -> dict:
    result = runner.run(argv, env=env)
    if not result.ok:
        raise VmAccessError(f"AWS SSM a refusé {argv[2]} (code {result.returncode})")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise VmAccessError(f"réponse AWS SSM illisible pour {argv[2]}") from error
    if not isinstance(value, dict):
        raise VmAccessError(f"réponse AWS SSM inattendue pour {argv[2]}")
    return value


def _wait_online(instance_id: str, runner: CommandRunner, env: Mapping[str, str]) -> None:
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        payload = _aws_json(
            runner,
            ("aws", "ssm", "describe-instance-information", "--filters", f"Key=InstanceIds,Values={instance_id}", "--output", "json"),
            env,
        )
        if any(item.get("InstanceId") == instance_id and item.get("PingStatus") == "Online"
               for item in payload.get("InstanceInformationList", [])):
            return
        time.sleep(5)
    raise VmAccessError("la VM n'est pas enregistrée dans SSM après 5 minutes ; vérifier son réseau et son rôle IAM")


def _fetch_kubeconfig(instance_id: str, runner: CommandRunner, env: Mapping[str, str]) -> str:
    payload = _aws_json(
        runner,
        (
            "aws", "ssm", "send-command", "--instance-ids", instance_id,
            "--document-name", "AWS-RunShellScript",
            "--parameters", json.dumps({"commands": ["sudo cloud-init status --wait >/dev/null && sudo cat /etc/rancher/k3s/k3s.yaml"]}),
            "--output", "json",
        ),
        env,
    )
    command_id = payload.get("Command", {}).get("CommandId")
    if not isinstance(command_id, str) or not re.fullmatch(r"[a-f0-9-]{36}", command_id):
        raise VmAccessError("AWS SSM n'a pas renvoyé d'identifiant de commande")
    # Terraform reports the instance before user data finishes installing k3s.
    # The remote command waits for cloud-init, so allow its full startup window.
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        result = runner.run(
            ("aws", "ssm", "get-command-invocation", "--command-id", command_id,
             "--instance-id", instance_id, "--output", "json"), env=env,
        )
        if not result.ok:
            time.sleep(2)  # L'invocation peut ne pas être visible immédiatement.
            continue
        try:
            invocation = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise VmAccessError("réponse SSM get-command-invocation illisible") from error
        status = invocation.get("Status")
        if status == "Success":
            content = invocation.get("StandardOutputContent")
            if not isinstance(content, str) or not content.strip():
                raise VmAccessError("kubeconfig k3s vide dans la réponse SSM")
            return content
        if status in ("Failed", "Cancelled", "TimedOut", "Cancelling"):
            raise VmAccessError(f"lecture du kubeconfig k3s échouée via SSM ({status})")
        time.sleep(2)
    raise VmAccessError("la lecture du kubeconfig k3s a expiré après 5 minutes")


def _write_local_kubeconfig(content: str, path: Path, port: int) -> None:
    try:
        config = yaml.safe_load(content)
        clusters = config["clusters"]
        users = config["users"]
        if len(clusters) != 1 or not users:
            raise ValueError("structure kubeconfig inattendue")
        clusters[0]["cluster"]["server"] = f"https://127.0.0.1:{port}"
    except (TypeError, KeyError, IndexError, ValueError, yaml.YAMLError) as error:
        raise VmAccessError("kubeconfig k3s invalide") from error
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise VmAccessError("impossible d'écrire le kubeconfig k3s privé") from error
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        os.fchmod(output.fileno(), 0o600)
        yaml.safe_dump(config, output, sort_keys=False)


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextmanager
def connect_aws_vm(
    instance_id: str,
    workdir: Path,
    runner: CommandRunner,
    env: Mapping[str, str],
) -> Iterator[dict[str, str]]:
    """Yield subprocess env for Helm/kubectl; terminate the tunnel on exit.

    The kubeconfig contains a client key and is written with mode 0600. The
    SSM response and key are never printed or embedded in Terraform state.
    """

    if not isinstance(instance_id, str) or not re.fullmatch(r"i-[a-f0-9]{8,17}", instance_id):
        raise VmAccessError("sortie Terraform instance_id AWS absente ou invalide")
    _wait_online(instance_id, runner, env)
    content = _fetch_kubeconfig(instance_id, runner, env)
    port = _available_port()
    path = workdir / "k3s-kubeconfig.yaml"
    _write_local_kubeconfig(content, path, port)
    log_path = workdir / "ssm-port-forward.log"
    log_path.touch(mode=0o600)
    log_path.chmod(0o600)
    parameters = json.dumps({"portNumber": ["6443"], "localPortNumber": [str(port)]})
    try:
        process = runner.start(
            ("aws", "ssm", "start-session", "--target", instance_id,
             "--document-name", "AWS-StartPortForwardingSession", "--parameters", parameters),
            env=env, stdout_path=str(log_path),
        )
    except OSError as error:
        raise VmAccessError("impossible de démarrer le tunnel SSM") from error
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise VmAccessError("le tunnel SSM vers l'API k3s s'est arrêté avant d'être prêt")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            raise VmAccessError("le tunnel SSM vers l'API k3s n'est pas prêt après 60 secondes")
        yield {**env, "KUBECONFIG": str(path)}
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _fetch_gcp_kubeconfig(
    instance_name: str, zone: str, project: str, runner: CommandRunner, env: Mapping[str, str]
) -> str:
    """Lit la clé client via SSH sur IAP, sans l'écrire dans les journaux."""

    argv = (
        "gcloud", "compute", "ssh", instance_name, f"--project={project}", f"--zone={zone}",
        "--tunnel-through-iap", "--quiet", "--ssh-flag=-oBatchMode=yes",
        "--ssh-flag=-oConnectTimeout=10",
        "--command=sudo systemctl is-active --quiet k3s && sudo cat /etc/rancher/k3s/k3s.yaml",
    )
    # Terraform annonce la VM avant la fin du startup-script k3s. Réessayer
    # quelques minutes ; ne jamais joindre stderr (qui peut contenir du site)
    # ni stdout (qui contient la clé client) à l'exception.
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        result = runner.run(argv, env=env)
        if result.ok and result.stdout.strip():
            return result.stdout
        time.sleep(5)
    raise VmAccessError("k3s ou SSH IAP indisponible après 5 minutes")


@contextmanager
def connect_gcp_vm(
    instance_name: str,
    zone: str,
    project: str,
    workdir: Path,
    runner: CommandRunner,
    env: Mapping[str, str],
) -> Iterator[dict[str, str]]:
    """Ouvre SSH sur IAP (22/tcp) puis transfère l'API k3s en loopback.

    L'IAP ne contacte que le port 22 de la VM : aucune règle de pare-feu
    supplémentaire pour 6443 n'est nécessaire. Le kubeconfig reste en 0600.
    """

    if not isinstance(instance_name, str) or re.fullmatch(r"[a-z][a-z0-9-]{1,62}", instance_name) is None:
        raise VmAccessError("sortie Terraform instance_name GCP absente ou invalide")
    if not isinstance(zone, str) or re.fullmatch(r"[a-z][a-z0-9-]{2,62}", zone) is None:
        raise VmAccessError("sortie Terraform zone GCP absente ou invalide")
    if not isinstance(project, str) or re.fullmatch(r"[a-z][a-z0-9-]{4,62}", project) is None:
        raise VmAccessError("projet GCP absent ou invalide")
    content = _fetch_gcp_kubeconfig(instance_name, zone, project, runner, env)
    port = _available_port()
    path = workdir / "k3s-kubeconfig.yaml"
    _write_local_kubeconfig(content, path, port)
    log_path = workdir / "iap-port-forward.log"
    if log_path.is_symlink():
        raise VmAccessError("journal du tunnel IAP invalide")
    log_path.touch(mode=0o600)
    log_path.chmod(0o600)
    try:
        process = runner.start(
            (
                "gcloud", "compute", "ssh", instance_name, f"--project={project}", f"--zone={zone}",
                "--tunnel-through-iap", "--quiet", "--",
                "-N", "-o", "ExitOnForwardFailure=yes", "-L", f"127.0.0.1:{port}:127.0.0.1:6443",
            ),
            env=env, stdout_path=str(log_path),
        )
    except OSError as error:
        raise VmAccessError("impossible de démarrer le tunnel SSH IAP") from error
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise VmAccessError("le tunnel SSH IAP vers k3s s'est arrêté avant d'être prêt")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            raise VmAccessError("le tunnel SSH IAP vers k3s n'est pas prêt après 60 secondes")
        yield {**env, "KUBECONFIG": str(path)}
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
