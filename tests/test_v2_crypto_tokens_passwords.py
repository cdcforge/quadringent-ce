"""Primitives cryptographiques des jetons d'agent et mots de passe (tâches 8, 9)."""

from __future__ import annotations

import pytest

from quadringent_control_plane.v2 import crypto


def test_generate_agent_token_has_expected_prefix_per_scope() -> None:
    for scope, prefix in (("read", "rd"), ("operate", "op"), ("admin", "ad")):
        token, returned_prefix = crypto.generate_agent_token(scope)
        assert returned_prefix == prefix
        assert token.startswith(f"qdt_{prefix}_")
        assert len(token) > len(f"qdt_{prefix}_")


def test_generate_agent_token_rejects_unknown_scope() -> None:
    with pytest.raises(ValueError):
        crypto.generate_agent_token("superadmin")


def test_hash_agent_token_is_deterministic_and_pepper_dependent() -> None:
    token, _ = crypto.generate_agent_token("operate")
    hash_a = crypto.hash_agent_token(token, b"pepper-a")
    hash_b = crypto.hash_agent_token(token, b"pepper-a")
    hash_c = crypto.hash_agent_token(token, b"pepper-b")
    assert hash_a == hash_b
    assert hash_a != hash_c
    assert token not in hash_a


def test_load_token_pepper_fails_closed_without_environment() -> None:
    with pytest.raises(crypto.TokenPepperUnavailableError):
        crypto.load_token_pepper(env={})


def test_load_token_pepper_reads_inline_env_value() -> None:
    pepper = crypto.load_token_pepper(env={crypto.ENV_TOKEN_PEPPER: "un-pepper-secret"})
    assert pepper == b"un-pepper-secret"


def test_hash_password_then_verify_round_trip() -> None:
    stored = crypto.hash_password("un-mot-de-passe-robuste")
    assert stored.startswith("scrypt$")
    assert "un-mot-de-passe-robuste" not in stored
    assert crypto.verify_password("un-mot-de-passe-robuste", stored) is True
    assert crypto.verify_password("mauvais-mot-de-passe", stored) is False


def test_hash_password_rejects_empty_password() -> None:
    with pytest.raises(ValueError):
        crypto.hash_password("")


def test_verify_password_rejects_malformed_stored_hash() -> None:
    assert crypto.verify_password("peu-importe", "pas-un-hash-valide") is False


def test_sign_payload_then_verify_signature_round_trip() -> None:
    signature = crypto.sign_payload("user-1.1234567890", b"pepper-de-test")
    assert crypto.verify_signature("user-1.1234567890", signature, b"pepper-de-test") is True
    assert crypto.verify_signature("user-1.9999999999", signature, b"pepper-de-test") is False
    assert crypto.verify_signature("user-1.1234567890", signature, b"autre-pepper") is False
