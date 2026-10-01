"""Coûts v2 (``GET /v2/costs``, contrat §2.5).

Reprend les invariants déjà en place dans ``costs.py``/
``infrastructure_costs.py`` (v1) — mesuré/estimé/absent, jamais de montant
inventé, devise et base de calcul toujours explicites, fraîcheur datée —
sans réutiliser leurs types directement : ces modules v1 calculent à partir
d'un ``ObservabilityProjection``/``SourceDescriptor`` liés à
``quadringent.site_config`` (un seul site par processus, §"Constat de
départ" du contrat), alors qu'un coût v2 est adressé par ``scope`` +
``id`` opaque (connexion ou table v2), sans notion de site global. Un futur
chantier écrira un adaptateur ``project_costs``/``project_infrastructure_costs``
-> ``CostSnapshot`` pour un déploiement réel ; tant qu'aucun fournisseur
n'est injecté, ``NullCostsProvider`` répond ``absent`` (jamais 0).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

CostStatus = Literal["measured", "estimated", "absent"]
CostScope = Literal["connection", "table"]

KNOWN_SCOPES = frozenset({"connection", "table"})


@dataclass(frozen=True)
class CostSnapshot:
    scope: CostScope
    id: str
    window: str | None
    status: CostStatus
    amount: float | None
    currency: str | None
    basis: str | None
    collected_at: str | None
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "id": self.id,
            "window": self.window,
            "status": self.status,
            "amount": self.amount,
            "currency": self.currency,
            "basis": self.basis,
            "collected_at": self.collected_at,
            "reason": self.reason,
        }


def absent_cost(scope: CostScope, id_: str, *, window: str | None, reason: str | None = None) -> CostSnapshot:
    return CostSnapshot(
        scope=scope,
        id=id_,
        window=window,
        status="absent",
        amount=None,
        currency=None,
        basis=None,
        collected_at=None,
        reason=reason,
    )


class CostsProviderProtocol(Protocol):
    """Contrat minimal d'un fournisseur de coûts — jamais de facturation
    Snowflake/cloud câblée ici."""

    def get(self, scope: CostScope, id_: str, *, window: str | None) -> CostSnapshot: ...


NO_PROVIDER_REASON = "aucun fournisseur de coûts configuré pour ce control plane"


class NullCostsProvider:
    """Fournisseur par défaut : échoue fermé, aucun coût mesuré ni estimé."""

    def get(self, scope: CostScope, id_: str, *, window: str | None) -> CostSnapshot:
        return absent_cost(scope, id_, window=window, reason=NO_PROVIDER_REASON)
