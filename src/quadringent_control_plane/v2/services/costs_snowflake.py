"""Coûts Snowflake mesurés en direct — crédits d'un warehouse dédié (chantier
« observabilité v2 », suite).

``costs_projection.py`` réutilise ``costs.py`` v1, qui exige que le
document console publie déjà une mesure de crédits Snowflake fraîche
(preuve « live », fenêtre de 24 h). Quand ce n'est pas le cas (ou pour un
warehouse dédié au cockpit, hors du pipeline de capture), ce module
interroge directement Snowflake via une requête **injectable**
(``SnowflakeCreditsQueryProtocol`` — jamais un client Snowflake réel câblé
ici, comme ``PipelineExecutorProtocol``/``TableDiscoveryClientProtocol``
ailleurs) et applique le même principe que ``costs.py`` : **aucun montant
sans prix par crédit déclaré**. La valeur renvoyée est explicitement
marquée provisoire tant que la facturation Snowflake n'est pas finalisée
(``SnowflakeCreditsSample.provisional`` — la consommation d'un warehouse
n'est définitive côté Snowflake qu'après un délai, voir
``ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY`` dans la documentation
Snowflake) — statut ``estimated`` dans ce cas, ``measured`` sinon.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .costs import CostScope, CostSnapshot, absent_cost

NO_SAMPLE_REASON = "aucun échantillon de crédits Snowflake pour cette fenêtre"
NO_PRICE_REASON = "aucun prix par crédit déclaré pour ce warehouse — jamais de montant sans prix"


@dataclass(frozen=True)
class SnowflakeCreditsSample:
    """Un relevé de consommation, tel que renvoyé par la requête injectée —
    jamais recalculé ici."""

    credits: float
    collected_at: str
    provisional: bool


class SnowflakeCreditsQueryProtocol(Protocol):
    """Contrat minimal d'une requête de crédits Snowflake — jamais de
    connecteur Snowflake réel câblé dans ce module."""

    def query_credits(self, warehouse: str, window: str | None) -> SnowflakeCreditsSample | None: ...


class SnowflakeWarehouseCostsAdapter:
    """``CostsProviderProtocol`` réel adossé à une requête de crédits
    injectée — ``scope`` reste ``connection`` (un warehouse, pas une
    table) ; ``id_`` du contrat n'est pas utilisé pour choisir le
    warehouse (un seul warehouse dédié par instance de cet adaptateur,
    voir ``warehouse`` au constructeur) mais reste porté dans la réponse
    pour respecter la forme du contrat."""

    def __init__(
        self,
        query: SnowflakeCreditsQueryProtocol,
        *,
        warehouse: str,
        price_per_credit: float | None,
        currency: str,
    ) -> None:
        self._query = query
        self._warehouse = warehouse
        self._price_per_credit = price_per_credit
        self._currency = currency

    def get(self, scope: CostScope, id_: str, *, window: str | None) -> CostSnapshot:
        sample = self._query.query_credits(self._warehouse, window)
        basis = f"warehouse:{self._warehouse}"
        if sample is None:
            return absent_cost(scope, id_, window=window, reason=NO_SAMPLE_REASON)
        if self._price_per_credit is None:
            return CostSnapshot(
                scope=scope,
                id=id_,
                window=window,
                status="absent",
                amount=None,
                currency=self._currency,
                basis=f"{basis} credits={sample.credits}",
                collected_at=sample.collected_at,
                reason=NO_PRICE_REASON,
            )
        return CostSnapshot(
            scope=scope,
            id=id_,
            window=window,
            status="estimated" if sample.provisional else "measured",
            amount=sample.credits * self._price_per_credit,
            currency=self._currency,
            basis=basis,
            collected_at=sample.collected_at,
            reason=None,
        )
