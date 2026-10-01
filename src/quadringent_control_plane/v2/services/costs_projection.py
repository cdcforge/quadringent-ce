"""Adaptateur de coûts réel — réutilise ``costs.py`` v1 tel quel (chantier
« observabilité v2 », suite).

``costs.project_costs`` calcule un coût **warehouse** (un compte Snowflake
entier, pas une table) à partir d'un ``ObservabilityProjection`` (mesure
« crédits Snowflake » publiée par le document console, fraîche, en preuve
live) et d'un ``SiteConfig`` (prix par crédit + devise déclarés). C'est
strictement une portée **connexion** au sens du contrat §2.5
(``scope=connection``) — il n'existe aucun collecteur v1 par table :
``scope=table`` reste donc explicitement absent ici, jamais approximé.

Comme ``observation_projection.py``, un résolveur injecté
(``connection_source_spec``) traduit l'id de connexion v2 vers une
spécification de source v1 ; sans résolution, l'observation reste absente.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from quadringent.site_config import SiteConfig, current as current_site

from ... import repository as v1_repository
from ...costs import project_costs
from ...model import PipelineProjection, SourceDescriptor
from .costs import CostScope, CostSnapshot, absent_cost

NO_MAPPING_REASON = "aucune source de projection v1 déclarée pour cette connexion"
UNREADABLE_REASON = "document de projection v1 introuvable ou illisible"
TABLE_SCOPE_UNSUPPORTED_REASON = (
    "coûts par table non mesurés par ce collecteur — costs.py v1 ne calcule qu'un coût "
    "warehouse (portée connexion), jamais par table"
)
NO_MEASUREMENT_REASON = (
    "aucune fenêtre de facturation warehouse mesurée dans le document de projection "
    "(fraîcheur, preuve live ou prix déclaré insuffisants)"
)
OUT_OF_SITE_REASON = "connexion hors du site configuré (quadringent.site_config)"

ConnectionSourceSpecResolver = Callable[[str], str | None]


def cost_snapshot_from_pipeline(
    pipeline: PipelineProjection,
    descriptor: SourceDescriptor,
    site: SiteConfig,
    *,
    scope: CostScope,
    id_: str,
    window: str | None,
    now: datetime | None = None,
) -> CostSnapshot:
    """Applique ``costs.project_costs`` (v1, inchangé) à une projection déjà
    résolue — fonction pure, testable sans passer par un document JSON complet
    (voir ``tests/test_v2_costs_projection.py``)."""

    result = project_costs(pipeline.observability, descriptor, now or datetime.now(timezone.utc), site=site)
    if result is None:
        return absent_cost(scope, id_, window=window, reason=OUT_OF_SITE_REASON)
    amount = result.get("amount")
    basis = f"warehouse:{result['warehouse']}"
    if amount is None:
        return CostSnapshot(
            scope=scope,
            id=id_,
            window=window,
            status="absent",
            amount=None,
            currency=result.get("currency"),
            basis=basis,
            collected_at=pipeline.observed_at,
            reason=NO_MEASUREMENT_REASON,
        )
    return CostSnapshot(
        scope=scope,
        id=id_,
        window=window,
        status="measured",
        amount=float(amount),
        currency=result["currency"],
        basis=basis,
        collected_at=pipeline.observed_at,
        reason=None,
    )


class CostsV1ProjectionAdapter:
    """``CostsProviderProtocol`` réel adossé à ``costs.py``/``repository.py`` v1."""

    def __init__(
        self,
        connection_source_spec: ConnectionSourceSpecResolver,
        *,
        site_resolver: Callable[[], SiteConfig] | None = None,
    ) -> None:
        self._resolve = connection_source_spec
        self._site_resolver = site_resolver or current_site

    def get(self, scope: CostScope, id_: str, *, window: str | None) -> CostSnapshot:
        if scope != "connection":
            return absent_cost(scope, id_, window=window, reason=TABLE_SCOPE_UNSUPPORTED_REASON)
        spec = self._resolve(id_)
        if spec is None:
            return absent_cost(scope, id_, window=window, reason=NO_MAPPING_REASON)
        try:
            source = v1_repository.parse_source_spec(spec)
        except ValueError:
            return absent_cost(scope, id_, window=window, reason="spécification de source de projection v1 invalide")
        repo = v1_repository.ProjectionRepository([source])
        snapshot = repo.refresh()
        if not snapshot.pipelines:
            return absent_cost(scope, id_, window=window, reason=UNREADABLE_REASON)
        pipeline = snapshot.pipelines[0]
        return cost_snapshot_from_pipeline(
            pipeline, source.descriptor, self._site_resolver(), scope=scope, id_=id_, window=window
        )
