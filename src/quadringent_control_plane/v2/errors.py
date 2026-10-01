"""Enveloppe d'erreur standard ``/v2`` (§2.6 du contrat).

``{"error":{"code","message","next_action","retryable"}}`` — catalogue
stable. Seuls les codes pertinents au périmètre de ce chantier (tâches 1,
2, 3, 5, 6) sont construits ici ; le catalogue complet (§2.6) reste la
référence pour les chantiers suivants.
"""

from __future__ import annotations


class ApiError(Exception):
    """Erreur HTTP `/v2` portant le corps de l'enveloppe standard."""

    def __init__(self, status_code: int, code: str, message: str, *, next_action: str, retryable: bool) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.next_action = next_action
        self.retryable = retryable

    def to_body(self) -> dict[str, object]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "next_action": self.next_action,
                "retryable": self.retryable,
            }
        }


def invalid_request(message: str) -> ApiError:
    return ApiError(400, "invalid_request", message, next_action="corriger le corps, revoir OpenAPI", retryable=False)


def idempotency_key_conflict(message: str) -> ApiError:
    return ApiError(
        409,
        "idempotency_key_conflict",
        message,
        next_action="changer de clé ou renvoyer le corps identique",
        retryable=False,
    )


def not_found(message: str) -> ApiError:
    return ApiError(404, "not_found", message, next_action="vérifier l'identifiant", retryable=False)


def store_unavailable(message: str) -> ApiError:
    return ApiError(503, "store_unavailable", message, next_action="réessayer, alerter l'admin", retryable=True)


def insufficient_role(message: str) -> ApiError:
    return ApiError(
        403, "insufficient_role", message, next_action="demander l'élévation à un admin", retryable=False
    )


def capability_unavailable(message: str) -> ApiError:
    return ApiError(
        409, "capability_unavailable", message, next_action="relire l'état de la ressource", retryable=False
    )


def action_in_progress(message: str) -> ApiError:
    return ApiError(409, "action_in_progress", message, next_action="réessayer après", retryable=True)


def pending_confirmation_required(message: str) -> ApiError:
    return ApiError(
        409,
        "pending_confirmation_required",
        message,
        next_action="consulter /v2/confirmations",
        retryable=False,
    )


def wrong_confirmation(message: str) -> ApiError:
    return ApiError(
        403,
        "wrong_confirmation",
        message,
        next_action="relancer avec dry_run puis confirmer",
        retryable=False,
    )


def wrong_environment(message: str) -> ApiError:
    return ApiError(
        403,
        "wrong_environment",
        message,
        next_action="vérifier la restriction de source du jeton",
        retryable=False,
    )


def invalid_credentials(message: str) -> ApiError:
    return ApiError(
        401,
        "invalid_request",
        message,
        next_action="fournir une identité valide",
        retryable=False,
    )


def executor_unavailable(message: str) -> ApiError:
    """Panne de l'exécuteur Kubernetes (Job/Deployment illisible ou refusé,
    Secrets non provisionnables, position de journal illisible) — transitoire
    par nature : retenter a une chance raisonnable de réussir."""

    return ApiError(
        503,
        "executor_unavailable",
        message,
        next_action="réessayer ; si l'échec persiste, vérifier le déploiement de l'exécuteur Kubernetes",
        retryable=True,
    )


def invalid_configuration(message: str) -> ApiError:
    """La configuration déclarée (source/table/destination) ne peut pas
    produire un manifeste valide — un problème de données, jamais transitoire :
    retenter sans corriger la configuration échouera de la même façon."""

    return ApiError(
        422,
        "invalid_configuration",
        message,
        next_action="corriger la configuration de la source/table/destination avant de relancer",
        retryable=False,
    )


# Constat du 24 septembre 2026 (premier démarrage réel, QDC_ORDERS) :
# ``ExecutorError``/``BoundaryUnavailableError`` (``v2/executor/kubernetes.py``,
# ``v2/executor/boundary_reader.py``) portent déjà un code sûr (``.code``) et
# un message, mais n'étaient converties nulle part — l'API renvoyait une 500
# brute (trace Python) au lieu d'une enveloppe structurée. Un seul catalogue
# ici, partagé par les deux classes (même vocabulaire de code) : ``not_found``
# (la ressource visée a disparu entre la décision et l'exécution — 404),
# ``capability_unavailable`` (précondition manquante mais corrigible par
# l'opérateur — 409, cohérent avec l'usage existant de ce code ailleurs dans
# ce catalogue), ``executor_unavailable`` (panne d'infrastructure — 503).
_EXECUTOR_ERROR_CODES = {
    "not_found": not_found,
    "capability_unavailable": capability_unavailable,
    "executor_unavailable": executor_unavailable,
}


def from_executor_error(exc: Exception) -> ApiError:
    """Convertit une ``ExecutorError``/``BoundaryUnavailableError`` (``.code``
    + message) en ``ApiError`` structurée. Un code inconnu (catalogue étendu
    côté exécuteur sans mise à jour ici) retombe sur ``executor_unavailable``
    (503, retryable) — jamais une 500 non enveloppée."""

    code = getattr(exc, "code", None)
    builder = _EXECUTOR_ERROR_CODES.get(code, executor_unavailable)
    return builder(str(exc))
