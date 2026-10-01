"""Configuration d'un run de qualification (YAML ou JSON).

La configuration ne contient jamais de secret en clair : toute valeur
sensible (mot de passe source, clé d'entrepôt) est une référence — variable
d'environnement (``${VAR}``) ou chemin vers un fichier secret déjà déposé
hors dépôt — résolue au chargement. Une référence non résolue fait échouer le
chargement avec la liste complète des variables manquantes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from pathlib import Path
from typing import Any, Mapping

from .generator import DML_STEP_NAMES
from .schema import Column, TableSchema

_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_SAFE_RECEIVER = re.compile(r"[A-Za-z0-9_$#@]{1,128}\Z")
_SAFE_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}\Z")
_SAFE_USER = re.compile(r"[A-Za-z0-9_$#@]{1,128}\Z")
_SAFE_KEYCHAIN_SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9/_-]{0,127}\Z")
_SAFE_RUN_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,79}\Z")

KNOWN_STEPS: tuple[str, ...] = (*DML_STEP_NAMES, "snapshot", "capture", "rotate", "reconcile", "freshness")


class ConfigError(ValueError):
    """Configuration invalide, ou référence de secret non résolue."""


def _substitute(value: Any, env: Mapping[str, str], missing: set[str]) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in env:
                missing.add(name)
                return match.group(0)
            return env[name]

        return _PLACEHOLDER.sub(replace, value)
    if isinstance(value, dict):
        return {k: _substitute(v, env, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, env, missing) for v in value]
    return value


def _parse_document(text: str, fmt: str) -> dict[str, Any]:
    if fmt == "json":
        return json.loads(text)
    if fmt == "yaml":
        try:
            import yaml
        except ImportError as error:  # pragma: no cover - dépendance dev toujours présente en test
            raise ConfigError("PyYAML est requis pour charger une configuration YAML") from error
        return yaml.safe_load(text)
    raise ConfigError(f"format de configuration inconnu : {fmt!r} (attendu json|yaml)")


def _format_from_path(path: Path) -> str:
    return "yaml" if path.suffix in (".yml", ".yaml") else "json"


@dataclass(frozen=True)
class SourceConfig:
    driver: str
    library_whitelist: tuple[str, ...]
    connection_secret_file: str
    journal_library: str
    journal_name: str
    allow_dml: bool = False
    allow_rotation: bool = False
    host: str | None = None
    user: str | None = None
    source_time_zone: str | None = None


@dataclass(frozen=True)
class CaptureConfig:
    image: str
    max_seconds: int = 120
    max_consecutive_errors: int = 3


@dataclass(frozen=True)
class StorageConfig:
    backend: str  # "s3" | "gcs"
    bucket: str
    raw_prefix: str
    checkpoint_location: str | None = None  # DynamoDB table or GCS state bucket


@dataclass(frozen=True)
class WarehouseConfig:
    loader: str
    account_secret_file: str
    database: str
    schema_name: str


@dataclass(frozen=True)
class RunConfig:
    run_id: str
    table: TableSchema
    source: SourceConfig
    capture: CaptureConfig
    storage: StorageConfig
    warehouse: WarehouseConfig
    steps: tuple[str, ...]
    bootstrap_sequence: int | None = None
    bootstrap_receiver: str | None = None

    def resolved_steps(self, requested: str) -> tuple[str, ...]:
        """``"all"`` renvoie ``self.steps`` ; une liste ``"a,b,c"`` est filtrée et validée."""
        if requested == "all":
            return self.steps
        names = tuple(s.strip() for s in requested.split(",") if s.strip())
        if not names:
            raise ConfigError("aucune étape demandée pour ce run")
        unknown = [n for n in names if n not in KNOWN_STEPS]
        if unknown:
            raise ConfigError(f"étapes inconnues demandées : {unknown} (attendu parmi {KNOWN_STEPS})")
        not_configured = [n for n in names if n not in self.steps]
        if not_configured:
            raise ConfigError(f"étapes demandées absentes de la configuration du run : {not_configured}")
        return names


def _table_schema(raw: dict[str, Any]) -> TableSchema:
    columns = tuple(
        Column(
            name=c["name"], kind=c["kind"], length=c.get("length"),
            precision=c.get("precision"), scale=c.get("scale"),
            timestamp_precision=c.get("timestamp_precision", 6),
        )
        for c in raw["columns"]
    )
    return TableSchema(qualified_name=raw["qualified_name"], columns=columns, primary_key=raw["primary_key"])


def parse_config(text: str, *, fmt: str, env: Mapping[str, str]) -> RunConfig:
    """Charge et valide une configuration de run à partir de son texte.

    ``env`` fournit les valeurs des références ``${VAR}`` — typiquement
    ``os.environ`` en production, un mapping construit à la main en test.
    """
    raw = _parse_document(text, fmt)
    if not isinstance(raw, dict):
        raise ConfigError("la configuration doit être un document racine de type objet")
    missing: set[str] = set()
    resolved = _substitute(raw, env, missing)
    if missing:
        raise ConfigError(f"variables d'environnement non résolues : {sorted(missing)}")

    try:
        table = _table_schema(resolved["table"])
        source = SourceConfig(
            driver=resolved["source"]["driver"],
            library_whitelist=tuple(resolved["source"]["library_whitelist"]),
            connection_secret_file=resolved["source"]["connection_secret_file"],
            journal_library=resolved["source"]["journal_library"],
            journal_name=resolved["source"]["journal_name"],
            allow_dml=resolved["source"].get("allow_dml", False),
            allow_rotation=resolved["source"].get("allow_rotation", False),
            host=resolved["source"].get("host"),
            user=resolved["source"].get("user"),
            source_time_zone=resolved["source"].get("source_time_zone"),
        )
        capture = CaptureConfig(
            image=resolved["capture"]["image"],
            max_seconds=int(resolved["capture"].get("max_seconds", 120)),
            max_consecutive_errors=int(resolved["capture"].get("max_consecutive_errors", 3)),
        )
        storage = StorageConfig(
            backend=resolved["storage"]["backend"],
            bucket=resolved["storage"]["bucket"],
            raw_prefix=resolved["storage"]["raw_prefix"],
            checkpoint_location=resolved["storage"].get("checkpoint_location"),
        )
        warehouse = WarehouseConfig(
            loader=resolved["warehouse"]["loader"],
            account_secret_file=resolved["warehouse"]["account_secret_file"],
            database=resolved["warehouse"]["database"],
            schema_name=resolved["warehouse"]["schema"],
        )
        steps = tuple(resolved["steps"])
        run_id = resolved["run_id"]
    except KeyError as error:
        raise ConfigError(f"champ de configuration manquant : {error}") from error
    except ValueError as error:
        raise ConfigError("schéma de qualification invalide") from error

    unknown_steps = [s for s in steps if s not in KNOWN_STEPS]
    if not steps:
        raise ConfigError("aucune étape configurée pour ce run")
    if unknown_steps:
        raise ConfigError(f"étapes de configuration inconnues : {unknown_steps} (attendu parmi {KNOWN_STEPS})")
    if storage.backend not in ("s3", "gcs"):
        raise ConfigError(f"backend de stockage inconnu : {storage.backend!r} (attendu s3|gcs)")
    if not isinstance(run_id, str) or _SAFE_RUN_ID.fullmatch(run_id) is None:
        raise ConfigError("run_id doit être un identifiant minuscule borné, sans chemin")
    table_library = table.qualified_name.split(".", 1)[0].upper()
    if any(not isinstance(name, str) or _SAFE_IDENTIFIER.fullmatch(name) is None
           for name in (*source.library_whitelist, source.journal_library, source.journal_name)):
        raise ConfigError("identifiant de source ou de journal non sûr")
    if not source.library_whitelist or table_library not in {name.upper() for name in source.library_whitelist}:
        raise ConfigError("la whitelist source doit inclure la bibliothèque de la table de qualification")
    if type(source.allow_dml) is not bool or type(source.allow_rotation) is not bool:
        raise ConfigError("allow_dml et allow_rotation doivent être des booléens explicites")
    if not isinstance(source.connection_secret_file, str) or not source.connection_secret_file:
        raise ConfigError("la référence du secret source est invalide")
    if source.connection_secret_file.startswith("keychain:"):
        service = source.connection_secret_file.removeprefix("keychain:")
        if (_SAFE_KEYCHAIN_SERVICE.fullmatch(service) is None
                or not isinstance(source.host, str) or _SAFE_HOST.fullmatch(source.host) is None
                or not isinstance(source.user, str) or _SAFE_USER.fullmatch(source.user) is None):
            raise ConfigError("la source du trousseau exige un service, un hôte et un utilisateur valides")

    bootstrap_sequence = resolved.get("bootstrap_sequence")
    bootstrap_receiver = resolved.get("bootstrap_receiver")
    if (bootstrap_sequence is None) != (bootstrap_receiver is None):
        raise ConfigError("bootstrap_receiver et bootstrap_sequence doivent être fournis ensemble")
    if bootstrap_sequence is not None:
        if isinstance(bootstrap_sequence, bool) or not isinstance(bootstrap_sequence, int) or bootstrap_sequence < 0:
            raise ConfigError("bootstrap_sequence doit être un entier non négatif")
        if not isinstance(bootstrap_receiver, str) or _SAFE_RECEIVER.fullmatch(bootstrap_receiver) is None:
            raise ConfigError("bootstrap_receiver doit désigner un receiver")

    return RunConfig(
        run_id=run_id, table=table, source=source, capture=capture, storage=storage,
        warehouse=warehouse, steps=steps, bootstrap_sequence=bootstrap_sequence,
        bootstrap_receiver=bootstrap_receiver,
    )


def load_config(path: str | Path, *, env: Mapping[str, str]) -> RunConfig:
    """Charge une configuration depuis un fichier (YAML ou JSON selon l'extension)."""
    p = Path(path)
    return parse_config(p.read_text(encoding="utf-8"), fmt=_format_from_path(p), env=env)
