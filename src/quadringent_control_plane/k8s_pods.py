"""Client Kubernetes minimal (stdlib) pour lister des pods et lire leurs
journaux — lecture seule, jamais d'écriture.

Symétrique de ``k8s_jobs.py``/``k8s_deployments.py`` (même identité de
ServiceAccount, mêmes codes d'erreur réduits, même style de transport
injectable) mais avec un transport dédié : contrairement aux Jobs/
Deployments, ``GET .../log`` renvoie du texte brut, pas du JSON — le
transport ici renvoie donc des octets non interprétés, et c'est
l'appelant (``list_pods``/``read_pod_log``) qui décide de la lecture
(JSON pour la liste des pods, texte pour un journal).

Toute lecture est bornée : ``list_pods`` plafonne au ``limit`` demandé,
``read_pod_log`` plafonne le nombre d'octets lus (``_MAX_LOG_BYTES``) — un
pod bavard ne peut jamais faire gonfler une réponse HTTP v2 sans limite.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .k8s_jobs import DEFAULT_TIMEOUT_SECONDS, ServiceAccountContext, is_dns_label

PODS_PATH = "/api/v1/namespaces/{namespace}/pods"
POD_LOG_PATH = "/api/v1/namespaces/{namespace}/pods/{name}/log"
_MAX_LOG_BYTES = 256 * 1024
_MAX_LIST_BYTES = 1024 * 1024

CODE_NOT_CONFIGURED = "not_configured"
CODE_UNAUTHORIZED = "pods_api_unauthorized"
CODE_REJECTED = "pods_api_rejected"
CODE_UNAVAILABLE = "pods_api_unavailable"
CODE_INVALID_RESPONSE = "pods_api_invalid_response"
CODE_NOT_FOUND = "pod_not_found"


class PodsApiError(RuntimeError):
    """Erreur de l'API Pods, réduite à un code sûr et sans détail distant."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PodsResponse:
    status: int
    body: bytes


# PodsTransport(method, path_avec_query) -> réponse en octets bruts, jamais
# décodée par le transport lui-même (JSON pour la liste, texte pour un
# journal — deux formats, un seul point d'accès HTTP).
PodsTransport = Callable[[str, str], PodsResponse]


def https_pods_transport(
    context: ServiceAccountContext,
    *,
    host: str,
    port: int,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = _MAX_LIST_BYTES,
) -> PodsTransport:
    """Construit le transport HTTPS réel vers l'API server du cluster."""

    if not host.strip():
        raise PodsApiError(CODE_NOT_CONFIGURED, "Adresse de l'API Kubernetes absente")
    if not 1 <= port <= 65535:
        raise PodsApiError(CODE_NOT_CONFIGURED, "Port de l'API Kubernetes invalide")
    if timeout_seconds <= 0:
        raise PodsApiError(CODE_NOT_CONFIGURED, "Délai Kubernetes invalide")
    authority = f"{host}:{port}"
    import ssl

    tls = ssl.create_default_context(cafile=str(context.ca_file))

    def transport(method: str, path: str) -> PodsResponse:
        request = Request(
            url=f"https://{authority}{path}",
            method=method,
            headers={"Authorization": f"Bearer {context.token}", "Accept": "application/json, text/plain"},
        )
        try:
            with urlopen(request, timeout=timeout_seconds, context=tls) as response:
                raw = response.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise PodsApiError(CODE_INVALID_RESPONSE, "Réponse Kubernetes trop volumineuse")
                return PodsResponse(status=response.status, body=raw)
        except HTTPError as error:
            raw = error.read(max_bytes + 1) if error.fp is not None else b""
            return PodsResponse(status=error.code, body=raw)
        except (URLError, TimeoutError, OSError):
            raise PodsApiError(CODE_UNAVAILABLE, "API Kubernetes injoignable") from None

    return transport


class KubernetesPodsClient:
    """Liste des pods par sélecteur de labels et lecture bornée de leurs
    journaux — aucune autre capacité (pas de création, pas de suppression)."""

    def __init__(self, transport: PodsTransport, namespace: str) -> None:
        if not callable(transport):
            raise PodsApiError(CODE_NOT_CONFIGURED, "Transport Kubernetes absent")
        if not is_dns_label(namespace):
            raise PodsApiError(CODE_NOT_CONFIGURED, "Namespace Kubernetes invalide")
        self._transport = transport
        self._namespace = namespace

    @property
    def namespace(self) -> str:
        return self._namespace

    def list_pod_names(self, *, label_selector: str, limit: int = 20) -> tuple[str, ...]:
        if not label_selector.strip():
            raise PodsApiError(CODE_NOT_CONFIGURED, "Sélecteur de labels absent")
        if not 1 <= limit <= 200:
            raise PodsApiError(CODE_NOT_CONFIGURED, "Limite de pods invalide")
        query = urlencode({"labelSelector": label_selector, "limit": limit})
        path = f"{PODS_PATH.format(namespace=self._namespace)}?{query}"
        response = self._transport("GET", path)
        if response.status != 200:
            raise _status_error(response.status)
        payload = _parse_json(response.body)
        if not isinstance(payload, Mapping) or not isinstance(payload.get("items"), list):
            raise PodsApiError(CODE_INVALID_RESPONSE, "Liste de pods Kubernetes illisible")
        names: list[str] = []
        for item in payload["items"][:limit]:
            if not isinstance(item, Mapping):
                continue
            metadata = item.get("metadata")
            name = metadata.get("name") if isinstance(metadata, Mapping) else None
            if isinstance(name, str) and name:
                names.append(name)
        return tuple(names)

    def read_pod_log(
        self,
        name: str,
        *,
        container: str | None = None,
        since_time: str | None = None,
        tail_lines: int = 200,
    ) -> str | None:
        """Journal brut d'un pod, avec horodatage k8s en tête de chaque ligne
        (``timestamps=true`` — un vrai horodatage serveur, jamais recalculé
        côté control plane). ``None`` si le pod n'existe pas (jamais une
        chaîne vide silencieuse confondue avec « aucune ligne »)."""

        if not is_dns_label(name):
            raise PodsApiError(CODE_NOT_CONFIGURED, "Nom de pod invalide")
        if not 1 <= tail_lines <= 5000:
            raise PodsApiError(CODE_NOT_CONFIGURED, "Nombre de lignes invalide")
        params: dict[str, object] = {"tailLines": tail_lines, "timestamps": "true"}
        if container:
            params["container"] = container
        if since_time:
            params["sinceTime"] = since_time
        query = urlencode(params)
        path = f"{POD_LOG_PATH.format(namespace=self._namespace, name=quote(name))}?{query}"
        response = self._transport("GET", path)
        if response.status == 404:
            return None
        if response.status != 200:
            raise _status_error(response.status)
        try:
            return response.body.decode("utf-8", errors="replace")
        except AttributeError:
            raise PodsApiError(CODE_INVALID_RESPONSE, "Journal Kubernetes illisible") from None


def _parse_json(raw: bytes) -> object:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PodsApiError(CODE_INVALID_RESPONSE, "Réponse Kubernetes illisible") from None


def _status_error(status: int) -> PodsApiError:
    if status in (401, 403):
        return PodsApiError(CODE_UNAUTHORIZED, "API Pods refuse l'identité du pod")
    if status == 404:
        return PodsApiError(CODE_NOT_FOUND, "Pod introuvable")
    if 400 <= status < 500:
        return PodsApiError(CODE_REJECTED, "Requête Pods refusée par l'API Kubernetes")
    return PodsApiError(CODE_UNAVAILABLE, "API Pods en erreur")
