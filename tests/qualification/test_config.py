"""Tests du chargement de configuration (config.py)."""

from __future__ import annotations

import pytest

from quadringent_qualification.config import ConfigError, parse_config

JSON_DOC = """
{
  "run_id": "${QUALIF_RUN_ID}",
  "table": {
    "qualified_name": "${QUALIF_LIBRARY}.QUALIF_ORDERS",
    "primary_key": "ORDER_ID",
    "columns": [
      {"name": "ORDER_ID", "kind": "integer"},
      {"name": "LABEL", "kind": "varchar", "length": 40},
      {"name": "CODE", "kind": "char", "length": 8},
      {"name": "AMOUNT", "kind": "decimal", "precision": 11, "scale": 2},
      {"name": "EVENT_DATE", "kind": "date"},
      {"name": "UPDATED_AT", "kind": "timestamp", "timestamp_precision": 6},
      {"name": "NOTE", "kind": "varchar", "length": 80}
    ]
  },
  "source": {
    "driver": "ibmi_java",
    "library_whitelist": ["${QUALIF_LIBRARY}"],
    "connection_secret_file": "${QUALIF_SOURCE_SECRET_FILE}",
    "journal_library": "${QUALIF_LIBRARY}",
    "journal_name": "QUALJRN"
  },
  "capture": {"image": "${QUALIF_CAPTURE_IMAGE}", "max_seconds": 120},
  "storage": {"backend": "gcs", "bucket": "${QUALIF_BUCKET}", "raw_prefix": "qualification/${QUALIF_RUN_ID}"},
  "warehouse": {
    "loader": "snowflake",
    "account_secret_file": "${QUALIF_SNOWFLAKE_SECRET_FILE}",
    "database": "${QUALIF_SF_DATABASE}",
    "schema": "${QUALIF_SF_SCHEMA}"
  },
  "steps": ["seed", "snapshot", "changes1", "capture", "reconcile"],
  "bootstrap_receiver": "QUALJRN0001",
  "bootstrap_sequence": 105
}
"""

ENV = {
    "QUALIF_RUN_ID": "qual-test-1", "QUALIF_LIBRARY": "QUALIF_LIB",
    "QUALIF_SOURCE_SECRET_FILE": "/secrets/source.json", "QUALIF_CAPTURE_IMAGE": "quadringent-capture:test",
    "QUALIF_BUCKET": "qualif-bucket", "QUALIF_SNOWFLAKE_SECRET_FILE": "/secrets/sf.json",
    "QUALIF_SF_DATABASE": "QUALIF_DB", "QUALIF_SF_SCHEMA": "QUALIF_SCHEMA",
}


def test_parse_config_substitutes_env_placeholders():
    config = parse_config(JSON_DOC, fmt="json", env=ENV)
    assert config.run_id == "qual-test-1"
    assert config.table.qualified_name == "QUALIF_LIB.QUALIF_ORDERS"
    assert config.storage.raw_prefix == "qualification/qual-test-1"
    assert config.bootstrap_sequence == 105
    assert config.bootstrap_receiver == "QUALJRN0001"


def test_run_id_cannot_escape_the_isolated_run_directory():
    for value in ("../outside", "UPPER", "bad/slash", "", "a" * 81):
        with pytest.raises(ConfigError, match="run_id"):
            parse_config(JSON_DOC, fmt="json", env={**ENV, "QUALIF_RUN_ID": value})


def test_checkpoint_location_is_part_of_the_real_storage_contract():
    doc = JSON_DOC.replace(
        '"raw_prefix": "qualification/${QUALIF_RUN_ID}"',
        '"raw_prefix": "qualification/${QUALIF_RUN_ID}", '
        '"checkpoint_location": "${QUALIF_CHECKPOINT_LOCATION}"',
    )
    config = parse_config(doc, fmt="json", env={**ENV, "QUALIF_CHECKPOINT_LOCATION": "qual-state"})
    assert config.storage.checkpoint_location == "qual-state"


def test_bootstrap_requires_receiver_and_sequence_together():
    missing_receiver = JSON_DOC.replace('  "bootstrap_receiver": "QUALJRN0001",\n', "")
    with pytest.raises(ConfigError, match="bootstrap_receiver"):
        parse_config(missing_receiver, fmt="json", env=ENV)

    config = parse_config(JSON_DOC, fmt="json", env=ENV)
    assert config.bootstrap_receiver == "QUALJRN0001"


def test_source_whitelist_must_cover_the_qualification_table():
    document = JSON_DOC.replace('"library_whitelist": ["${QUALIF_LIBRARY}"]',
                                '"library_whitelist": ["OTHER_LIB"]')
    with pytest.raises(ConfigError, match="whitelist"):
        parse_config(document, fmt="json", env=ENV)


def test_source_mutation_flags_are_explicit_booleans():
    document = JSON_DOC.replace('"journal_name": "QUALJRN"',
                                '"journal_name": "QUALJRN", "allow_dml": true, "allow_rotation": false')
    config = parse_config(document, fmt="json", env=ENV)
    assert config.source.allow_dml is True
    assert config.source.allow_rotation is False
    for value in ('"true"', '1', 'null'):
        invalid = document.replace('"allow_dml": true', f'"allow_dml": {value}')
        with pytest.raises(ConfigError, match="booléens explicites"):
            parse_config(invalid, fmt="json", env=ENV)


def test_keychain_source_requires_valid_host_and_user():
    keychain_env = {**ENV, "QUALIF_SOURCE_SECRET_FILE": "keychain:" + "Qualification/Test"}
    document = JSON_DOC.replace(
        '"journal_name": "QUALJRN"',
        '"journal_name": "QUALJRN", "host": "ibmi.example", "user": "QUALUSER"',
    )
    config = parse_config(document, fmt="json", env=keychain_env)
    assert config.source.host == "ibmi.example"
    assert config.source.user == "QUALUSER"
    for invalid in (
        JSON_DOC,
        document.replace('"user": "QUALUSER"', '"user": "BAD USER"'),
        document.replace('"host": "ibmi.example"', '"host": "bad host"'),
    ):
        with pytest.raises(ConfigError, match="trousseau"):
            parse_config(invalid, fmt="json", env=keychain_env)


def test_parse_config_missing_env_var_raises_with_all_missing_names():
    partial_env = {k: v for k, v in ENV.items() if k != "QUALIF_BUCKET"}
    with pytest.raises(ConfigError, match="QUALIF_BUCKET"):
        parse_config(JSON_DOC, fmt="json", env=partial_env)


def test_parse_config_rejects_unknown_storage_backend():
    doc = JSON_DOC.replace('"backend": "gcs"', '"backend": "azure"')
    with pytest.raises(ConfigError, match="backend de stockage inconnu"):
        parse_config(doc, fmt="json", env=ENV)


def test_parse_config_rejects_unknown_step():
    doc = JSON_DOC.replace('"seed", "snapshot"', '"bogus", "snapshot"')
    with pytest.raises(ConfigError, match="étapes de configuration inconnues"):
        parse_config(doc, fmt="json", env=ENV)


def test_parse_config_missing_field_raises_config_error():
    doc = JSON_DOC.replace('"run_id": "${QUALIF_RUN_ID}",', "")
    with pytest.raises(ConfigError, match="champ de configuration manquant"):
        parse_config(doc, fmt="json", env=ENV)


def test_parse_config_unknown_format_raises():
    with pytest.raises(ConfigError, match="format de configuration inconnu"):
        parse_config("{}", fmt="toml", env=ENV)


def test_parse_config_non_object_root_raises():
    with pytest.raises(ConfigError, match="objet"):
        parse_config("[1, 2, 3]", fmt="json", env=ENV)


def test_resolved_steps_all_returns_configured_steps():
    config = parse_config(JSON_DOC, fmt="json", env=ENV)
    assert config.resolved_steps("all") == ("seed", "snapshot", "changes1", "capture", "reconcile")


def test_resolved_steps_subset_filters_and_validates_order_preserved():
    config = parse_config(JSON_DOC, fmt="json", env=ENV)
    assert config.resolved_steps("seed,reconcile") == ("seed", "reconcile")


def test_resolved_steps_rejects_step_not_in_run_config():
    config = parse_config(JSON_DOC, fmt="json", env=ENV)
    with pytest.raises(ConfigError, match="absentes de la configuration"):
        config.resolved_steps("rotate")


def test_resolved_steps_rejects_completely_unknown_step():
    config = parse_config(JSON_DOC, fmt="json", env=ENV)
    with pytest.raises(ConfigError, match="étapes inconnues demandées"):
        config.resolved_steps("bogus")


def test_empty_step_selection_cannot_be_a_successful_run():
    config = parse_config(JSON_DOC, fmt="json", env=ENV)
    with pytest.raises(ConfigError, match="aucune étape"):
        config.resolved_steps(" , ")


def test_empty_configured_steps_are_rejected():
    doc = JSON_DOC.replace('"steps": ["seed", "snapshot", "changes1", "capture", "reconcile"]',
                           '"steps": []')
    with pytest.raises(ConfigError, match="aucune étape"):
        parse_config(doc, fmt="json", env=ENV)


def test_parse_config_yaml_format():
    yaml_doc = """
run_id: qual-test-1
table:
  qualified_name: QUALIF_LIB.QUALIF_ORDERS
  primary_key: ORDER_ID
  columns:
    - {name: ORDER_ID, kind: integer}
source:
  driver: ibmi_java
  library_whitelist: [QUALIF_LIB]
  connection_secret_file: /secrets/source.json
  journal_library: QUALIF_LIB
  journal_name: QUALJRN
capture:
  image: quadringent-capture:test
storage:
  backend: gcs
  bucket: qualif-bucket
  raw_prefix: qualification/qual-test-1
warehouse:
  loader: snowflake
  account_secret_file: /secrets/sf.json
  database: QUALIF_DB
  schema: QUALIF_SCHEMA
steps: [seed]
"""
    config = parse_config(yaml_doc, fmt="yaml", env={})
    assert config.run_id == "qual-test-1"
    assert config.capture.max_seconds == 120
