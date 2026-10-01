"""Copies JSON sans secrets à émission unique pour audit, rejeu et migration.

La première réponse HTTP reste intacte. Les champs voisins (identifiants,
empreintes, indicateurs ``secret_set``/``token_set``, script public) sont conservés.
"""

from __future__ import annotations

from typing import overload

_ONE_TIME_SECRET_FIELDS = frozenset({"private_key_pem", "token", "activation_token", "secret"})


@overload
def redact_one_time_secrets(value: dict[str, object]) -> dict[str, object]: ...


@overload
def redact_one_time_secrets(value: object) -> object: ...


def redact_one_time_secrets(value: object) -> object:
    """Retire récursivement les champs réservés, sans muter la valeur fournie."""
    if isinstance(value, dict):
        return {key: redact_one_time_secrets(item) for key, item in value.items() if key not in _ONE_TIME_SECRET_FIELDS}
    if isinstance(value, (list, tuple)):
        return [redact_one_time_secrets(item) for item in value]
    return value
