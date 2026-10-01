"""Client Kubernetes minimal (stdlib) pour les Secrets référencés par les Jobs/Deployments.

Même discipline que ``k8s_jobs.py``/``k8s_deployments.py`` (transport
injectable, codes d'erreur réduits, aucune capacité superflue) : ce module
n'expose que la création et la mise à jour d'un Secret par son nom — jamais
sa lecture (le control plane n'a jamais besoin de relire un secret déjà
provisionné, seulement de le maintenir à jour). L'API Secrets vit dans le
groupe « core » (``/api/v1``, pas ``/apis/...``), à la différence de
``batch/v1``/``apps/v1``.

Les valeurs manipulées par l'appelant (``secrets_provisioner.py``) ne
doivent jamais être journalisées ; ce module ne journalise rien lui-même.
"""

from __future__ import annotations

import base64
import json
from typing import Mapping

from .k8s_jobs import Transport, is_dns_label

SECRETS_PATH = "/api/v1/namespaces/{namespace}/secrets"
JSON_CONTENT_TYPE = "application/json"
MERGE_PATCH_CONTENT_TYPE = "application/merge-patch+json"

CODE_NOT_CONFIGURED = "not_configured"
CODE_UNAUTHORIZED = "secrets_api_unauthorized"
CODE_REJECTED = "secrets_api_rejected"
CODE_UNAVAILABLE = "secrets_api_unavailable"
CODE_INVALID_RESPONSE = "secrets_api_invalid_response"


class SecretsApiError(RuntimeError):
    """Erreur de l'API Secrets, réduite à un code sûr — jamais de valeur en clair."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class KubernetesSecretsClient:
    """Crée ou met à jour un Secret ``Opaque`` par son nom — jamais ne le lit."""

    def __init__(self, transport: Transport, namespace: str) -> None:
        if not callable(transport):
            raise SecretsApiError(CODE_NOT_CONFIGURED, "Transport Kubernetes absent")
        if not is_dns_label(namespace):
            raise SecretsApiError(CODE_NOT_CONFIGURED, "Namespace Kubernetes invalide")
        self._transport = transport
        self._namespace = namespace

    @property
    def namespace(self) -> str:
        return self._namespace

    def upsert_secret(self, name: str, string_data: Mapping[str, str]) -> None:
        """Crée le Secret s'il n'existe pas, sinon remplace ses données.

        ``string_data`` est encodé en base64 nous-mêmes (``data``, pas
        ``stringData``) : la valeur ne transite jamais en clair dans le
        corps de la réponse journalisable par un intermédiaire — même
        prudence que ``crypto.py`` pour les secrets applicatifs.
        """

        if not is_dns_label(name):
            raise SecretsApiError(CODE_NOT_CONFIGURED, "Nom de Secret invalide")
        if not string_data:
            raise SecretsApiError(CODE_NOT_CONFIGURED, "Secret sans donnée à provisionner")
        data = {key: base64.b64encode(value.encode("utf-8")).decode("ascii") for key, value in string_data.items()}
        manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": {"name": name, "namespace": self._namespace},
            "data": data,
        }
        body = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response = self._transport("POST", self._collection_path(), body, JSON_CONTENT_TYPE)
        if response.status in (200, 201):
            return
        if response.status == 409:
            self._replace(name, data)
            return
        raise _status_error(response.status)

    def delete_secret(self, name: str) -> None:
        """Supprime un Secret. Idempotent : absent est déjà l'état voulu —
        le nettoyage d'un Secret éphémère (sonde/découverte) doit toujours
        pouvoir être rappelé sans condition, y compris après un échec."""

        if not is_dns_label(name):
            raise SecretsApiError(CODE_NOT_CONFIGURED, "Nom de Secret invalide")
        response = self._transport("DELETE", f"{self._collection_path()}/{name}", None, JSON_CONTENT_TYPE)
        if response.status in (200, 202, 404):
            return
        raise _status_error(response.status)

    def _replace(self, name: str, data: Mapping[str, str]) -> None:
        body = json.dumps({"data": data}, separators=(",", ":")).encode("utf-8")
        response = self._transport(
            "PATCH", f"{self._collection_path()}/{name}", body, MERGE_PATCH_CONTENT_TYPE
        )
        if response.status in (200, 201):
            return
        raise _status_error(response.status)

    def _collection_path(self) -> str:
        return SECRETS_PATH.format(namespace=self._namespace)


def _status_error(status: int) -> SecretsApiError:
    if status in (401, 403):
        return SecretsApiError(CODE_UNAUTHORIZED, "API Secrets refuse l'identité du pod")
    if 400 <= status < 500:
        return SecretsApiError(CODE_REJECTED, "Secret refusé par l'API Kubernetes")
    return SecretsApiError(CODE_UNAVAILABLE, "API Secrets en erreur")
