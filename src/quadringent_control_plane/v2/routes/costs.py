"""Route ``GET /v2/costs?scope=connection|table&id=&window=`` (contrat §2.5).

``request.app.state.costs_provider`` est ``None`` par défaut (échec fermé —
même discipline que ``pipeline_executor``/``log_source``) : sans
fournisseur injecté, la réponse est toujours ``status: absent``, jamais un
montant inventé.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth import require_scope
from ..errors import invalid_request
from ..services.costs import KNOWN_SCOPES, NullCostsProvider

router = APIRouter(prefix="/v2/costs", tags=["costs"])


@router.get("")
async def get_costs(
    request: Request,
    scope: str | None = None,
    id: str | None = None,  # noqa: A002 - nom de paramètre imposé par le contrat
    window: str | None = None,
    identity=Depends(require_scope("read")),
) -> dict[str, object]:
    if scope not in KNOWN_SCOPES:
        raise invalid_request("scope doit être « connection » ou « table »")
    if not id:
        raise invalid_request("id est obligatoire")
    provider = getattr(request.app.state, "costs_provider", None) or NullCostsProvider()
    snapshot = provider.get(scope, id, window=window)
    return snapshot.to_dict()
