#!/usr/bin/env python3
"""Point d'entrée du Job Kubernetes éphémère de sonde de source IBM i.

Lancé par ``KubernetesJobSourceProbe``
(``quadringent_control_plane.v2.executor.diagnostic_jobs``) avec l'image de
capture. Mesure, dans l'ordre (chaque étape ne s'exécute que si la
précédente a réussi — un réseau injoignable ne tente jamais un handshake
TLS) :

1. réseau : connexion TCP au serveur hôte IBM i (service ``as-signon``,
   sign-on) ;
2. TLS : poignée de main, empreinte SHA-256 du certificat présenté,
   comparée à ``--pinned-fingerprint`` si ``--tls-trust=pinned`` (jamais de
   confiance implicite au-delà de la racine système sinon) ;
3. authentification + version IBM i + ``QTIMZON`` via **JTOpen** (pilote
   Java du produit — jamais ODBC/pyodbc, propriétaire et absent de l'image)
   : ``quadringent.java_worker.PersistentJavaWorker`` ouvre la connexion
   JDBC JTOpen (même worker que ``quadringent_table_discovery_job.py``),
   l'authentification est prouvée par cette ouverture elle-même
   (``connect_error`` sinon), puis la commande ``probe`` (ajoutée à
   ``DiagnosticWorker``, voir ``SourceInfo.java``) interroge
   ``QSYS2.SYSTEM_STATUS_INFO``/``QSYS2.SYSTEM_VALUE_INFO`` — deux vues
   catalogue IBM i standard, jamais de ligne métier.

Les étapes 1-2 (réseau/TLS) restent une mesure indépendante en Python pur
(``socket``/``ssl``, sans dépendance externe) contre le service
``as-signon`` — un signal utile même si l'étape 3 échoue ensuite, et qui ne
préjuge pas du port effectivement utilisé par la connexion JDBC JTOpen
(négocié par le pilote via le répartiteur de services IBM i, port 449).

Numéros de port des serveurs hôtes IBM i (« host servers ») : ``as-signon``
écoute en clair sur 8476, en TLS sur 9476 — convention documentée IBM
(rubrique « Host servers » du IBM i Knowledge Center). Ajustable via
``--port`` si un site en décide autrement (répartiteur, pare-feu).

Le mot de passe n'est **jamais** un argument de commande ni journalisé : il
est lu depuis la variable d'environnement ``ISERIES_PASSWORD`` (Secret
Kubernetes éphémère, ``envFrom`` — voir
``executor/manifests.py::build_source_probe_job``). Le résultat (JSON, une
ligne) est imprimé sur stdout, préfixé par ``quadringent_probe_result=``,
et ne contient jamais le mot de passe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import ssl
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quadringent.java_worker import PersistentJavaWorker

from quadringent_control_plane.v2.services.source_probe import (
    ProbeOutcome,
    SourceProbeResult,
    build_probe_result,
)

DEFAULT_PLAINTEXT_PORT = 8476
DEFAULT_TLS_PORT = 9476
DEFAULT_TIMEOUT_SECONDS = 10.0
RESULT_PREFIX = "quadringent_probe_result="


def tcp_probe(host: str, port: int, *, timeout: float, connect=socket.create_connection) -> ProbeOutcome:
    """Connexion TCP brute — jamais de TLS ni de protocole applicatif ici."""

    try:
        sock = connect((host, port), timeout=timeout)
    except OSError as error:
        return ProbeOutcome(ok=False, detail=f"connexion TCP refusée/injoignable : {error}")
    sock.close()
    return ProbeOutcome(ok=True, detail=f"connexion TCP établie sur {port}")


def _fingerprint(der_certificate: bytes) -> str:
    return hashlib.sha256(der_certificate).hexdigest()


def _certificate_pem(der_certificate: bytes) -> str:
    return ssl.DER_cert_to_PEM_cert(der_certificate)


def tls_probe(
    host: str,
    port: int,
    *,
    timeout: float,
    ca_file: str | None,
    tls_trust: str,
    pinned_fingerprint: str | None,
    connect=socket.create_connection,
    wrap_socket=None,
) -> tuple[ProbeOutcome, str | None, str | None, str | None]:
    """Poignée de main TLS + empreinte/PEM du certificat présenté.

    Rend ``(outcome, fingerprint, trust, certificate_pem)``.

    ``ca_file`` déclaré (``AS400_TLS_CA_FILE`` posé) mais introuvable — le
    bug constaté en production (fichier absent de l'image) — est un échec
    explicite ici, jamais une exception non rattrapée : sans ce garde-fou,
    ``ssl.create_default_context(cafile=...)`` lèverait ``FileNotFoundError``
    et le Job planterait sans ligne de résultat.

    ``tls_trust="pinned"`` compare l'empreinte mesurée à
    ``pinned_fingerprint`` — la connexion réussit même si la racine système
    ne connaît pas l'autorité (c'est tout le sens de l'épinglage), mais une
    empreinte qui diffère est un échec explicite, jamais une alerte
    silencieuse.

    ``tls_trust="system"`` valide la chaîne contre le magasin système (ou
    ``ca_file`` s'il est fourni — cas d'une autorité privée déjà épinglée et
    montée). Si — et seulement si — cette validation échoue précisément à
    cause d'une autorité non reconnue (``ssl.SSLCertVerificationError``),
    une seconde poignée de main est tentée sans validation de chaîne, dans
    le seul but de *mesurer* l'empreinte/le certificat présenté : cette
    connexion n'authentifie jamais rien, elle rend ``trust="unknown"`` avec
    l'empreinte et le PEM pour qu'un opérateur décide (épinglage) — jamais
    acceptée silencieusement comme une racine connue. Toute autre erreur
    (réseau, protocole, timeout) reste un échec sans mesure.
    """

    if ca_file is not None and not os.path.isfile(ca_file):
        return (
            ProbeOutcome(ok=False, detail=f"CA TLS déclarée introuvable : {ca_file}"),
            None,
            None,
            None,
        )

    def _attempt(*, verify: bool) -> tuple[bytes | None, BaseException | None]:
        context = ssl.create_default_context(cafile=ca_file)
        if not verify:
            # Mesure seule : jamais utilisée pour authentifier quoi que ce
            # soit (voir docstring) — désactive uniquement la validation de
            # chaîne, jamais le TLS lui-même.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        try:
            raw_sock = connect((host, port), timeout=timeout)
            wrapper = wrap_socket or context.wrap_socket
            with wrapper(raw_sock, server_hostname=host) as tls_sock:
                return tls_sock.getpeercert(binary_form=True), None
        except (OSError, ssl.SSLError) as error:
            return None, error

    if tls_trust == "pinned":
        der_certificate, error = _attempt(verify=False)
        if error is not None:
            return ProbeOutcome(ok=False, detail=f"poignée de main TLS échouée : {error}"), None, None, None
        if not der_certificate:
            return ProbeOutcome(ok=False, detail="certificat serveur absent"), None, None, None
        fingerprint = _fingerprint(der_certificate)
        pem = _certificate_pem(der_certificate)
        expected = (pinned_fingerprint or "").strip().lower().replace(":", "")
        if expected and fingerprint != expected:
            return (
                ProbeOutcome(ok=False, detail="empreinte du certificat différente de l'empreinte épinglée"),
                fingerprint,
                "pinned",
                pem,
            )
        return (
            ProbeOutcome(ok=True, detail="poignée de main TLS réussie (empreinte épinglée confirmée)"),
            fingerprint,
            "pinned",
            pem,
        )

    # tls_trust == "system" : validation standard d'abord, jamais de
    # confiance implicite au-delà de la racine système/CA fournie.
    der_certificate, error = _attempt(verify=True)
    if error is not None:
        if isinstance(error, ssl.SSLCertVerificationError):
            measured, measure_error = _attempt(verify=False)
            if measure_error is not None or not measured:
                return ProbeOutcome(ok=False, detail=f"poignée de main TLS échouée : {error}"), None, None, None
            fingerprint = _fingerprint(measured)
            pem = _certificate_pem(measured)
            return (
                ProbeOutcome(
                    ok=True,
                    detail=(
                        "autorité non reconnue par le magasin système — mesure seule, "
                        "jamais utilisée pour authentifier ; épinglage requis"
                    ),
                ),
                fingerprint,
                "unknown",
                pem,
            )
        return ProbeOutcome(ok=False, detail=f"poignée de main TLS échouée : {error}"), None, None, None
    if not der_certificate:
        return ProbeOutcome(ok=False, detail="certificat serveur absent"), None, None, None
    fingerprint = _fingerprint(der_certificate)
    pem = _certificate_pem(der_certificate)
    return (
        ProbeOutcome(ok=True, detail="poignée de main TLS réussie (autorité publique reconnue)"),
        fingerprint,
        "system",
        pem,
    )


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} manquant")
    return value


def parse_probe_output(raw: str) -> tuple[str | None, str | None]:
    """Parse le protocole ligne de la commande worker ``probe`` (voir
    ``SourceInfo.java::write``) : ``version\\t<V.R>`` puis ``qtimzon\\t<valeur>``,
    champ vide (jamais absent) si la mesure est indisponible. Logique pure,
    testée sans connexion IBM i."""

    version: str | None = None
    qtimzon: str | None = None
    for line in raw.splitlines():
        key, sep, value = line.partition("\t")
        if not sep:
            continue
        value = value.strip() or None
        if key == "version":
            version = value
        elif key == "qtimzon":
            qtimzon = value
    return version, qtimzon



def _worker_failure_detail(error: BaseException) -> str:
    """Seuls les refus reconnus par le classifieur du worker sont présentés
    comme des refus d'authentification ou de configuration ; tout le reste
    est une sonde indisponible (jamais le message brut : il pourrait citer
    l'environnement)."""
    from quadringent.java_worker import IbmiUserDisabledError, SourceAuthenticationBlockedError
    from quadringent.source_gate import SourceConfigurationBlockedError

    if isinstance(error, IbmiUserDisabledError):
        return "profil IBM i désactivé"
    if isinstance(error, SourceAuthenticationBlockedError):
        return "authentification refusée"
    if isinstance(error, SourceConfigurationBlockedError):
        return "configuration source invalide (voir les journaux du Job)"
    return f"sonde indisponible ({type(error).__name__}) — voir les journaux du Job"

def _default_worker_factory(host: str, user: str, timeout: float) -> "PersistentJavaWorker":
    """Construction par défaut du worker Java pour ``authenticate_and_probe``.

    ``probe`` n'exige ni schema ni table : le worker Java de diagnostic
    (``DiagnosticWorker`` par défaut) ne les lit jamais — aucune valeur
    fictive à passer au constructeur. Nom de classe exposé pour les tests
    (voir ``tests/test_quadringent_source_probe_job.py``).
    """
    from quadringent.java_worker import PersistentJavaWorker

    return PersistentJavaWorker(
        java=os.environ.get("AS400_JAVA", "java"),
        classpath=_required_env("AS400_JAVA_CLASSPATH"),
        host=host,
        user=user,
        timeout_seconds=timeout,
        # Jamais ``AS400_JAVA_WORKER_CLASS`` : l'image capture le fixe au worker
        # de capture, qui exige table, journal et fuseau.
        class_name=os.environ.get("AS400_DIAGNOSTIC_WORKER_CLASS", "io.quadringent.as400.DiagnosticWorker"),
    )


def authenticate_and_probe(
    host: str, user: str, password: str, *, timeout: float, worker_factory=None
) -> tuple[ProbeOutcome, str | None, str | None]:
    """Authentification + version IBM i + ``QTIMZON`` via JTOpen (jamais ODBC).

    ``worker_factory`` construit le worker Java (par défaut
    ``PersistentJavaWorker``, injectable pour les tests). Rend
    ``(authentication, ibmi_version, qtimzon)`` — une connexion refusée est
    le seul cas où ``ibmi_version``/``qtimzon`` restent ``None``, jamais
    devinés.
    """

    factory = worker_factory or (lambda: _default_worker_factory(host, user, timeout))
    # ``PersistentJavaWorker`` transmet le mot de passe au sous-processus
    # Java depuis l'environnement du process (``environment = os.environ.
    # copy()``, voir ``java_worker.py::_ensure_worker``) — déjà posé par le
    # Secret Kubernetes (``envFrom``) avant l'appel à cette fonction
    # (``main`` vérifie sa présence). ``password`` n'est donc pas repassé
    # explicitement ici : c'est la même variable ``ISERIES_PASSWORD``.
    try:
        worker = factory()
    except Exception as error:  # noqa: BLE001 — jamais le mot de passe dans le message
        return ProbeOutcome(ok=False, detail=_worker_failure_detail(error)), None, None
    try:
        raw_output = worker.probe()
    except Exception as error:  # noqa: BLE001 — jamais le mot de passe dans le message (voir java_worker.py)
        return ProbeOutcome(ok=False, detail=_worker_failure_detail(error)), None, None
    finally:
        worker.close()
    ibmi_version, qtimzon = parse_probe_output(raw_output)
    return ProbeOutcome(ok=True, detail="authentification réussie"), ibmi_version, qtimzon


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--tls-trust", choices=("system", "pinned"), default="system")
    parser.add_argument("--pinned-fingerprint", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    return parser.parse_args(argv)


def _run_probe(args: argparse.Namespace, port: int, password: str) -> SourceProbeResult:
    network = tcp_probe(args.host, port, timeout=args.timeout)
    tls = ProbeOutcome(ok=False, detail="non tenté (réseau injoignable)")
    fingerprint: str | None = None
    trust: str | None = None
    certificate_pem: str | None = None
    authentication = ProbeOutcome(ok=False, detail="non tentée (réseau/TLS indisponible)")
    ibmi_version: str | None = None
    qtimzon: str | None = None

    if network.ok:
        tls, fingerprint, trust, certificate_pem = tls_probe(
            args.host,
            port,
            timeout=args.timeout,
            ca_file=os.environ.get("AS400_TLS_CA_FILE"),
            tls_trust=args.tls_trust,
            pinned_fingerprint=args.pinned_fingerprint,
        )
    if network.ok and tls.ok:
        authentication, ibmi_version, qtimzon = authenticate_and_probe(
            args.host, args.user, password, timeout=args.timeout
        )

    return build_probe_result(
        network=network,
        tls=tls,
        tls_fingerprint=fingerprint,
        authentication=authentication,
        ibmi_version=ibmi_version,
        qtimzon=qtimzon,
        tls_trust=trust,
        tls_certificate_pem=certificate_pem,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    password = os.environ.get("ISERIES_PASSWORD")
    if not password:
        print("ISERIES_PASSWORD manquant (Secret non monté)", file=sys.stderr)
        return 2
    port = args.port or DEFAULT_TLS_PORT

    try:
        result = _run_probe(args, port, password)
    except Exception as error:  # noqa: BLE001 — le Job doit toujours produire une
        # ligne de résultat JSON exploitable (voir docstring du module,
        # objectif C) : un plantage nu (ex. AS400_TLS_CA_FILE inexistant
        # avant ce correctif) laissait l'appelant sans aucun signal. Le nom
        # de la classe d'exception est sûr (jamais le mot de passe ni un
        # détail réseau) ; le détail complet reste dans stderr, jamais dans
        # la ligne de résultat.
        result = build_probe_result(
            network=ProbeOutcome(ok=False, detail="sonde interrompue par une erreur inattendue"),
            tls=ProbeOutcome(ok=False, detail="non tentée"),
            tls_fingerprint=None,
            authentication=ProbeOutcome(ok=False, detail="non tentée"),
            ibmi_version=None,
            qtimzon=None,
        )
        print(RESULT_PREFIX + json.dumps(result.to_dict()))
        print(f"sonde interrompue : {type(error).__name__}", file=sys.stderr)
        return 1

    print(RESULT_PREFIX + json.dumps(result.to_dict()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
