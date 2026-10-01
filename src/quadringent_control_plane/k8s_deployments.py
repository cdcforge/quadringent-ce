"""Client Kubernetes minimal (stdlib) pour le Deployment du lecteur de capture.

Symétrique de ``k8s_jobs.py`` (même transport injectable, mêmes codes
d'erreur réduits, mêmes garde-fous), étendu aux verbes nécessaires à un
Deployment : création, lecture, remplacement (``PUT`` — la ``spec`` change
avec le jeu de tables du journal), suppression (plus aucune table live sur
ce journal). Réutilise ``ServiceAccountContext`` et le transport HTTPS de
``k8s_jobs.py`` : une seule identité de ServiceAccount pour tout le module
d'exécution v2.
"""

from __future__ import annotations

from typing import Mapping

from .k8s_jobs import (  # noqa: F401 — réexportés pour les appelants de ce module
    DEFAULT_TIMEOUT_SECONDS,
    ServiceAccountContext,
    Transport,
    is_dns_label,
    https_transport,
)

DEPLOYMENTS_PATH = "/apis/apps/v1/namespaces/{namespace}/deployments"
JSON_CONTENT_TYPE = "application/json"

CODE_NOT_CONFIGURED = "not_configured"
CODE_ALREADY_EXISTS = "deployment_already_exists"
CODE_UNAUTHORIZED = "deployments_api_unauthorized"
CODE_REJECTED = "deployments_api_rejected"
CODE_UNAVAILABLE = "deployments_api_unavailable"
CODE_INVALID_RESPONSE = "deployments_api_invalid_response"


class DeploymentsApiError(RuntimeError):
    """Erreur de l'API Deployments, réduite à un code sûr sans détail distant."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DeploymentAlreadyExists(DeploymentsApiError):
    def __init__(self) -> None:
        super().__init__(CODE_ALREADY_EXISTS, "Deployment déjà existant")


class KubernetesDeploymentsClient:
    """Création, lecture, remplacement et suppression d'un Deployment."""

    def __init__(self, transport: Transport, namespace: str) -> None:
        if not callable(transport):
            raise DeploymentsApiError(CODE_NOT_CONFIGURED, "Transport Kubernetes absent")
        if not is_dns_label(namespace):
            raise DeploymentsApiError(CODE_NOT_CONFIGURED, "Namespace Kubernetes invalide")
        self._transport = transport
        self._namespace = namespace

    @property
    def namespace(self) -> str:
        return self._namespace

    def create_deployment(self, manifest: Mapping[str, object]) -> dict[str, object]:
        import json

        name = _manifest_name(manifest)
        body = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response = self._transport("POST", self._collection_path(), body, JSON_CONTENT_TYPE)
        if response.status in (200, 201):
            return _body(response.body)
        if response.status == 409:
            raise DeploymentAlreadyExists()
        raise _status_error(response.status)

    def read_deployment(self, name: str) -> dict[str, object] | None:
        if not is_dns_label(name):
            raise DeploymentsApiError(CODE_NOT_CONFIGURED, "Nom de Deployment invalide")
        response = self._transport("GET", f"{self._collection_path()}/{name}", None, JSON_CONTENT_TYPE)
        if response.status == 200:
            return _body(response.body)
        if response.status == 404:
            return None
        raise _status_error(response.status)

    def replace_deployment(self, name: str, manifest: Mapping[str, object]) -> dict[str, object]:
        """Remplace la ``spec`` d'un Deployment existant (mise à jour désirée).

        Un ``PUT`` complet, pas un patch partiel : le manifeste désiré porte
        toujours l'état complet voulu (jeu de tables, réplicas, image) — la
        réconciliation (``reconcile.py``) a déjà décidé qu'une mise à jour
        est nécessaire en comparant l'empreinte de ``spec``.
        """

        import json

        if not is_dns_label(name):
            raise DeploymentsApiError(CODE_NOT_CONFIGURED, "Nom de Deployment invalide")
        body = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response = self._transport("PUT", f"{self._collection_path()}/{name}", body, JSON_CONTENT_TYPE)
        if response.status in (200, 201):
            return _body(response.body)
        if response.status == 404:
            raise DeploymentsApiError(CODE_REJECTED, "Deployment introuvable pour le remplacement")
        raise _status_error(response.status)

    def delete_deployment(self, name: str) -> None:
        if not is_dns_label(name):
            raise DeploymentsApiError(CODE_NOT_CONFIGURED, "Nom de Deployment invalide")
        response = self._transport("DELETE", f"{self._collection_path()}/{name}", None, JSON_CONTENT_TYPE)
        if response.status in (200, 202, 404):
            return
        raise _status_error(response.status)

    def _collection_path(self) -> str:
        return DEPLOYMENTS_PATH.format(namespace=self._namespace)


def _manifest_name(manifest: object) -> str:
    if not isinstance(manifest, Mapping):
        raise DeploymentsApiError(CODE_NOT_CONFIGURED, "Manifeste de Deployment invalide")
    metadata = manifest.get("metadata")
    name = metadata.get("name") if isinstance(metadata, Mapping) else None
    if not is_dns_label(name):
        raise DeploymentsApiError(CODE_NOT_CONFIGURED, "Nom de Deployment invalide")
    return name


def _body(body: object) -> dict[str, object]:
    if not isinstance(body, Mapping):
        raise DeploymentsApiError(CODE_INVALID_RESPONSE, "Deployment Kubernetes illisible")
    return dict(body)


def _status_error(status: int) -> DeploymentsApiError:
    if status in (401, 403):
        return DeploymentsApiError(CODE_UNAUTHORIZED, "API Deployments refuse l'identité du pod")
    if status == 409:
        return DeploymentAlreadyExists()
    if 400 <= status < 500:
        return DeploymentsApiError(CODE_REJECTED, "Deployment refusé par l'API Kubernetes")
    return DeploymentsApiError(CODE_UNAVAILABLE, "API Deployments en erreur")
