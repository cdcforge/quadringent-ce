"""Exécution injectable des commandes externes de l'installateur.

Toute la logique de plan (``plan.py``, ``apply.py``) et le CLI (``cli.py``)
passent par un ``CommandRunner`` pour lancer terraform/helm/aws/gcloud/
kubectl. En test, un ``RecordingRunner`` remplace le sous-processus réel :
aucune commande externe n'est jamais exécutée pendant la suite de tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import shutil
import subprocess
from typing import Mapping, Protocol, Sequence


class ProcessHandle(Protocol):
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


@dataclass(frozen=True)
class CommandResult:
    """Résultat normalisé d'une commande externe."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandRunner(Protocol):
    """Interface minimale injectée dans le plan d'installation."""

    def run(
        self, argv: Sequence[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None
    ) -> CommandResult: ...

    def which(self, tool: str) -> str | None: ...

    def start(
        self, argv: Sequence[str], *, env: Mapping[str, str] | None = None,
        stdout_path: str,
    ) -> ProcessHandle: ...


class SubprocessRunner:
    """Exécution réelle, utilisée uniquement par le CLI hors ``--dry-run``.

    Jamais utilisé par la suite de tests : les tests injectent toujours
    ``RecordingRunner`` pour rester hors ligne.
    """

    def run(
        self, argv: Sequence[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None
    ) -> CommandResult:
        import os

        full_env = {**os.environ, **env} if env else None
        completed = subprocess.run(
            list(argv), cwd=cwd, env=full_env, capture_output=True, text=True, check=False
        )
        return CommandResult(tuple(argv), completed.returncode, completed.stdout, completed.stderr)

    def which(self, tool: str) -> str | None:
        return shutil.which(tool)

    def start(
        self, argv: Sequence[str], *, env: Mapping[str, str] | None = None,
        stdout_path: str,
    ) -> ProcessHandle:
        import os

        full_env = {**os.environ, **env} if env else None
        # Garder les diagnostics du tunnel dans un fichier privé ; refuser
        # un lien symbolique préexistant dans l'espace de travail du site.
        descriptor = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        os.fchmod(descriptor, 0o600)
        output = os.fdopen(descriptor, "a", encoding="utf-8")
        try:
            process = subprocess.Popen(
                list(argv), env=full_env, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, text=True,
            )
        finally:
            output.close()
        return process


@dataclass
class RecordingRunner:
    """Runner de test : n'exécute rien, enregistre les appels et rejoue des
    résultats préconfigurés (par défaut : succès vide pour toute commande).
    """

    available_tools: frozenset[str] = field(default_factory=lambda: frozenset({"terraform", "helm", "kubectl", "aws", "session-manager-plugin"}))
    scripted_results: dict[tuple[str, ...], CommandResult] = field(default_factory=dict)
    default_returncode: int = 0
    calls: list[CommandResult] = field(default_factory=list, compare=False)
    # (argv, cwd, env) de chaque appel, dans l'ordre — pour les tests qui
    # doivent vérifier *où* et avec quel environnement une commande a couru
    # (ex. AWS_PROFILE, localisation de l'état Terraform), pas seulement
    # l'argv. ``calls`` reste inchangé pour la compatibilité des tests
    # existants.
    invocations: list[tuple[tuple[str, ...], str | None, dict[str, str] | None]] = field(
        default_factory=list, compare=False
    )

    def run(
        self, argv: Sequence[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None
    ) -> CommandResult:
        key = tuple(argv)
        result = self.scripted_results.get(key)
        if result is None:
            result = CommandResult(key, self.default_returncode, "", "")
        self.calls.append(result)
        self.invocations.append((key, cwd, dict(env) if env else None))
        return result

    def which(self, tool: str) -> str | None:
        return f"/usr/bin/{tool}" if tool in self.available_tools else None

    def start(
        self, argv: Sequence[str], *, env: Mapping[str, str] | None = None,
        stdout_path: str,
    ) -> ProcessHandle:
        raise AssertionError("un test hors ligne ne doit pas lancer de tunnel VM réel")
