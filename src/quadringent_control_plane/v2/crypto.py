"""Chiffrement des secrets (Fernet) — jamais de valeur en clair persistée.

La clé est lue depuis une variable d'environnement
(``QUADRINGENT_V2_SECRET_KEY``, valeur Fernet encodée base64) ou un fichier
désigné par ``QUADRINGENT_V2_SECRET_KEY_FILE`` (même contenu). Aucune des
deux n'est journalisée. En leur absence, le chiffrement échoue fermé
(``SecretKeyUnavailableError``) plutôt que d'inventer une clé éphémère en
production — les appelants de test fournissent une clé explicite.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import secrets

from cryptography.fernet import Fernet, InvalidToken

ENV_KEY = "QUADRINGENT_V2_SECRET_KEY"
ENV_KEY_FILE = "QUADRINGENT_V2_SECRET_KEY_FILE"

# Pepper serveur pour le hash des jetons d'agent (sha256(pepper || jeton)).
# Choix documenté (tâche 8, contrat §6.3) : un jeton d'agent est une valeur
# aléatoire à haute entropie (32 octets), jamais un secret choisi par un
# humain — un hachage lent (argon2id) n'apporte donc aucune résistance
# supplémentaire à une attaque par force brute (l'espace de recherche est
# déjà de 2**256) et introduit une dépendance native supplémentaire
# (argon2-cffi) ainsi qu'une surface de déni de service à la vérification
# (chaque appel authentifié coûterait un hachage lent). sha256 avec pepper
# serveur (jamais stocké en base) suffit ici, à l'image de la pratique
# GitHub/Stripe pour leurs jetons d'API. Les mots de passe utilisateurs
# (tâche 9, faible entropie humaine) restent hachés avec scrypt (module
# ``hashlib`` standard, pas de dépendance supplémentaire).
ENV_TOKEN_PEPPER = "QUADRINGENT_V2_TOKEN_PEPPER"
ENV_TOKEN_PEPPER_FILE = "QUADRINGENT_V2_TOKEN_PEPPER_FILE"


class TokenPepperUnavailableError(RuntimeError):
    """Aucun pepper de jeton déclaré — fail-closed (jamais de valeur par défaut)."""


def load_token_pepper(env: dict[str, str] | None = None) -> bytes:
    """Résout le pepper serveur des jetons d'agent depuis l'environnement."""

    environ = os.environ if env is None else env
    inline = (environ.get(ENV_TOKEN_PEPPER) or "").strip()
    if inline:
        return inline.encode("utf-8")
    pepper_file = (environ.get(ENV_TOKEN_PEPPER_FILE) or "").strip()
    if pepper_file:
        content = Path(pepper_file).read_text(encoding="utf-8").strip()
        if content:
            return content.encode("utf-8")
    raise TokenPepperUnavailableError(
        f"aucun pepper de jeton déclaré : définir {ENV_TOKEN_PEPPER} ou {ENV_TOKEN_PEPPER_FILE}"
    )


_TOKEN_PREFIX_BY_SCOPE = {"read": "rd", "operate": "op", "admin": "ad"}
_TOKEN_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def generate_agent_token(scope: str) -> tuple[str, str]:
    """Génère ``(jeton_en_clair, prefixe)`` — format ``qdt_<rd|op|ad>_<b62>``."""

    if scope not in _TOKEN_PREFIX_BY_SCOPE:
        raise ValueError(f"scope inconnu : {scope!r}")
    prefix = _TOKEN_PREFIX_BY_SCOPE[scope]
    random_part = "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(43))  # ~32 octets d'entropie
    return f"qdt_{prefix}_{random_part}", prefix


def hash_agent_token(token: str, pepper: bytes) -> str:
    """Empreinte stable d'un jeton d'agent — jamais le jeton en clair stocké."""

    digest = hmac.new(pepper, token.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()


_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_SALT_LEN = 16


def hash_password(password: str) -> str:
    """Hache un mot de passe humain avec scrypt (``hashlib`` standard).

    Format stocké : ``scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex>`` — permet de
    faire évoluer les paramètres scrypt sans invalider les hachages déjà en
    base.
    """

    if not isinstance(password, str) or not password:
        raise ValueError("mot de passe vide ou invalide")
    salt = secrets.token_bytes(_SCRYPT_SALT_LEN)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${derived.hex()}"


def sign_payload(payload: str, pepper: bytes) -> str:
    """Signature HMAC-sha256 d'un texte arbitraire — utilisée pour les
    cookies de session (tâche 9) et les liens d'activation à usage unique.
    """

    return hmac.new(pepper, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_signature(payload: str, signature: str, pepper: bytes) -> bool:
    return hmac.compare_digest(sign_payload(payload, pepper), signature)


def verify_password(password: str, stored_hash: str) -> bool:
    """Vérifie un mot de passe contre son hash scrypt stocké — jamais de log."""

    try:
        algo, n_str, r_str, p_str, salt_hex, hash_hex = stored_hash.split("$")
        if algo != "scrypt":
            return False
        n, r, p = int(n_str), int(r_str), int(p_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    candidate = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected))
    return hmac.compare_digest(candidate, expected)


class SecretKeyUnavailableError(RuntimeError):
    """Aucune clé de chiffrement déclarée — fail-closed."""


class SecretDecryptionError(RuntimeError):
    """Le secret chiffré est invalide ou la clé a été rotée sans migration."""


def load_secret_key(env: dict[str, str] | None = None) -> bytes:
    """Résout la clé Fernet depuis l'environnement — jamais depuis le code."""

    environ = os.environ if env is None else env
    inline = (environ.get(ENV_KEY) or "").strip()
    if inline:
        return inline.encode("utf-8")
    key_file = (environ.get(ENV_KEY_FILE) or "").strip()
    if key_file:
        content = Path(key_file).read_text(encoding="utf-8").strip()
        if content:
            return content.encode("utf-8")
    raise SecretKeyUnavailableError(
        f"aucune clé de chiffrement déclarée : définir {ENV_KEY} ou {ENV_KEY_FILE}"
    )


class SecretBox:
    """Chiffre/déchiffre des secrets avec une clé Fernet donnée."""

    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    @classmethod
    def from_environment(cls, env: dict[str, str] | None = None) -> "SecretBox":
        return cls(load_secret_key(env))

    @staticmethod
    def generate_key() -> bytes:
        """Génère une nouvelle clé Fernet — utilitaire d'exploitation/tests."""

        return Fernet.generate_key()

    def encrypt(self, plaintext: str) -> str:
        if not isinstance(plaintext, str) or not plaintext:
            raise ValueError("valeur à chiffrer vide ou invalide")
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as error:
            raise SecretDecryptionError("secret chiffré invalide ou clé rotée") from error
