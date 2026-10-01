"""Serveur HTTP local de consultation des projections Quadringent."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import ipaddress
import json
import hashlib
import mimetypes
from pathlib import Path
from typing import Mapping, Type
from urllib.parse import unquote, urlparse, urlsplit

from .audit import ActionAuditLog
from .actions import (
    MAX_ACTION_BYTES,
    execute_pipeline_action,
    parse_action_route,
    PipelineActionGate,
    PipelineActionInvocation,
    executor_supports,
)
from quadringent.site_config import SiteConfig, current as _current_site
from . import model as _model
from . import fleet as _fleet
from . import onboarding as _onboarding
from .onboarding import evaluate_onboarding, parse_table_selection
from .auth import AuthConfig, authenticate, authorize
from .connections import ConnectionsError, ConnectionsStorageError, ConnectionsStore
from .repository import ProjectionRepository, ProjectionSnapshot


KEEPALIVE_SECONDS = 15.0
MAX_ONBOARDING_BYTES = 16 * 1024
MAX_CONNECTIONS_BYTES = 16 * 1024
# Borne du corps relayé vers /v2 : plus large que les routes v1 (utilisateurs,
# sources, webhooks…) mais volontairement plafonnée — aucune route v2 connue
# n'approche cette taille ; au-delà, c'est un signal d'anomalie, pas un besoin
# légitime.
MAX_PROXY_BODY_BYTES = 256 * 1024
# Délai socket du relais vers v2 : assez large pour couvrir l'intervalle de
# keepalive SSE côté v2 sans bloquer indéfiniment un amont muet.
PROXY_UPSTREAM_TIMEOUT_SECONDS = 60.0
POST_MEDIA_TYPE = "application/json"
ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})
RUNTIME_FORMAT_VERSION = "quadringent-fleet-runtime-v1"
# Identités runtime du site déclaré — résolues à l'usage via ``__getattr__``.
RUNTIME_PIPELINE_ID: str
RUNTIME_ENVIRONMENT: str
RUNTIME_MANIFEST: tuple[str, ...]
RUNTIME_PHASES = frozenset(
    {
        "NOT_PREPARED",
        "PREPARED",
        "HISTORICAL",
        "CATCHING_UP",
        "LIVE",
        "RECONCILING",
        "CERTIFIED",
        "PAUSED",
        "BLOCKED",
        "UNKNOWN",
    }
)
# Une voie expose sa phase domaine réelle : READY n'existe qu'au niveau
# table — le reste partage le vocabulaire de la phase agrégée.
TABLE_PHASES = RUNTIME_PHASES | {"READY"}
RUNTIME_ACTIONS = ("prepare", "start", "pause", "resume", "refresh")
RUNTIME_KEYS = (
    "format_version",
    "fleet_id",
    "environment",
    "pipeline_id",
    "phase",
    "checkpoint",
    "capabilities",
    "table_states",
)


def __getattr__(name: str) -> object:
    """Identités du site déclaré, résolues à l'accès — jamais figées au code."""

    site_attributes = {
        "RUNTIME_PIPELINE_ID": lambda site: site.site_id,
        "RUNTIME_ENVIRONMENT": lambda site: site.environment,
        "RUNTIME_MANIFEST": lambda site: site.fleet_tables,
    }
    resolver = site_attributes.get(name)
    if resolver is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return resolver(_site())


def _site() -> SiteConfig:
    return _current_site()
_SENSITIVE_TOKENS = ("host", "user", "password", "secret", "token", "credential")
SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    (
        "Content-Security-Policy",
        "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
        "form-action 'none'; img-src 'self' data:; font-src 'self'; style-src 'self'; "
        "script-src 'self'; connect-src 'self'",
    ),
)


# En-têtes client relayés vers l'amont v2 — liste blanche : tout ce que le
# client peut poser n'est pas fiable, seuls ces en-têtes ont un sens pour
# v2 (contenu, négociation, authentification propre à v2 — jetons, cookies
# de session, idempotence, curseur SSE, session MCP). ``Host`` en est
# délibérément absent : http.client fixe lui-même l'en-tête Host de la
# requête amont d'après la connexion réelle (127.0.0.1:<port>), jamais
# d'après une valeur fournie par le client.
PROXY_REQUEST_HEADERS = (
    "Content-Type",
    "Accept",
    "Authorization",
    "Cookie",
    "Idempotency-Key",
    "Last-Event-ID",
    "Mcp-Session-Id",
    "MCP-Protocol-Version",
    "If-Match",
)
# En-têtes de bout en bout (hop-by-hop, RFC 9110 §7.6.1) : jamais relayés
# dans un sens ni dans l'autre — ils décrivent la connexion TCP locale, pas
# la ressource. Content-Length est traité à part (voir _proxy_to_v2).
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
    }
)


def _proxied_path(path: str) -> bool:
    """Vrai pour tout ce qui doit traverser vers le control plane v2."""

    return path == "/v2" or path.startswith("/v2/") or path.startswith("/mcp")


def _proxy_request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    forwarded: dict[str, str] = {}
    for name in PROXY_REQUEST_HEADERS:
        value = headers.get(name)
        if value is not None:
            forwarded[name] = value
    return forwarded


def _proxy_to_v2(handler: BaseHTTPRequestHandler, port: int, *, include_body: bool) -> None:
    """Relaie une requête vers le control plane v2 en loopback.

    v2 possède sa propre authentification (jetons d'agent, cookies de
    session) : ce relais ne fait ni authentification ni autorisation — voir
    l'appelant, qui court-circuite le garde v1 pour ces chemins avant même
    d'atteindre cette fonction.
    """

    length_header = handler.headers.get("Content-Length")
    body: bytes | None = None
    if length_header:
        try:
            length = int(length_header)
        except ValueError:
            handler._error(HTTPStatus.BAD_REQUEST, "invalid_request", include_body)
            return
        if length < 0 or length > MAX_PROXY_BODY_BYTES:
            handler.close_connection = True
            handler._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "payload_too_large", include_body)
            return
        body = handler.rfile.read(length) if length else b""
    upstream_headers = _proxy_request_headers(handler.headers)
    if body:
        upstream_headers["Content-Length"] = str(len(body))
    connection = http.client.HTTPConnection(
        "127.0.0.1", port, timeout=PROXY_UPSTREAM_TIMEOUT_SECONDS
    )
    try:
        connection.request(handler.command, handler.path, body=body, headers=upstream_headers)
        response = connection.getresponse()
    except (OSError, http.client.HTTPException):
        connection.close()
        # Aucune trace ni détail interne : l'amont est un détail
        # d'implémentation, pas une information à exposer au client.
        handler.close_connection = True
        handler._error(HTTPStatus.BAD_GATEWAY, "upstream_unavailable", include_body)
        return
    try:
        handler.send_response(response.status)
        content_length = response.getheader("Content-Length")
        if content_length is not None:
            handler.send_header("Content-Length", content_length)
        else:
            # Pas de taille annoncée (streaming SSE typiquement) : la fin de
            # la réponse est signalée par la fermeture de la connexion, en
            # HTTP/1.1 comme en HTTP/1.0.
            handler.close_connection = True
        upstream_origin = f"http://127.0.0.1:{port}"
        for name, value in response.getheaders():
            lowered = name.lower()
            if lowered in _HOP_BY_HOP_HEADERS:
                continue
            if lowered == "location" and value.startswith(upstream_origin + "/"):
                # Redirection absolue de v2 (ex. ``/mcp`` → ``/mcp/``) : jamais
                # l'adresse interne, injoignable derrière le tunnel.
                value = value[len(upstream_origin):]
            handler.send_header(name, value)
        handler.end_headers()
        if include_body:
            content_type = (response.getheader("Content-Type") or "").lower()
            is_event_stream = content_type.startswith("text/event-stream")
            # SSE : lire par très petits blocs pour ne jamais retenir un
            # évènement en attendant d'en accumuler d'autres — http.client
            # ne rend la main qu'une fois ``amt`` octets obtenus, y compris
            # au travers de plusieurs chunks HTTP ; un ``amt`` minimal évite
            # ce blocage et vide chaque évènement au fil de l'eau. Les
            # réponses ordinaires utilisent un tampon large, sans risque
            # puisqu'elles restent bornées par MAX_PROXY_BODY_BYTES côté
            # amont applicatif.
            block_size = 1 if is_event_stream else 65536
            while True:
                chunk = response.read(block_size)
                if not chunk:
                    break
                handler.wfile.write(chunk)
                if is_event_stream:
                    handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        handler.close_connection = True
    finally:
        connection.close()


def _authority_parts(authority: str) -> tuple[str, str | None] | None:
    """Décompose `host[:port]` sans accepter d'espace, d'identifiant ni de chemin."""

    text = authority.strip()
    if not text or any(character in text for character in " \t\r\n/\\?#@"):
        return None
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            return None
        host = text[1:end]
        remainder = text[end + 1 :]
        if remainder == "":
            port = None
        elif remainder.startswith(":"):
            port = remainder[1:]
        else:
            return None
    elif ":" in text:
        host, _, port = text.rpartition(":")
    else:
        host, port = text, None
    if not host:
        return None
    if port is not None and (not port.isdigit() or not 0 < int(port) <= 65535):
        return None
    return host, port


def authority_stays_loopback(authority: str) -> bool:
    """Vrai si un en-tête Host vise une adresse loopback (anti-DNS rebinding)."""

    parts = _authority_parts(authority)
    if parts is None:
        return False
    return is_loopback_host(parts[0])


def origin_stays_loopback(origin: str) -> bool:
    """Vrai si une origine navigateur reste locale, donc non hostile.

    L'interface servie en same-origin et le proxy Vite de développement sont
    deux origines loopback ; une page web distante ne peut pas en produire une,
    même en forgeant Host ou Origin.
    """

    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if port is not None and not 0 < port <= 65535:
        return False
    host = parsed.hostname
    if not host:
        return False
    return is_loopback_host(host)


def post_violation(headers: Mapping[str, str], *, authenticated_proxy: bool = False) -> tuple[HTTPStatus, str] | None:
    """Contrôle une écriture (POST) avant toute lecture du corps.

    Fail-closed : un Host non loopback, une origine distante, une requête
    inter-site ou un corps non JSON sont refusés sans exécuteur ni lecture.
    Derrière un proxy d'authentification (``authenticated_proxy``), le Host
    public et l'origine du site sont légitimes : le contrôle loopback est
    délégué au proxy et seul le type de corps reste vérifié ici.
    """

    if not authenticated_proxy:
        if not authority_stays_loopback(headers.get("Host") or ""):
            return HTTPStatus.FORBIDDEN, "invalid_host"
        origin = headers.get("Origin")
        if origin is not None and not origin_stays_loopback(origin):
            return HTTPStatus.FORBIDDEN, "forbidden_origin"
        fetch_site = headers.get("Sec-Fetch-Site")
        if fetch_site is not None and fetch_site.strip().lower() not in ALLOWED_FETCH_SITES:
            return HTTPStatus.FORBIDDEN, "forbidden_origin"
    media_type = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if media_type != POST_MEDIA_TYPE:
        return HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "unsupported_media_type"
    return None


def serve(
    repository: ProjectionRepository,
    host: str = "127.0.0.1",
    port: int = 8844,
    *,
    ui_dist: str | Path | None = None,
    action_executor: object | None = None,
    auth: AuthConfig | None = None,
    connections_store: ConnectionsStore | None = None,
    audit_log: ActionAuditLog | None = None,
    v2_upstream_port: int | None = None,
) -> ThreadingHTTPServer:
    """Construit un serveur sans démarrer sa boucle, afin de faciliter son intégration.

    ``connections_store`` reste optionnel : sans magasin fourni, les routes
    ``/v1/connections`` se comportent comme toute route ``/v1/`` inconnue —
    aucune régression pour un déploiement qui ne le câble pas encore.

    ``v2_upstream_port`` reste optionnel : sans lui, ``/v2/*`` et ``/mcp``
    se comportent exactement comme avant (404 ou fallback SPA) — aucune
    régression pour un déploiement qui ne câble pas le control plane v2.
    Fourni, il active un relais reverse-proxy minimal vers
    ``http://127.0.0.1:<port>`` (voir ``_proxy_to_v2``).
    """
    if not is_loopback_host(host) and auth is None:
        raise ValueError("Le control plane sans authentification doit rester sur loopback")
    if action_executor is not None and audit_log is None:
        raise ValueError("Un journal durable est obligatoire pour les actions")
    if v2_upstream_port is not None and not 0 < v2_upstream_port <= 65535:
        raise ValueError("v2_upstream_port doit être un port TCP valide")
    dist = Path(ui_dist).resolve() if ui_dist is not None else None
    if dist is not None and not dist.is_dir():
        raise ValueError("ui_dist must be an existing directory")
    handler = _handler_for(
        repository,
        ui_dist=dist,
        action_executor=action_executor,
        auth=auth,
        connections_store=connections_store,
        audit_log=audit_log,
        v2_upstream_port=v2_upstream_port,
    )
    return ThreadingHTTPServer((host, port), handler)


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolve_ui_path(ui_dist: Path, url_path: str) -> Path | None:
    decoded = unquote(url_path)
    if ".." in decoded or "\\" in decoded or decoded.startswith("//"):
        return None
    relative = decoded.lstrip("/")
    if "fixture" in relative.lower():
        return None
    root = ui_dist.resolve()
    if not relative:
        candidate = root / "index.html"
    else:
        first = relative.split("/", 1)[0]
        if first != "assets" and relative not in {"index.html", "favicon.ico"}:
            if Path(relative).suffix:
                return None
            candidate = root / "index.html"
        else:
            candidate = root / relative
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _handler_for(
    repository: ProjectionRepository,
    *,
    ui_dist: Path | None,
    action_executor: object | None = None,
    auth: AuthConfig | None = None,
    connections_store: ConnectionsStore | None = None,
    audit_log: ActionAuditLog | None = None,
    v2_upstream_port: int | None = None,
) -> Type[BaseHTTPRequestHandler]:
    action_gate = PipelineActionGate()

    class ControlPlaneHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _maybe_proxy(self, *, include_body: bool) -> bool:
            """Court-circuite tout le reste (gardes, SPA) pour /v2 et /mcp.

            v2 porte sa propre authentification (jetons, cookies de
            session) : le garde v1 par en-têtes de proxy (``_gate``) ne
            s'applique pas à ces chemins, et le fallback SPA (``_static``)
            ne doit jamais les intercepter. D'où l'appel en tout premier,
            avant ``_gate()``, dans chaque point d'entrée HTTP.
            """
            if v2_upstream_port is None:
                return False
            if not _proxied_path(urlparse(self.path).path):
                return False
            _proxy_to_v2(self, v2_upstream_port, include_body=include_body)
            return True

        def _gate(self) -> bool:
            """Authentifie et autorise ; répond et renvoie ``True`` sur refus."""
            self._actor = "local-port-forward"
            if auth is None:
                return False
            identity = authenticate(self.headers, auth)
            if identity is None:
                self._error(HTTPStatus.UNAUTHORIZED, "authentication_required", True)
                return True
            self._actor = identity.subject
            violation = authorize(identity, self.command)
            if violation is not None:
                self._error(violation[0], violation[1], True)
                return True
            return False

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch(include_body=True)

        def do_HEAD(self) -> None:  # noqa: N802
            self._dispatch(include_body=False)

        def do_POST(self) -> None:  # noqa: N802
            if self._maybe_proxy(include_body=True):
                return
            if self._gate():
                return
            path = urlparse(self.path).path
            if path == "/v1/onboarding/evaluate":
                self._onboarding_evaluate()
                return
            if path == "/v1/connections" and connections_store is not None:
                self._connections_create()
                return
            action_route = parse_action_route(path)
            if action_route is not None:
                self._pipeline_action(action_route[0], action_route[1])
                return
            self._method_not_allowed()

        def _reject(self) -> None:
            if self._maybe_proxy(include_body=True):
                return
            if self._gate():
                return
            self._method_not_allowed()

        def do_PUT(self) -> None:  # noqa: N802
            self._reject()

        def do_PATCH(self) -> None:  # noqa: N802
            self._reject()

        def do_DELETE(self) -> None:  # noqa: N802
            self._reject()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._reject()

        def __getattr__(self, name: str) -> object:
            if name.startswith("do_"):
                return self._reject
            raise AttributeError(name)

        def finish(self) -> None:
            try:
                super().finish()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def handle_one_request(self) -> None:
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def log_message(self, format: str, *args: object) -> None:
            """Ne journalise pas les routes ou identifiants contrôlés par le client."""

        def _dispatch(self, *, include_body: bool) -> None:
            if self._maybe_proxy(include_body=include_body):
                return
            path = urlparse(self.path).path
            # Sonde d'orchestrateur : exempte d'authentification, aucune donnée —
            # un 200 nu qui ne révèle rien et ne lit aucune preuve.
            if path == "/healthz":
                self._healthz(include_body)
                return
            if self._gate():
                return
            if parse_action_route(path) is not None:
                self._method_not_allowed()
                return
            if path == "/v1/events":
                if not include_body:
                    self._sse_headers()
                    return
                self._events()
                return
            snapshot = repository.snapshot()
            if path == "/v1/version":
                from .version import version
                self._json({"version": version(), "api": "v1"}, snapshot, include_body)
                return
            if path == "/v1/overview":
                overview = snapshot.to_dict()
                overview["pipelines"] = [
                    _pipeline_payload(pipeline, action_executor, audit_available=audit_log is None or audit_log.healthy) for pipeline in snapshot.pipelines
                ]
                self._json(overview, snapshot, include_body)
                return
            if path == "/v1/pipelines":
                self._json(
                    {
                        "revision": snapshot.revision,
                        "pipelines": [
                            _pipeline_payload(pipeline, action_executor, audit_available=audit_log is None or audit_log.healthy)
                            for pipeline in snapshot.pipelines
                        ],
                    },
                    snapshot,
                    include_body,
                )
                return
            prefix = "/v1/pipelines/"
            if path.startswith(prefix) and path[len(prefix):] and "/" not in path[len(prefix):]:
                requested_id = unquote(path[len(prefix):])
                pipeline = next((item for item in snapshot.pipelines if item.id == requested_id), None)
                if pipeline is None:
                    self._error(HTTPStatus.NOT_FOUND, "not_found", include_body)
                    return
                self._json(
                    {
                        "revision": snapshot.revision,
                        "pipeline": _pipeline_payload(pipeline, action_executor, audit_available=audit_log is None or audit_log.healthy),
                    },
                    snapshot,
                    include_body,
                )
                return
            if path == "/v1/onboarding/defaults":
                self._json_raw(
                    {
                        "defaults": _onboarding.DEFAULTS,
                        "site": _onboarding.SITE_IDENTITY,
                    },
                    include_body,
                )
                return
            if path == "/v1/connections" and connections_store is not None:
                self._connections_list(include_body)
                return
            if path.startswith("/v1/"):
                self._error(HTTPStatus.NOT_FOUND, "not_found", include_body)
                return
            if ui_dist is not None:
                self._static(path, include_body)
                return
            self._error(HTTPStatus.NOT_FOUND, "not_found", include_body)

        def _json(self, value: dict[str, object], snapshot: ProjectionSnapshot, include_body: bool) -> None:
            etag = _etag(snapshot, value)
            if self.headers.get("If-None-Match") == etag:
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", etag)
                self._security_headers()
                self.end_headers()
                return
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("ETag", etag)
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            self.end_headers()
            if include_body:
                self.wfile.write(body)

        def _json_raw(self, value: Mapping[str, object], include_body: bool, *, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            self.end_headers()
            if include_body:
                self.wfile.write(body)

        def _error(self, status: HTTPStatus, code: str, include_body: bool) -> None:
            body = json.dumps({"error": {"code": code}}, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            self.end_headers()
            if include_body:
                self.wfile.write(body)

        def _healthz(self, include_body: bool) -> None:
            body = b'{"status":"ok"}'
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            self.end_headers()
            if include_body:
                self.wfile.write(body)

        def _method_not_allowed(self) -> None:
            path = urlparse(self.path).path
            if path == "/v1/connections" and connections_store is not None:
                allow = "GET, HEAD, POST"
            elif path == "/v1/onboarding/evaluate" or parse_action_route(path) is not None:
                allow = "POST"
            else:
                allow = "GET, HEAD"
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", allow)
            self.send_header("Content-Length", "0")
            self._security_headers()
            self.end_headers()

        def _security_headers(self) -> None:
            for name, value in SECURITY_HEADERS:
                self.send_header(name, value)

        def _sse_headers(self) -> bool:
            try:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Connection", "keep-alive")
                self._security_headers()
                self.end_headers()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        def _events(self) -> None:
            try:
                cursor = _last_event_id(self.headers.get("Last-Event-ID"))
            except ValueError:
                self._error(HTTPStatus.BAD_REQUEST, "invalid_cursor", True)
                return
            if not self._sse_headers():
                return
            snapshot = repository.snapshot()
            if not self._write_event("stream.cursor", snapshot):
                return
            if cursor is None:
                cursor = snapshot.revision
            else:
                event, snapshots = repository.events_after(cursor)
                if event != "cursor":
                    for snapshot in snapshots:
                        if not self._write_event(f"projection.{event}", snapshot):
                            return
                    cursor = snapshots[-1].revision
            while True:
                snapshot = repository.wait_after(cursor, KEEPALIVE_SECONDS)
                if snapshot.revision > cursor:
                    if not self._write_event("projection.updated", snapshot):
                        return
                    cursor = snapshot.revision
                else:
                    if not _write_sse(self.wfile, b": keepalive\n\n"):
                        return

        def _write_event(self, event: str, snapshot: ProjectionSnapshot) -> bool:
            payload = json.dumps({"revision": snapshot.revision}, separators=(",", ":")).encode("utf-8")
            frame = b"id: " + str(snapshot.revision).encode("ascii") + b"\nevent: " + event.encode("ascii") + b"\ndata: " + payload + b"\n\n"
            return _write_sse(self.wfile, frame)

        def _guard_unsafe_post(self) -> bool:
            violation = post_violation(self.headers, authenticated_proxy=auth is not None)
            if violation is None:
                return False
            status, code = violation
            # Le corps déclaré n'est pas lu : fermer la connexion évite de
            # désynchroniser une connexion keep-alive.
            self.close_connection = True
            self._error(status, code, True)
            return True

        def _pipeline_action(self, pipeline_id: str, action: str) -> None:
            if self._guard_unsafe_post():
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._error(HTTPStatus.BAD_REQUEST, "invalid_request", True)
                return
            if length <= 0 or length > MAX_ACTION_BYTES:
                self._error(HTTPStatus.BAD_REQUEST, "invalid_request", True)
                return
            raw = self.rfile.read(length)
            audit_id = None
            if audit_log is not None:
                try:
                    audit_id = audit_log.begin(action, pipeline_id, self._actor)
                except OSError:
                    self._error(HTTPStatus.SERVICE_UNAVAILABLE, "audit_unavailable", True)
                    return
            result = execute_pipeline_action(
                raw_body=raw,
                pipeline_id=pipeline_id,
                action=action,
                snapshot=repository.snapshot(),
                executor=action_executor,
                gate=action_gate,
            )
            if audit_log is not None and audit_id is not None:
                try:
                    audit_log.finish(audit_id, int(result.status), result.body)
                except OSError:
                    self._error(HTTPStatus.SERVICE_UNAVAILABLE, "audit_result_unavailable", True)
                    return
            self._json_raw(result.body, True, status=result.status)

        def _onboarding_evaluate(self) -> None:
            if self._guard_unsafe_post():
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._error(HTTPStatus.BAD_REQUEST, "invalid_request", True)
                return
            if length <= 0 or length > MAX_ONBOARDING_BYTES:
                self._error(HTTPStatus.BAD_REQUEST, "invalid_request", True)
                return
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._error(HTTPStatus.BAD_REQUEST, "invalid_request", True)
                return
            if not isinstance(payload, dict):
                self._error(HTTPStatus.BAD_REQUEST, "invalid_request", True)
                return
            verdict = evaluate_onboarding(payload)
            self._json_raw(verdict, True)

        def _connections_list(self, include_body: bool) -> None:
            assert connections_store is not None
            try:
                records = connections_store.list()
            except ConnectionsError:
                self._error(
                    HTTPStatus.SERVICE_UNAVAILABLE, "connections_store_unavailable", include_body
                )
                return
            self._json_raw(
                {"connections": [record.to_dict() for record in records]},
                include_body,
            )

        def _connections_create(self) -> None:
            assert connections_store is not None
            if self._guard_unsafe_post():
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._error(HTTPStatus.BAD_REQUEST, "invalid_request", True)
                return
            if length <= 0 or length > MAX_CONNECTIONS_BYTES:
                self._error(HTTPStatus.BAD_REQUEST, "invalid_request", True)
                return
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._error(HTTPStatus.BAD_REQUEST, "invalid_request", True)
                return
            if not isinstance(payload, dict):
                self._error(HTTPStatus.BAD_REQUEST, "invalid_request", True)
                return
            # La validation réutilise entièrement evaluate_onboarding() : le
            # "step" déclaré par le client est ignoré et forcé à
            # "destination" — le seul palier qui couvre la totalité des
            # champs qu'une liaison exige (source, permissions, journal,
            # destination). Un corps invalide reçoit le verdict d'évaluation
            # tel quel, jamais une trace technique.
            verdict = evaluate_onboarding({**payload, "step": "destination"})
            if verdict["errors"] or verdict["blocked"]:
                self._json_raw(verdict, True, status=HTTPStatus.BAD_REQUEST)
                return
            declared_selection = payload.get("tables")
            if declared_selection is None and payload.get("table"):
                declared_selection = [payload["table"]]
            selection, _selection_errors = parse_table_selection(declared_selection)
            display_name = payload.get("display_name")
            site = _site()
            try:
                record = connections_store.create(
                    site_id=site.site_id,
                    display_name=display_name if isinstance(display_name, str) else "",
                    ibmi_host=_connections_text(payload.get("ibmi_host")),
                    ibmi_user=_connections_text(payload.get("ibmi_user")),
                    snowflake_account=site.snowflake_account,
                    destination_database=site.destination_database,
                    destination_schema=site.destination_schema,
                    tables=selection,
                    secret_ref_name=_connections_text(payload.get("secret_ref_name")),
                    secret_ref_key=_connections_text(payload.get("secret_ref_key")),
                )
            except ConnectionsStorageError:
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "connections_store_unavailable", True)
                return
            except ConnectionsError:
                # evaluate_onboarding ne couvre pas display_name (propre à
                # cette route) : un champ hors contrat reste un refus
                # explicite, jamais une trace technique.
                self._error(HTTPStatus.BAD_REQUEST, "invalid_connection", True)
                return
            self._json_raw({"connection": record.to_dict()}, True, status=HTTPStatus.CREATED)

        def _static(self, path: str, include_body: bool) -> None:
            assert ui_dist is not None
            target = resolve_ui_path(ui_dist, path)
            if target is None:
                self._error(HTTPStatus.NOT_FOUND, "not_found", include_body)
                return
            body = target.read_bytes()
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if target.suffix == ".js":
                content_type = "text/javascript; charset=utf-8"
            elif target.suffix == ".css":
                content_type = "text/css; charset=utf-8"
            elif target.suffix in {".html", ".htm"}:
                content_type = "text/html; charset=utf-8"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            self.end_headers()
            if include_body:
                self.wfile.write(body)

    return ControlPlaneHandler


def _pipeline_payload(pipeline: object, executor: object | None, *, audit_available: bool = True) -> dict[str, object]:
    payload = pipeline.to_dict()
    if not audit_available:
        for key in ("fleet", "fleet_runtime"):
            section = payload.get(key)
            if isinstance(section, dict):
                payload[key] = {**section, "capabilities": {
                    name: {"state": "unavailable", "reason": "audit_unavailable"}
                    for name in section.get("capabilities", {})
                }}
        return payload
    runtime = _safe_runtime_projection(executor)
    if (
        runtime is not None
        and _runtime_matches_pipeline(runtime, pipeline)
        and _has_fleet_catalog(payload)
    ):
        payload["fleet_runtime"] = runtime
        _overlay_catalog_capabilities(payload, runtime["capabilities"])
        return payload
    return _apply_executor_capabilities(payload, pipeline, executor)


def _safe_runtime_projection(executor: object | None) -> dict[str, object] | None:
    if executor is None:
        return None
    project = getattr(executor, "project", None)
    if not callable(project):
        return None
    try:
        raw = project()
    except Exception:
        return None
    try:
        return _validate_runtime_projection(raw)
    except Exception:
        return None


def _validate_runtime_projection(raw: object) -> dict[str, object] | None:
    if type(raw) is not dict:
        return None
    if tuple(raw) != RUNTIME_KEYS and set(raw) != set(RUNTIME_KEYS):
        return None
    if _contains_sensitive(raw):
        return None
    format_version = raw.get("format_version")
    fleet_id = raw.get("fleet_id")
    environment = raw.get("environment")
    pipeline_id = raw.get("pipeline_id")
    phase = raw.get("phase")
    if format_version != RUNTIME_FORMAT_VERSION:
        return None
    if fleet_id != _model.FLEET_ID:
        return None
    if environment != _site().environment:
        return None
    if pipeline_id != _site().site_id:
        return None
    if phase not in RUNTIME_PHASES:
        return None
    checkpoint = _validate_runtime_checkpoint(raw.get("checkpoint"))
    if checkpoint is False:
        return None
    capabilities = _validate_runtime_capabilities(raw.get("capabilities"))
    if capabilities is None:
        return None
    table_states = _validate_runtime_table_states(raw.get("table_states"), phase)
    if table_states is None:
        return None
    return {
        "format_version": RUNTIME_FORMAT_VERSION,
        "fleet_id": _model.FLEET_ID,
        "environment": _site().environment,
        "pipeline_id": _site().site_id,
        "phase": phase,
        "checkpoint": checkpoint,
        "capabilities": capabilities,
        "table_states": table_states,
    }


def _validate_runtime_checkpoint(value: object) -> dict[str, object] | None | bool:
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"receiver", "sequence"}:
        return False
    if _contains_sensitive(value):
        return False
    receiver = value.get("receiver")
    sequence = value.get("sequence")
    if type(receiver) is not str or not receiver.strip() or receiver != receiver.strip():
        return False
    if type(sequence) is not int or type(sequence) is bool or sequence < 0:
        return False
    return {"receiver": receiver, "sequence": sequence}


def _validate_runtime_capabilities(value: object) -> dict[str, dict[str, object]] | None:
    if type(value) is not dict or set(value) != set(RUNTIME_ACTIONS):
        return None
    if _contains_sensitive(value):
        return None
    capabilities: dict[str, dict[str, object]] = {}
    for action in RUNTIME_ACTIONS:
        current = value.get(action)
        if type(current) is not dict or set(current) != {"state", "reason"}:
            return None
        state = current.get("state")
        reason = current.get("reason")
        if state == "available":
            if reason is not None:
                return None
        elif state == "unavailable":
            if type(reason) is not str or not reason.strip() or reason != reason.strip():
                return None
            if _contains_sensitive(reason):
                return None
        else:
            return None
        capabilities[action] = {"state": state, "reason": reason}
    return capabilities


def _validate_runtime_table_states(value: object, phase: str) -> list[dict[str, object]] | None:
    if type(value) is not list or len(value) != len(_fleet.MANIFEST):
        return None
    tables: list[dict[str, object]] = []
    for index, item in enumerate(value):
        if type(item) is not dict or set(item) != {"name", "phase", "copied_rows", "total_rows"}:
            return None
        if _contains_sensitive(item):
            return None
        name = item.get("name")
        table_phase = item.get("phase")
        copied = item.get("copied_rows")
        total = item.get("total_rows")
        if name != _fleet.MANIFEST[index]:
            return None
        # La phase de voie est réelle : elle suit la phase agrégée quand
        # le run domaine est absent, et peut la précéder ou la retarder
        # quand les voies n'avancent pas au même pas.
        if table_phase not in TABLE_PHASES:
            return None
        for count in (copied, total):
            if count is not None and (
                type(count) is not int or type(count) is bool or count < 0
            ):
                return None
        if copied is not None and total is not None and copied > total:
            return None
        tables.append(
            {"name": name, "phase": table_phase, "copied_rows": copied, "total_rows": total}
        )
    return tables


def _runtime_matches_pipeline(runtime: Mapping[str, object], pipeline: object) -> bool:
    pipeline_id = getattr(pipeline, "id", None)
    environment = getattr(pipeline, "environment", None)
    if pipeline_id != runtime.get("pipeline_id") or pipeline_id != _site().site_id:
        return False
    if type(environment) is not str or environment.casefold() != _site().environment:
        return False
    return True


def _has_fleet_catalog(payload: Mapping[str, object]) -> bool:
    return isinstance(payload.get("fleet"), dict) or isinstance(payload.get("fleet_plan"), dict)


def _overlay_catalog_capabilities(payload: dict[str, object], capabilities: Mapping[str, object]) -> None:
    fleet = payload.get("fleet")
    if not isinstance(fleet, dict):
        return
    updated = dict(fleet)
    updated["capabilities"] = _copy_capabilities(capabilities)
    payload["fleet"] = updated


def _copy_capabilities(value: Mapping[str, object]) -> dict[str, dict[str, object]]:
    copied: dict[str, dict[str, object]] = {}
    for name, capability in value.items():
        if isinstance(capability, Mapping):
            copied[str(name)] = dict(capability)
    return copied


def _apply_executor_capabilities(
    payload: dict[str, object],
    pipeline: object,
    executor: object | None,
) -> dict[str, object]:
    fleet = payload.get("fleet")
    if not isinstance(fleet, dict):
        return payload
    fleet = dict(fleet)
    payload["fleet"] = fleet
    capabilities = fleet.get("capabilities")
    if not isinstance(capabilities, dict):
        return payload
    updated = dict(capabilities)
    for action in RUNTIME_ACTIONS:
        current = updated.get(action)
        reason = current.get("reason") if isinstance(current, Mapping) else None
        if reason in {"stale_proof", "not_live"}:
            continue
        invocation = PipelineActionInvocation(
            pipeline_id=str(getattr(pipeline, "id")),
            action=action,
            fleet_id=_model.FLEET_ID,
            environment=_site().environment,
        )
        if executor_supports(executor, invocation):
            updated[action] = {"state": "available", "reason": None}
    fleet["capabilities"] = updated
    return payload


def _contains_sensitive(value: object) -> bool:
    if type(value) is str:
        lowered = value.lower()
        return any(token in lowered for token in _SENSITIVE_TOKENS) or "://" in value
    if type(value) is dict:
        return any(_contains_sensitive(key) or _contains_sensitive(item) for key, item in value.items())
    if type(value) is list:
        return any(_contains_sensitive(item) for item in value)
    return False


def _etag(snapshot: ProjectionSnapshot, payload: Mapping[str, object] | None = None) -> str:
    # Revisions are process-local; a restart can reuse one for different data.
    document = snapshot.to_dict() if payload is None else payload
    content = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f'"projection-{snapshot.revision}-{hashlib.sha256(content).hexdigest()}"'


def _last_event_id(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    if not value.isdecimal():
        raise ValueError("invalid cursor")
    return int(value)


def _write_sse(writer: object, frame: bytes) -> bool:
    """Écrit un frame SSE; une déconnexion client termine seulement ce flux."""
    try:
        writer.write(frame)  # type: ignore[attr-defined]
        writer.flush()  # type: ignore[attr-defined]
        return True
    except (BrokenPipeError, ConnectionResetError, OSError):
        return False


def _connections_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""
