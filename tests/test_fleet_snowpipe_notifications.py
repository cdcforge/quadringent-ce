"""Notifications S3 par table de flotte, sans perdre ni doubler un fil."""

from __future__ import annotations

import site_fixture

import json

import pytest

from quadringent.s3_snowpipe_notification import (
    lane_notification_id,
    lane_prefix,
    with_snowpipe_notifications,
    without_snowpipe_notifications,
)

SITE = site_fixture.build_test_site()

CHANNEL = f"arn:aws:sqs:{SITE.aws_region}:{SITE.aws_account_id}:sf-snowpipe-ACME"
LANES = {"ORDER": CHANNEL, "HOLIDAYS": CHANNEL}
LEGACY_SALE = {
    "Id": SITE.snowpipe_notification_id("SALE"),
    "QueueArn": CHANNEL,
    "Events": ["s3:ObjectCreated:*"],
    "Filter": {
        "Key": {
            "FilterRules": [
                {"Name": "Prefix", "Value": f"{SITE.stream_prefix}/"},
                {"Name": "Suffix", "Value": ".jsonl"},
            ]
        }
    },
}


def empty_bucket() -> dict:
    return {"ResponseMetadata": {"HTTPStatusCode": 200}}


def with_sale() -> dict:
    return {"QueueConfigurations": [dict(LEGACY_SALE)], "ResponseMetadata": {}}


def test_lane_naming_is_derived_from_the_table_only() -> None:
    # Le manifeste de flotte est en majuscules : l'identifiant reste strict
    # pour qu'aucune variante de casse ne puisse designer deux voies.
    assert lane_notification_id("ORDER", site=SITE) == SITE.snowpipe_notification_id("ORDER")
    assert lane_prefix("HOLIDAYS", site=SITE) == f"{SITE.journal_prefix_for('HOLIDAYS')}/"


def test_unsafe_table_identifier_is_refused() -> None:
    for table in ("order/journal", "ORDER*", "", "ORDER "):
        with pytest.raises(ValueError):
            lane_notification_id(table, site=SITE)


def test_each_lane_gets_its_own_prefix_and_suffix() -> None:
    updated = with_snowpipe_notifications(empty_bucket(), LANES, site=SITE)
    entries = {entry["Id"]: entry for entry in updated["QueueConfigurations"]}
    order = entries[SITE.snowpipe_notification_id("ORDER")]
    rules = order["Filter"]["Key"]["FilterRules"]
    assert {"Name": "prefix", "Value": f"{SITE.journal_prefix_for('ORDER')}/"} in rules
    assert {"Name": "suffix", "Value": ".jsonl"} in rules
    assert order["QueueArn"] == CHANNEL


def test_existing_notifications_are_preserved() -> None:
    updated = with_snowpipe_notifications(with_sale(), LANES, site=SITE)
    ids = [entry["Id"] for entry in updated["QueueConfigurations"]]
    assert SITE.snowpipe_notification_id("SALE") in ids
    assert len(ids) == 3


def test_application_is_idempotent() -> None:
    once = with_snowpipe_notifications(with_sale(), LANES, site=SITE)
    twice = with_snowpipe_notifications(once, LANES, site=SITE)
    assert once == twice


def test_a_reused_identifier_with_other_content_is_refused() -> None:
    conflicting = {
        "QueueConfigurations": [
            {
                "Id": SITE.snowpipe_notification_id("ORDER"),
                "QueueArn": CHANNEL,
                "Events": ["s3:ObjectCreated:*"],
                "Filter": {
                    "Key": {
                        "FilterRules": [
                            {"Name": "prefix", "Value": f"{SITE.raw_prefix_root}/autre/journal/"},
                        ]
                    }
                },
            }
        ]
    }
    with pytest.raises(ValueError):
        with_snowpipe_notifications(conflicting, LANES, site=SITE)


def test_a_foreign_overlapping_notification_stops_the_batch() -> None:
    foreign = {
        "QueueConfigurations": [
            {
                "Id": "someone-else",
                "QueueArn": CHANNEL,
                "Events": ["s3:ObjectCreated:*"],
                "Filter": {
                    "Key": {
                        "FilterRules": [
                            # Majuscules comme les renvoie AWS : le chevauchement
                            # doit etre vu malgre la casse du nom de la regle.
                            {"Name": "Prefix", "Value": f"{SITE.raw_prefix_root}/order/"},
                            {"Name": "Suffix", "Value": ".jsonl"},
                        ]
                    }
                },
            }
        ]
    }
    with pytest.raises(ValueError):
        with_snowpipe_notifications(foreign, LANES, site=SITE)


def test_an_unrelated_foreign_notification_is_kept() -> None:
    other = {
        "QueueConfigurations": [
            {
                "Id": "someone-else",
                "QueueArn": CHANNEL,
                "Events": ["s3:ObjectCreated:*"],
                "Filter": {
                    "Key": {
                        "FilterRules": [
                            {"Name": "Prefix", "Value": "autre-product/"},
                            {"Name": "Suffix", "Value": ".jsonl"},
                        ]
                    }
                },
            }
        ]
    }
    updated = with_snowpipe_notifications(other, LANES, site=SITE)
    assert len(updated["QueueConfigurations"]) == 3


def test_rollback_removes_only_the_requested_lanes() -> None:
    installed = with_snowpipe_notifications(with_sale(), LANES, site=SITE)
    rolled = without_snowpipe_notifications(installed, list(LANES), site=SITE)
    assert [entry["Id"] for entry in rolled["QueueConfigurations"]] == [SITE.snowpipe_notification_id("SALE")]


def test_empty_lane_request_is_refused() -> None:
    with pytest.raises(ValueError):
        with_snowpipe_notifications(empty_bucket(), {}, site=SITE)


def test_an_installed_lane_is_recognised_despite_rule_name_case() -> None:
    """AWS normalise `prefix` en `Prefix` : la deuxieme execution doit passer.

    Sans cette normalisation, reinstaller une notification deja posee levait
    « identifier is already in use », alors que le document etait identique.
    """

    installed = with_snowpipe_notifications(empty_bucket(), LANES, site=SITE)
    # AWS renvoie les noms de regles capitalises.
    normalized = json.loads(json.dumps(installed))
    for entry in normalized["QueueConfigurations"]:
        rules = entry["Filter"]["Key"]["FilterRules"]
        for rule in rules:
            rule["Name"] = rule["Name"].capitalize()
    again = with_snowpipe_notifications(normalized, LANES, site=SITE)
    assert [entry["Id"] for entry in again["QueueConfigurations"]] == [
        entry["Id"] for entry in normalized["QueueConfigurations"]
    ]


def test_a_genuinely_different_lane_is_still_refused() -> None:
    installed = with_snowpipe_notifications(empty_bucket(), LANES, site=SITE)
    tampered = json.loads(json.dumps(installed))
    tampered["QueueConfigurations"][0]["QueueArn"] = (
        f"arn:aws:sqs:{SITE.aws_region}:{SITE.aws_account_id}:autre-file"
    )
    with pytest.raises(ValueError):
        with_snowpipe_notifications(tampered, LANES, site=SITE)
