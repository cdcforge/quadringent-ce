"""Le nom du document de preuve autonome fait partie du contrat IAM du site."""

from __future__ import annotations

import pytest

import site_fixture
from quadringent.site_config import SiteConfigurationError, from_environment


def _environ(**overrides: str) -> dict[str, str]:
    environ = dict(site_fixture.TEST_SITE_ENV)
    environ.update(overrides)
    return environ


def test_autonomous_proof_name_defaults_to_product_document() -> None:
    site = from_environment(_environ())
    assert site.autonomous_proof_name == "quadringent-autonomous-latest.json"
    assert site.autonomous_proof_key.endswith(
        "/proofs/quadringent-autonomous-latest.json"
    )
    assert site.autonomous_proof_s3_uri.startswith("s3://acme-000000000001-test-ibmi-raw/")


def test_autonomous_proof_name_follows_site_declaration() -> None:
    """Un site dont l'IAM n'autorise qu'une clé existante la déclare ici."""
    site = from_environment(
        _environ(QUADRINGENT_AUTONOMOUS_PROOF_NAME="legacy-proof-name.json")
    )
    assert site.autonomous_proof_key.endswith("/proofs/legacy-proof-name.json")


@pytest.mark.parametrize(
    "name",
    [
        "../escape.json",
        "nested/proof.json",
        "PROOF.JSON",
        "proof",
        "x" * 200 + ".json",
    ],
)
def test_autonomous_proof_name_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(SiteConfigurationError):
        from_environment(_environ(QUADRINGENT_AUTONOMOUS_PROOF_NAME=name))


def test_destination_prefix_defaults_to_product_names() -> None:
    site = from_environment(_environ())
    assert site.destination_prefix == "QUADRINGENT"
    assert site.warehouse_name.endswith("_WH")
    assert site.warehouse_name.startswith("QUADRINGENT_")
    assert site.verifier_role_name == f"QUADRINGENT_{site.fleet_environment}_VERIFIER_ROLE"
    assert site.snowflake_raw_table_for(site.proof_table).startswith("QUADRINGENT_")


def test_destination_prefix_follows_site_declaration() -> None:
    """Un site dont les objets Snowflake existent sous un ancien préfixe le
    déclare — même contrat d'infra que les ServiceAccounts et la preuve."""
    site = from_environment(_environ(QUADRINGENT_DESTINATION_PREFIX="CDC_FORGE"))
    table = site.proof_table
    assert site.snowflake_raw_table_for(table) == f"CDC_FORGE_{table}_RAW"
    assert site.snowflake_canonical_for(table) == f"CDC_FORGE_{table}_CANONICAL"
    assert site.snowflake_pipe_for(table) == f"CDC_FORGE_{table}_PIPE"
    assert site.warehouse_name == f"CDC_FORGE_{site.fleet_environment}_WH"
    assert site.verifier_role_name == (
        f"CDC_FORGE_{site.fleet_environment}_VERIFIER_ROLE"
    )


def test_destination_prefix_normalizes_to_uppercase() -> None:
    site = from_environment(_environ(QUADRINGENT_DESTINATION_PREFIX="legacy_co"))
    assert site.destination_prefix == "LEGACY_CO"


@pytest.mark.parametrize("prefix", ["1INVALID", "BAD-PREFIX", "x" * 70])
def test_destination_prefix_rejects_invalid_identifiers(prefix: str) -> None:
    with pytest.raises(SiteConfigurationError):
        from_environment(_environ(QUADRINGENT_DESTINATION_PREFIX=prefix))


# --- storage_backend : gap (c) de docs/product/install-default.md ----------
#
# site.awsAccountId/aws.region (chart) et QUADRINGENT_AWS_ACCOUNT_ID/
# QUADRINGENT_AWS_REGION (runtime) ne concernent que storage_backend=aws.


def test_storage_backend_defaults_to_aws_when_absent() -> None:
    """Compatibilité ascendante : un site déployé avant l'ajout de
    QUADRINGENT_STORAGE_BACKEND (absente du site fictif) continue de se
    comporter comme un site aws."""
    assert "QUADRINGENT_STORAGE_BACKEND" not in site_fixture.TEST_SITE_ENV
    site = from_environment(_environ())
    assert site.storage_backend == "aws"
    assert site.aws_account_id == "000000000001"
    assert site.aws_region == "us-east-1"


def test_gcs_backend_does_not_require_aws_account_or_region() -> None:
    environ = _environ(
        QUADRINGENT_STORAGE_BACKEND="gcs",
        QUADRINGENT_AWS_ACCOUNT_ID="",
        QUADRINGENT_AWS_REGION="",
        QUADRINGENT_CHECKPOINT_TABLE="",
    )
    site = from_environment(environ)
    assert site.storage_backend == "gcs"
    assert site.aws_account_id == ""
    assert site.aws_region == ""


def test_gcs_backend_rejects_a_declared_aws_account_id() -> None:
    with pytest.raises(SiteConfigurationError) as excinfo:
        from_environment(
            _environ(
                QUADRINGENT_STORAGE_BACKEND="gcs",
                QUADRINGENT_AWS_REGION="",
                QUADRINGENT_CHECKPOINT_TABLE="",
            )
        )
    assert "storage_backend=aws" in str(excinfo.value)


def test_gcs_backend_rejects_a_declared_aws_region() -> None:
    with pytest.raises(SiteConfigurationError) as excinfo:
        from_environment(
            _environ(
                QUADRINGENT_STORAGE_BACKEND="gcs",
                QUADRINGENT_AWS_ACCOUNT_ID="",
                QUADRINGENT_CHECKPOINT_TABLE="",
            )
        )
    assert "storage_backend=aws" in str(excinfo.value)


def test_aws_backend_still_requires_account_and_region() -> None:
    with pytest.raises(SiteConfigurationError):
        from_environment(_environ(QUADRINGENT_AWS_ACCOUNT_ID=""))
    with pytest.raises(SiteConfigurationError):
        from_environment(_environ(QUADRINGENT_AWS_REGION=""))


def test_storage_backend_rejects_unknown_value() -> None:
    with pytest.raises(SiteConfigurationError):
        from_environment(_environ(QUADRINGENT_STORAGE_BACKEND="azure"))


# --- QUADRINGENT_CHECKPOINT_TABLE : même gap (c), table DynamoDB backend aws
# uniquement (storage.checkpointBucket/QUADRINGENT_CHECKPOINT_BUCKET porte
# l'équivalent GCS, publié par la chart, pas par ce module) -----------------


def test_gcs_backend_does_not_require_a_checkpoint_table() -> None:
    environ = _environ(
        QUADRINGENT_STORAGE_BACKEND="gcs",
        QUADRINGENT_AWS_ACCOUNT_ID="",
        QUADRINGENT_AWS_REGION="",
        QUADRINGENT_CHECKPOINT_TABLE="",
    )
    site = from_environment(environ)
    assert site.storage_backend == "gcs"
    assert site.checkpoint_table == ""


def test_gcs_backend_rejects_a_declared_checkpoint_table() -> None:
    with pytest.raises(SiteConfigurationError) as excinfo:
        from_environment(
            _environ(
                QUADRINGENT_STORAGE_BACKEND="gcs",
                QUADRINGENT_AWS_ACCOUNT_ID="",
                QUADRINGENT_AWS_REGION="",
            )
        )
    assert "storage_backend=aws" in str(excinfo.value)


def test_aws_backend_still_requires_a_checkpoint_table() -> None:
    with pytest.raises(SiteConfigurationError):
        from_environment(_environ(QUADRINGENT_CHECKPOINT_TABLE=""))


def test_destination_mode_defaults_to_copy_merge_when_absent() -> None:
    """Compatibilité ascendante : un site déployé avant l'ajout de
    QUADRINGENT_DESTINATION_MODE continue d'utiliser le chemin COPY/MERGE."""
    assert "QUADRINGENT_DESTINATION_MODE" not in site_fixture.TEST_SITE_ENV
    site = from_environment(_environ())
    assert site.destination_mode == "copy_merge"
    assert site.streaming_profile_json is None


def test_destination_mode_streaming_requires_profile_json() -> None:
    with pytest.raises(SiteConfigurationError) as excinfo:
        from_environment(_environ(QUADRINGENT_DESTINATION_MODE="streaming"))
    assert "STREAMING_PROFILE_JSON" in str(excinfo.value)


def test_destination_mode_streaming_with_profile_json() -> None:
    site = from_environment(
        _environ(
            QUADRINGENT_DESTINATION_MODE="streaming",
            QUADRINGENT_STREAMING_PROFILE_JSON="/app/secrets/streaming-profile.json",
        )
    )
    assert site.destination_mode == "streaming"
    assert site.streaming_profile_json == "/app/secrets/streaming-profile.json"


def test_destination_mode_rejects_unknown_value() -> None:
    with pytest.raises(SiteConfigurationError):
        from_environment(_environ(QUADRINGENT_DESTINATION_MODE="dynamic_table"))


def test_streaming_profile_json_rejected_outside_streaming_mode() -> None:
    with pytest.raises(SiteConfigurationError) as excinfo:
        from_environment(
            _environ(QUADRINGENT_STREAMING_PROFILE_JSON="/app/secrets/streaming-profile.json")
        )
    assert "destination_mode=streaming" in str(excinfo.value)
