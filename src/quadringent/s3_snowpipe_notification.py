"""Build a lossless S3 notification update for the declared site Snowpipe."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Mapping, Sequence

from .site_config import SiteConfig


SUFFIX = ".jsonl"
_CONFIGURATION_GROUPS = (
    "QueueConfigurations",
    "TopicConfigurations",
    "LambdaFunctionConfigurations",
)


def _sqs_arn(site: SiteConfig) -> re.Pattern[str]:
    """Le canal SQS doit rester dans la région et le compte déclarés du site."""

    return re.compile(
        rf"^arn:aws:sqs:{re.escape(site.aws_region)}:{re.escape(site.aws_account_id)}"
        r":[A-Za-z0-9_-]+$"
    )


def _site(site: SiteConfig) -> SiteConfig:
    if not isinstance(site, SiteConfig):
        raise ValueError("the declared site configuration is required")
    if site.storage_backend != "aws":
        # Notifications S3 -> SQS -> Snowpipe : mécanisme AWS uniquement. Un
        # site storage_backend=gcs n'a ni bucket S3 ni file SQS — refus
        # explicite plutôt qu'un ARN vide qui ne correspondrait jamais à rien.
        raise ValueError(
            "les notifications S3 Snowpipe ne sont disponibles que pour "
            f"storage_backend=aws (site déclaré : {site.storage_backend!r})"
        )
    return site


def with_snowpipe_notification(
    current: Mapping[str, object], notification_channel: str, *, site: SiteConfig
) -> dict[str, object]:
    """Return an idempotent full replacement document without dropping entries."""

    site = _site(site)
    if not _sqs_arn(site).fullmatch(notification_channel):
        raise ValueError("notification_channel must be an AWS SQS ARN")
    lane_prefix = site.stream_prefix + "/"
    notification_id = site.snowpipe_notification_id()
    updated = deepcopy(dict(current))
    updated.pop("ResponseMetadata", None)
    desired = {
        "Id": notification_id,
        "QueueArn": notification_channel,
        "Events": ["s3:ObjectCreated:*"],
        "Filter": {
            "Key": {
                "FilterRules": [
                    {"Name": "prefix", "Value": lane_prefix},
                    {"Name": "suffix", "Value": SUFFIX},
                ]
            }
        },
    }
    for group in _CONFIGURATION_GROUPS:
        entries = updated.get(group, [])
        if not isinstance(entries, list):
            raise ValueError(f"invalid S3 notification group: {group}")
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ValueError(f"invalid S3 notification entry: {group}")
            if entry.get("Id") == notification_id:
                if group == "QueueConfigurations" and dict(entry) == desired:
                    return updated
                raise ValueError("the Quadringent notification id is already in use")
            if _overlaps_target(entry, lane_prefix):
                raise ValueError("an existing ObjectCreated notification overlaps the proof JSONL lane")
    queues = updated.setdefault("QueueConfigurations", [])
    if not isinstance(queues, list):
        raise ValueError("invalid S3 queue notification group")
    queues.append(desired)
    return updated


def without_snowpipe_notification(
    current: Mapping[str, object], *, site: SiteConfig
) -> dict[str, object]:
    """Remove only Quadringent's queue notification and preserve every peer."""

    notification_id = _site(site).snowpipe_notification_id()
    updated = deepcopy(dict(current))
    updated.pop("ResponseMetadata", None)
    queues = updated.get("QueueConfigurations")
    if queues is None:
        return updated
    if not isinstance(queues, list):
        raise ValueError("invalid S3 queue notification group")
    if any(not isinstance(entry, Mapping) for entry in queues):
        raise ValueError("invalid S3 queue notification entry")
    retained = [
        entry
        for entry in queues
        if entry.get("Id") != notification_id
    ]
    if retained:
        updated["QueueConfigurations"] = retained
    else:
        updated.pop("QueueConfigurations", None)
    return updated


def _overlaps_target(entry: Mapping[str, object], lane_prefix: str) -> bool:
    events = entry.get("Events", [])
    if not isinstance(events, list) or not any(
        isinstance(event, str) and event.startswith("s3:ObjectCreated:")
        for event in events
    ):
        return False
    prefix, suffix = _filter_bounds(entry)
    prefix_overlaps = lane_prefix.startswith(prefix) or prefix.startswith(lane_prefix)
    suffix_overlaps = SUFFIX.endswith(suffix) or suffix.endswith(SUFFIX)
    return prefix_overlaps and suffix_overlaps


def _filter_bounds(entry: Mapping[str, object]) -> tuple[str, str]:
    filter_value = entry.get("Filter")
    if not isinstance(filter_value, Mapping):
        return "", ""
    key = filter_value.get("Key")
    if not isinstance(key, Mapping):
        return "", ""
    rules = key.get("FilterRules", [])
    if not isinstance(rules, list):
        raise ValueError("invalid S3 notification filter")
    values: dict[str, str] = {}
    for rule in rules:
        if not isinstance(rule, Mapping):
            raise ValueError("invalid S3 notification filter rule")
        name = rule.get("Name")
        value = rule.get("Value")
        # AWS renvoie `Prefix` / `Suffix` ; l'API accepte les deux casses.
        if isinstance(name, str) and isinstance(value, str):
            lowered = name.lower()
            if lowered in ("prefix", "suffix"):
                values[lowered] = value
    return values.get("prefix", ""), values.get("suffix", "")


# --- Flotte : une notification par préfixe de table -------------------------

_TABLE = re.compile(r"^[A-Z0-9_]{1,64}$")


def lane_notification_id(table: str, *, site: SiteConfig) -> str:
    """Identifiant stable de la notification d'une table de flotte déclarée."""

    site = _site(site)
    if not isinstance(table, str) or _TABLE.fullmatch(table) is None:
        raise ValueError("invalid fleet table identifier")
    return site.snowpipe_notification_id(table)


def lane_prefix(table: str, *, site: SiteConfig) -> str:
    """Préfixe de journal d'une table, forme `<racine>/<table>/journal/`."""

    site = _site(site)
    if not isinstance(table, str) or _TABLE.fullmatch(table) is None:
        raise ValueError("invalid fleet table identifier")
    return site.journal_prefix_for(table) + "/"


def with_snowpipe_notifications(
    current: Mapping[str, object], lanes: Mapping[str, str], *, site: SiteConfig
) -> dict[str, object]:
    """Ajoute la notification de chaque table, sans perdre ni doublonner.

    `lanes` associe une table à son canal SQS. L'opération est idempotente :
    une notification déjà identique est conservée telle quelle, un
    identifiant réutilisé pour autre chose est refusé, et toute notification
    étrangère qui chevaucherait le préfixe visé fait échouer l'ensemble plutôt
    que de créer un double routage.
    """

    site = _site(site)
    if not lanes:
        raise ValueError("fleet notification lanes must not be empty")
    sqs_arn = _sqs_arn(site)
    desired_by_id: dict[str, dict[str, object]] = {}
    prefixes: list[str] = []
    for table, channel in lanes.items():
        identifier = lane_notification_id(table, site=site)
        prefix = lane_prefix(table, site=site)
        if not sqs_arn.fullmatch(str(channel)):
            raise ValueError("notification_channel must be an AWS SQS ARN")
        if identifier in desired_by_id:
            raise ValueError("duplicate fleet notification identifier")
        desired_by_id[identifier] = {
            "Id": identifier,
            "QueueArn": str(channel),
            "Events": ["s3:ObjectCreated:*"],
            "Filter": {
                "Key": {
                    "FilterRules": [
                        {"Name": "prefix", "Value": prefix},
                        {"Name": "suffix", "Value": SUFFIX},
                    ]
                }
            },
        }
        prefixes.append(prefix)

    updated = deepcopy(dict(current))
    updated.pop("ResponseMetadata", None)
    for group in _CONFIGURATION_GROUPS:
        entries = updated.get(group, [])
        if not isinstance(entries, list):
            raise ValueError(f"invalid S3 notification group: {group}")
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ValueError(f"invalid S3 notification entry: {group}")
            identifier = str(entry.get("Id", ""))
            if identifier in desired_by_id:
                if group == "QueueConfigurations" and _same_notification(
                    entry, desired_by_id[identifier]
                ):
                    continue
                raise ValueError("a fleet notification identifier is already in use")
            if _overlaps_any(entry, prefixes):
                raise ValueError(
                    "an existing ObjectCreated notification overlaps a fleet journal lane"
                )
    queues = updated.setdefault("QueueConfigurations", [])
    if not isinstance(queues, list):
        raise ValueError("invalid S3 queue notification group")
    existing_ids = {str(entry.get("Id", "")) for entry in queues if isinstance(entry, Mapping)}
    for identifier, document in desired_by_id.items():
        if identifier not in existing_ids:
            queues.append(document)
    return updated


def without_snowpipe_notifications(
    current: Mapping[str, object], tables: Sequence[str], *, site: SiteConfig
) -> dict[str, object]:
    """Retire uniquement les notifications de flotte demandées."""

    identifiers = {lane_notification_id(table, site=site) for table in tables}
    updated = deepcopy(dict(current))
    updated.pop("ResponseMetadata", None)
    queues = updated.get("QueueConfigurations")
    if queues is None:
        return updated
    if not isinstance(queues, list):
        raise ValueError("invalid S3 queue notification group")
    if any(not isinstance(entry, Mapping) for entry in queues):
        raise ValueError("invalid S3 queue notification entry")
    retained = [
        entry for entry in queues if str(entry.get("Id", "")) not in identifiers
    ]
    if retained:
        updated["QueueConfigurations"] = retained
    else:
        updated.pop("QueueConfigurations", None)
    return updated


def _same_notification(
    existing: Mapping[str, object], desired: Mapping[str, object]
) -> bool:
    """Compare une notification existante au document voulu.

    AWS normalise la casse des noms de regles : ce qui a ete ecrit `prefix`
    revient en `Prefix`. Comparer les documents bruts ferait donc echouer la
    deuxieme execution d'une installation pourtant identique — mesure du
    17/09. La comparaison porte sur les valeurs, pas sur la casse des noms.
    """

    if str(existing.get("Id", "")) != str(desired.get("Id", "")):
        return False
    if str(existing.get("QueueArn", "")) != str(desired.get("QueueArn", "")):
        return False
    if sorted(str(event) for event in existing.get("Events", []) or []) != sorted(
        str(event) for event in desired.get("Events", []) or []
    ):
        return False
    existing_prefix, existing_suffix = _filter_bounds(existing)
    desired_prefix, desired_suffix = _filter_bounds(desired)
    return (existing_prefix, existing_suffix) == (desired_prefix, desired_suffix)


def _overlaps_any(entry: Mapping[str, object], prefixes: Sequence[str]) -> bool:
    events = entry.get("Events", [])
    if not isinstance(events, list) or not any(
        isinstance(event, str) and event.startswith("s3:ObjectCreated:")
        for event in events
    ):
        return False
    prefix, suffix = _filter_bounds(entry)
    suffix_overlaps = SUFFIX.endswith(suffix) or suffix.endswith(SUFFIX)
    if not suffix_overlaps:
        return False
    return any(
        prefix.startswith(candidate) or candidate.startswith(prefix)
        for candidate in prefixes
    )
