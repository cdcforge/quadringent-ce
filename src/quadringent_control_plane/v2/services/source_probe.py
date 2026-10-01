"""Sonde IBM i réelle pour ``POST /v2/sources/{id}/test`` (chantier 4, item 2).

``SourcesService.test`` (v1 de ce module) ne validait que le déchiffrement
du secret, en attendant « un exécuteur à brancher ultérieurement » — c'est
ce module. La sonde réseau/TLS/authentification/version reste hors
périmètre Python pur (elle appelle IBM i) : elle est injectée via
``SourceProbeProtocol`` (un petit outil Java autonome ou une commande
worker dédiée, câblée ailleurs) ; ce module ne fait que la déclarer, la
consommer sans jamais l'improviser, et traduire ``QTIMZON`` en fuseau IANA
— logique pure, testée sans IBM i.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# QTIMZON -> règle de capture : valeurs les plus courantes rencontrées sur des IBM i en
# qualification/production (Europe/Amérique du Nord). Une valeur absente de
# cette table n'est jamais devinée : la réponse porte `timezone: null` et
# `timezone_ambiguous: true`, à charge pour un humain de confirmer (design
# §2.1 : « fuseau relevé depuis QTIMZON » — jamais une valeur inventée).
# Noms de base des descriptions ``*TIMZON`` livrées par IBM dans QSYS.
# ``QP0100CET`` termine l'heure d'été le dernier dimanche de septembre,
# contrairement à ``Europe/Paris`` (octobre). Le jeton ``IBM:QP0100CET``
# active cette règle dédiée dans ``JournalTimestamps``. Les variantes
# numérotées portent d'autres règles : jamais traduites sans qualification.
QTIMZON_TO_CAPTURE_ZONE: dict[str, str] = {
    "Q0000UTC": "UTC",
    "Q0000GMT": "UTC",
    "QN0330NST": "America/St_Johns",
    "QN0400AST": "America/Halifax",
    "QN0500EST": "America/New_York",
    "QN0600CST": "America/Chicago",
    "QN0700MST": "America/Denver",
    "QN0800PST": "America/Los_Angeles",
    "QN0900AST": "America/Anchorage",
    "QN1000HST": "Pacific/Honolulu",
    "QP0100CET": "IBM:QP0100CET",
    "QP0200EET": "Europe/Helsinki",
    "QP0200SAST": "Africa/Johannesburg",
    "QP0530IST": "Asia/Kolkata",
    "QP0700WIB": "Asia/Jakarta",
    "QP0800AWST": "Australia/Perth",
    "QP0900JST": "Asia/Tokyo",
    "QP0900KST": "Asia/Seoul",
    "QP0930ACST": "Australia/Adelaide",
    "QP1000AEST": "Australia/Sydney",
    "QP1200NZST": "Pacific/Auckland",
}

# Alias de compatibilité pour les appelants du catalogue précédent : certaines
# valeurs sont des jetons de règle IBM, et non des identifiants IANA.
QTIMZON_TO_IANA = QTIMZON_TO_CAPTURE_ZONE


def map_qtimzon_to_capture_zone(qtimzon: str) -> str | None:
    """Retourne une règle de capture qualifiée, ou ``None`` si inconnue."""

    return QTIMZON_TO_CAPTURE_ZONE.get(qtimzon.strip().upper())


def map_qtimzon_to_iana(qtimzon: str) -> str | None:
    """Alias historique de :func:`map_qtimzon_to_capture_zone`.

    Peut retourner un jeton ``IBM:`` lorsque les règles IBM diffèrent d'IANA.
    """

    return map_qtimzon_to_capture_zone(qtimzon)


class SourceProbeError(RuntimeError):
    """Erreur de sonde réduite à un code sûr, jamais de détail réseau brut."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProbeOutcome:
    """Résultat d'une étape (réseau, TLS, authentification)."""

    ok: bool
    detail: str


@dataclass(frozen=True)
class SourceProbeResult:
    network: ProbeOutcome
    tls: ProbeOutcome
    tls_fingerprint: str | None
    authentication: ProbeOutcome
    ibmi_version: str | None
    qtimzon: str | None
    detected_timezone: str | None
    timezone_ambiguous: bool
    # "system" (chaîne publique validée), "pinned" (empreinte épinglée
    # confirmée) ou "unknown" (poignée de main réussie mais autorité non
    # reconnue — jamais utilisée pour authentifier, seulement pour mesurer
    # l'empreinte/le certificat présenté). ``None`` si la mesure n'a jamais
    # été tentée (réseau injoignable).
    tls_trust: str | None = None
    # PEM du certificat présenté (feuille) quand ``tls_trust == "unknown"` —
    # jamais persisté tel quel sans décision explicite de l'opérateur
    # (épinglage), voir ``SourcesService``.
    tls_certificate_pem: str | None = None

    def reachable(self) -> bool:
        return self.network.ok and self.tls.ok and self.authentication.ok

    def to_dict(self) -> dict[str, object]:
        return {
            "reachable": self.reachable(),
            "network": {"ok": self.network.ok, "detail": self.network.detail},
            "tls": {
                "ok": self.tls.ok,
                "detail": self.tls.detail,
                "fingerprint": self.tls_fingerprint,
                "trust": self.tls_trust,
                "certificate_pem": self.tls_certificate_pem,
            },
            "authentication": {"ok": self.authentication.ok, "detail": self.authentication.detail},
            "ibmi_version": self.ibmi_version,
            "qtimzon": self.qtimzon,
            "detected_timezone": self.detected_timezone,
            "timezone_ambiguous": self.timezone_ambiguous,
        }


@dataclass(frozen=True)
class SourceProbeRequest:
    ibmi_host: str
    ibmi_user: str
    secret_value: str
    tls_trust: str = "system"  # "system" | "pinned"
    pinned_fingerprint: str | None = None
    # PEM épinglé par l'opérateur (``SourcesService`` le fournit quand la
    # source a déjà une confiance "pinned" persistée) — jamais utilisé pour
    # valider la chaîne ici (l'empreinte suffit) : transmis pour que
    # l'exécuteur puisse le monter en CA des charges qui parlent à l'IBM i.
    pinned_pem: str | None = None


class SourceProbeProtocol(Protocol):
    """Sonde réelle injectée — jamais de secret journalisé, jamais improvisée ici."""

    def probe(self, request: SourceProbeRequest) -> SourceProbeResult: ...


def build_probe_result(
    *,
    network: ProbeOutcome,
    tls: ProbeOutcome,
    tls_fingerprint: str | None,
    authentication: ProbeOutcome,
    ibmi_version: str | None,
    qtimzon: str | None,
    tls_trust: str | None = None,
    tls_certificate_pem: str | None = None,
) -> SourceProbeResult:
    """Assemble le résultat en dérivant le fuseau IANA depuis ``QTIMZON``.

    Utilitaire pur pour les sondes réelles : elles rendent les mesures
    brutes, cette fonction applique la traduction (échoue « ouvert » côté
    fuseau — ``None``/ambigu — jamais fermée par une valeur inventée).
    """

    detected_timezone = map_qtimzon_to_capture_zone(qtimzon) if qtimzon else None
    return SourceProbeResult(
        network=network,
        tls=tls,
        tls_fingerprint=tls_fingerprint,
        authentication=authentication,
        ibmi_version=ibmi_version,
        qtimzon=qtimzon,
        detected_timezone=detected_timezone,
        timezone_ambiguous=bool(qtimzon) and detected_timezone is None,
        tls_trust=tls_trust,
        tls_certificate_pem=tls_certificate_pem,
    )
