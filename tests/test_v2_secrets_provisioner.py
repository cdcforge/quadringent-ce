"""Provisionnement des Secrets Kubernetes depuis les identifiants chiffrés (tâche 3).

Déchiffre via ``SecretBox`` (même mécanisme que ``services/sources.py``/
``services/destinations.py``) et pousse vers un client Kubernetes factice —
jamais de réseau réel. Vérifie les noms déterministes partagés avec
``manifests.py`` et l'absence totale de valeur en clair dans les traces de
l'appelant (seuls les noms de Secret provisionnés sont retournés).
"""

from __future__ import annotations

import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.executor.manifests import (
    IBMI_CA_SECRET_KEY,
    destination_secret_ref,
    ibmi_ca_secret_ref,
    ibmi_secret_ref,
)
from quadringent_control_plane.v2.executor.secrets_provisioner import (
    DestinationNotFoundError,
    DestinationServiceIdentityMissingError,
    KEY_ISERIES_PASSWORD,
    KEY_SNOWFLAKE_ACCOUNT,
    KEY_SNOWFLAKE_PRIVATE_KEY_PEM,
    KEY_SNOWFLAKE_ROLE,
    KEY_SNOWFLAKE_USER,
    SecretsProvisioner,
    SourceNotFoundError,
)


class _FakeSecretsClient:
    def __init__(self) -> None:
        self.secrets: dict[str, dict[str, str]] = {}

    def upsert_secret(self, name: str, string_data: dict[str, str]) -> None:
        self.secrets[name] = dict(string_data)


@pytest.fixture()
def engine(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'secrets-provisioner.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def secret_box():
    return SecretBox(SecretBox.generate_key())


def test_provision_source_secret_decrypts_the_ibmi_password(engine, secret_box) -> None:
    ciphertext = secret_box.encrypt("un-mot-de-passe-tres-secret")
    with engine.begin() as connection:
        connection.execute(
            v2_schema.sources.insert(),
            {
                "id": "src1",
                "org_id": "default",
                "display_name": "Site principal",
                "ibmi_host": "as400.example.test",
                "ibmi_user": "QSVCUSER",
                "secret_ciphertext": ciphertext,
            },
        )
    client = _FakeSecretsClient()
    provisioner = SecretsProvisioner(engine, secret_box, secrets_client=client)

    name = provisioner.provision_source_secret("src1")

    assert name == ibmi_secret_ref("src1")
    assert client.secrets[name] == {KEY_ISERIES_PASSWORD: "un-mot-de-passe-tres-secret"}


def test_provision_destination_secret_decrypts_the_private_key(engine, secret_box) -> None:
    ciphertext = secret_box.encrypt('fixture-private-key')
    with engine.begin() as connection:
        connection.execute(
            v2_schema.destinations.insert(),
            {
                "id": "dst1",
                "org_id": "default",
                "snowflake_account": "acme-sf",
                "key_pair_ciphertext": ciphertext,
                "setup_script": "-- setup.sql",
                "service_user": "QDT_SVC_ABCD1234",
                "service_role": "QDT_ROLE_ABCD1234",
            },
        )
    client = _FakeSecretsClient()
    provisioner = SecretsProvisioner(engine, secret_box, secrets_client=client)

    name = provisioner.provision_destination_secret("dst1")

    assert name == destination_secret_ref("dst1")
    assert client.secrets[name][KEY_SNOWFLAKE_ACCOUNT] == "acme-sf"
    assert client.secrets[name][KEY_SNOWFLAKE_PRIVATE_KEY_PEM] == "fixture-private-key"
    assert client.secrets[name][KEY_SNOWFLAKE_USER] == "QDT_SVC_ABCD1234"
    assert client.secrets[name][KEY_SNOWFLAKE_ROLE] == "QDT_ROLE_ABCD1234"


def test_provision_destination_secret_fails_closed_without_service_identity(engine, secret_box) -> None:
    """Destination créée avant 0011_dest_service_identity (colonnes NULL) :
    refus explicite plutôt qu'un Secret Snowflake sans utilisateur/rôle."""

    ciphertext = secret_box.encrypt('fixture-private-key')
    with engine.begin() as connection:
        connection.execute(
            v2_schema.destinations.insert(),
            {
                "id": "dst-legacy",
                "org_id": "default",
                "snowflake_account": "acme-sf",
                "key_pair_ciphertext": ciphertext,
                "setup_script": "-- setup.sql",
            },
        )
    provisioner = SecretsProvisioner(engine, secret_box, secrets_client=_FakeSecretsClient())

    with pytest.raises(DestinationServiceIdentityMissingError):
        provisioner.provision_destination_secret("dst-legacy")


def test_provision_source_secret_unknown_source_fails_closed(engine, secret_box) -> None:
    provisioner = SecretsProvisioner(engine, secret_box, secrets_client=_FakeSecretsClient())
    with pytest.raises(SourceNotFoundError):
        provisioner.provision_source_secret("not-a-real-source")


def test_provision_destination_secret_unknown_destination_fails_closed(engine, secret_box) -> None:
    provisioner = SecretsProvisioner(engine, secret_box, secrets_client=_FakeSecretsClient())
    with pytest.raises(DestinationNotFoundError):
        provisioner.provision_destination_secret("not-a-real-destination")


def test_secret_names_match_the_ones_referenced_by_the_generated_manifests(engine, secret_box) -> None:
    # Garde-fou anti-régression : si les deux modules divergeaient, un
    # Deployment/Job référencerait un Secret jamais provisionné.
    assert ibmi_secret_ref("src1") == "qdt-source-src1"
    assert destination_secret_ref("dst1") == "qdt-destination-dst1"


# --- Secret CA épinglé (objectif B, suite chantier 2026-09-24) -------------


def _insert_source(engine, secret_box, *, source_id="src1", tls_pinned_pem=None) -> None:
    ciphertext = secret_box.encrypt("un-mot-de-passe-tres-secret")
    with engine.begin() as connection:
        connection.execute(
            v2_schema.sources.insert(),
            {
                "id": source_id,
                "org_id": "default",
                "display_name": "Site principal",
                "ibmi_host": "as400.example.test",
                "ibmi_user": "QSVCUSER",
                "secret_ciphertext": ciphertext,
                "tls_pinned_pem": tls_pinned_pem,
            },
        )


def test_provision_source_ca_secret_does_nothing_without_a_pin(engine, secret_box) -> None:
    _insert_source(engine, secret_box, tls_pinned_pem=None)
    client = _FakeSecretsClient()
    provisioner = SecretsProvisioner(engine, secret_box, secrets_client=client)

    name = provisioner.provision_source_ca_secret("src1")

    assert name is None
    assert client.secrets == {}


def test_provision_source_ca_secret_upserts_the_pinned_pem_under_a_stable_name(engine, secret_box) -> None:
    pem = "-----BEGIN CERTIFICATE-----\nPINNED\n-----END CERTIFICATE-----\n"
    _insert_source(engine, secret_box, tls_pinned_pem=pem)
    client = _FakeSecretsClient()
    provisioner = SecretsProvisioner(engine, secret_box, secrets_client=client)

    name = provisioner.provision_source_ca_secret("src1")

    assert name == ibmi_ca_secret_ref("src1")
    assert client.secrets[name] == {IBMI_CA_SECRET_KEY: pem}

    # Idempotent : un second appel avec le même PEM ne crée pas un second
    # Secret, il met à jour le même (upsert, comme le mot de passe IBM i).
    second_name = provisioner.provision_source_ca_secret("src1")
    assert second_name == name
    assert len(client.secrets) == 1


def test_provision_source_ca_secret_updates_when_the_pin_changes(engine, secret_box) -> None:
    first_pem = "-----BEGIN CERTIFICATE-----\nFIRST\n-----END CERTIFICATE-----\n"
    _insert_source(engine, secret_box, tls_pinned_pem=first_pem)
    client = _FakeSecretsClient()
    provisioner = SecretsProvisioner(engine, secret_box, secrets_client=client)
    name = provisioner.provision_source_ca_secret("src1")
    assert client.secrets[name][IBMI_CA_SECRET_KEY] == first_pem

    second_pem = "-----BEGIN CERTIFICATE-----\nROTATED\n-----END CERTIFICATE-----\n"
    with engine.begin() as connection:
        connection.execute(
            v2_schema.sources.update().where(v2_schema.sources.c.id == "src1").values(tls_pinned_pem=second_pem)
        )

    provisioner.provision_source_ca_secret("src1")
    assert client.secrets[name][IBMI_CA_SECRET_KEY] == second_pem


def test_provision_source_ca_secret_never_deletes_an_existing_secret_when_unpinned_afterwards(engine, secret_box) -> None:
    """Un dé-épinglage (tls_pinned_pem remis à NULL, ex. rotation en cours)
    ne doit jamais supprimer un Secret CA qu'une charge en cours référence
    encore — ce provisionneur ne supprime jamais, seulement upsert."""

    pem = "-----BEGIN CERTIFICATE-----\nPINNED\n-----END CERTIFICATE-----\n"
    _insert_source(engine, secret_box, tls_pinned_pem=pem)
    client = _FakeSecretsClient()
    provisioner = SecretsProvisioner(engine, secret_box, secrets_client=client)
    name = provisioner.provision_source_ca_secret("src1")
    assert name in client.secrets

    with engine.begin() as connection:
        connection.execute(
            v2_schema.sources.update().where(v2_schema.sources.c.id == "src1").values(tls_pinned_pem=None)
        )
    result = provisioner.provision_source_ca_secret("src1")

    assert result is None
    assert name in client.secrets, "jamais supprimé proactivement par le provisionneur"


def test_provision_source_ca_secret_unknown_source_fails_closed(engine, secret_box) -> None:
    provisioner = SecretsProvisioner(engine, secret_box, secrets_client=_FakeSecretsClient())
    with pytest.raises(SourceNotFoundError):
        provisioner.provision_source_ca_secret("not-a-real-source")
