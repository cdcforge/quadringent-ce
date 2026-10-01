"""Client Kubernetes minimal (stdlib) pour créer et relire des Jobs DEV.

Ce module n'expose que deux opérations, volontairement :
créer un Job dans le namespace configuré et le relire par son nom. Aucun autre
verbe, aucune autre ressource, aucun corps d'erreur distant n'est propagé.

L'authentification utilise le jeton du ServiceAccount monté dans le pod. Un
jeton ou un certificat absent fait échouer l'appel : le contrôleur ne bascule
jamais vers un chemin non authentifié.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import ssl
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SERVICE_ACCOUNT_ROOT = "/var/run/secrets/kubernetes.io/serviceaccount"
JOBS_PATH = "/apis/batch/v1/namespaces/{namespace}/jobs"
JSON_CONTENT_TYPE = "application/json"
MERGE_PATCH_CONTENT_TYPE = "application/merge-patch+json"
DEFAULT_TIMEOUT_SECONDS = 10.0
_MAX_RESPONSE_BYTES = 1024 * 1024

_DNS_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

CODE_NOT_CONFIGURED = "not_configured"
CODE_ALREADY_EXISTS = "job_already_exists"
CODE_UNAUTHORIZED = "jobs_api_unauthorized"
CODE_REJECTED = "jobs_api_rejected"
CODE_UNAVAILABLE = "jobs_api_unavailable"
CODE_INVALID_RESPONSE = "jobs_api_invalid_response"


class JobsApiError(RuntimeError):
    """Erreur de l'API Jobs, réduite à un code sûr et sans détail distant."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class JobAlreadyExists(JobsApiError):
    def __init__(self) -> None:
        super().__init__(CODE_ALREADY_EXISTS, "Job déjà existant")


def is_dns_label(value: object) -> bool:
    """Vrai si la valeur est un nom DNS-1123 (namespace, nom de Job)."""

    return isinstance(value, str) and bool(_DNS_LABEL.match(value))


@dataclass(frozen=True)
class ServiceAccountContext:
    token: str
    ca_file: Path
    namespace: str

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not self.token.strip():
            raise JobsApiError(CODE_NOT_CONFIGURED, "Jeton de ServiceAccount absent")
        if any(character.isspace() for character in self.token):
            raise JobsApiError(CODE_NOT_CONFIGURED, "Jeton de ServiceAccount invalide")
        if not is_dns_label(self.namespace):
            raise JobsApiError(CODE_NOT_CONFIGURED, "Namespace Kubernetes invalide")
        if not Path(self.ca_file).is_file():
            raise JobsApiError(CODE_NOT_CONFIGURED, "Autorité de certification absente")

    @classmethod
    def load(
        cls,
        root: str | Path = SERVICE_ACCOUNT_ROOT,
        *,
        namespace: str | None = None,
    ) -> "ServiceAccountContext":
        """Charge l'identité du pod, ou échoue explicitement sans identité."""

        base = Path(root)
        try:
            token = (base / "token").read_text(encoding="utf-8").strip()
        except OSError:
            raise JobsApiError(CODE_NOT_CONFIGURED, "Jeton de ServiceAccount illisible") from None
        if namespace is None:
            try:
                namespace = (base / "namespace").read_text(encoding="utf-8").strip()
            except OSError:
                raise JobsApiError(CODE_NOT_CONFIGURED, "Namespace Kubernetes illisible") from None
        return cls(token=token, ca_file=base / "ca.crt", namespace=namespace)


@dataclass(frozen=True)
class JobsResponse:
    status: int
    body: object


# Transport(method, path, body, content_type) : le type de corps fait partie du
# contrat, un PATCH Kubernetes n'accepte pas n'importe quel type de fusion.
Transport = Callable[[str, str, "bytes | None", str], JobsResponse]


def _parse_json(raw: bytes) -> object:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise JobsApiError(CODE_INVALID_RESPONSE, "Réponse Kubernetes illisible") from None


def https_transport(
    context: ServiceAccountContext,
    *,
    host: str,
    port: int,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> Transport:
    """Construit le transport HTTPS réel vers l'API server du cluster."""

    if not host.strip():
        raise JobsApiError(CODE_NOT_CONFIGURED, "Adresse de l'API Kubernetes absente")
    if not 1 <= port <= 65535:
        raise JobsApiError(CODE_NOT_CONFIGURED, "Port de l'API Kubernetes invalide")
    if timeout_seconds <= 0:
        raise JobsApiError(CODE_NOT_CONFIGURED, "Délai Kubernetes invalide")
    authority = f"{host}:{port}"
    tls = ssl.create_default_context(cafile=str(context.ca_file))

    def transport(method: str, path: str, body: bytes | None, content_type: str) -> JobsResponse:
        request = Request(
            url=f"https://{authority}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {context.token}",
                "Accept": "application/json",
                "Content-Type": content_type,
            },
        )
        try:
            with urlopen(request, timeout=timeout_seconds, context=tls) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise JobsApiError(CODE_INVALID_RESPONSE, "Réponse Kubernetes trop volumineuse")
                return JobsResponse(status=response.status, body=_parse_json(raw))
        except HTTPError as error:
            raw = error.read(_MAX_RESPONSE_BYTES + 1) if error.fp is not None else b""
            return JobsResponse(status=error.code, body=_parse_json(raw))
        except (URLError, TimeoutError, OSError):
            raise JobsApiError(CODE_UNAVAILABLE, "API Kubernetes injoignable") from None

    return transport


class KubernetesJobsClient:
    """Création et relecture de Jobs, sans autre capacité."""

    def __init__(self, transport: Transport, namespace: str) -> None:
        if not callable(transport):
            raise JobsApiError(CODE_NOT_CONFIGURED, "Transport Kubernetes absent")
        if not is_dns_label(namespace):
            raise JobsApiError(CODE_NOT_CONFIGURED, "Namespace Kubernetes invalide")
        self._transport = transport
        self._namespace = namespace

    @property
    def namespace(self) -> str:
        return self._namespace

    def create_job(self, manifest: Mapping[str, object]) -> dict[str, object]:
        """Crée un Job. Un nom déjà pris est signalé, jamais écrasé."""

        name = _manifest_name(manifest)
        body = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response = self._transport("POST", self._collection_path(), body, JSON_CONTENT_TYPE)
        if response.status in (200, 201):
            return _job_body(response.body)
        if response.status == 409:
            raise JobAlreadyExists()
        raise _status_error(response.status)

    def read_job(self, name: str) -> dict[str, object] | None:
        """Relit un Job par son nom ; None s'il n'existe pas."""

        if not is_dns_label(name):
            raise JobsApiError(CODE_NOT_CONFIGURED, "Nom de Job invalide")
        response = self._transport(
            "GET", f"{self._collection_path()}/{name}", None, JSON_CONTENT_TYPE
        )
        if response.status == 200:
            return _job_body(response.body)
        if response.status == 404:
            return None
        raise _status_error(response.status)

    def set_job_suspend(self, name: str, suspend: bool) -> dict[str, object]:
        """Suspend ou reprend un Job, et rend la réponse du serveur.

        La fusion partielle ne touche que `spec.suspend` : un Job ne peut pas
        être redirigé vers un autre conteneur ou une autre image par ce chemin.
        L'appelant relit ensuite le Job pour constater l'effet.
        """

        if not is_dns_label(name):
            raise JobsApiError(CODE_NOT_CONFIGURED, "Nom de Job invalide")
        if type(suspend) is not bool:
            raise JobsApiError(CODE_NOT_CONFIGURED, "Suspension de Job invalide")
        body = json.dumps({"spec": {"suspend": suspend}}, separators=(",", ":")).encode("utf-8")
        response = self._transport(
            "PATCH",
            f"{self._collection_path()}/{name}",
            body,
            MERGE_PATCH_CONTENT_TYPE,
        )
        if response.status in (200, 201):
            return _job_body(response.body)
        if response.status == 404:
            raise JobsApiError(CODE_REJECTED, "Job introuvable pour la suspension")
        raise _status_error(response.status)

    def delete_job(self, name: str) -> None:
        """Supprime un Job et ses pods. La propagation ``Background`` est
        explicite : pour ``batch/v1``, l'API REST laisse sinon les pods
        orphelins (constaté sur GKE). Idempotent : un Job déjà absent n'est
        jamais une erreur — le nettoyage d'un Job de diagnostic doit
        toujours pouvoir être rappelé sans condition (échec, timeout)."""

        if not is_dns_label(name):
            raise JobsApiError(CODE_NOT_CONFIGURED, "Nom de Job invalide")
        options = json.dumps(
            {"kind": "DeleteOptions", "apiVersion": "v1", "propagationPolicy": "Background"},
            separators=(",", ":"),
        ).encode("utf-8")
        response = self._transport(
            "DELETE", f"{self._collection_path()}/{name}", options, JSON_CONTENT_TYPE
        )
        if response.status in (200, 202, 404):
            return
        raise _status_error(response.status)

    def _collection_path(self) -> str:
        return JOBS_PATH.format(namespace=self._namespace)


def _manifest_name(manifest: object) -> str:
    if not isinstance(manifest, Mapping):
        raise JobsApiError(CODE_NOT_CONFIGURED, "Manifeste de Job invalide")
    metadata = manifest.get("metadata")
    name = metadata.get("name") if isinstance(metadata, Mapping) else None
    if not is_dns_label(name):
        raise JobsApiError(CODE_NOT_CONFIGURED, "Nom de Job invalide")
    return name


def _job_body(body: object) -> dict[str, object]:
    if not isinstance(body, Mapping):
        raise JobsApiError(CODE_INVALID_RESPONSE, "Job Kubernetes illisible")
    return dict(body)


def _status_error(status: int) -> JobsApiError:
    if status in (401, 403):
        return JobsApiError(CODE_UNAUTHORIZED, "API Jobs refuse l'identité du pod")
    if status == 409:
        return JobAlreadyExists()
    if 400 <= status < 500:
        return JobsApiError(CODE_REJECTED, "Job refusé par l'API Kubernetes")
    return JobsApiError(CODE_UNAVAILABLE, "API Jobs en erreur")


def client_from_environment(
    environ: Mapping[str, str] | None = None,
    *,
    root: str | Path = SERVICE_ACCOUNT_ROOT,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> KubernetesJobsClient:
    """Assemble le client réel depuis l'identité montée et l'adresse du cluster."""

    source = os.environ if environ is None else environ
    context = ServiceAccountContext.load(root)
    host = source.get("KUBERNETES_SERVICE_HOST", "").strip()
    raw_port = source.get("KUBERNETES_SERVICE_PORT_HTTPS") or source.get("KUBERNETES_SERVICE_PORT") or "443"
    try:
        port = int(raw_port)
    except ValueError:
        raise JobsApiError(CODE_NOT_CONFIGURED, "Port de l'API Kubernetes invalide") from None
    return KubernetesJobsClient(
        https_transport(context, host=host, port=port, timeout_seconds=timeout_seconds),
        context.namespace,
    )
