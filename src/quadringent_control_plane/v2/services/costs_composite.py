"""Compose plusieurs fournisseurs de coûts en un seul, par ordre de
préférence (chantier « observabilité v2 », suite).

Essaie chaque fournisseur dans l'ordre et retient le premier résultat dont
le statut n'est pas ``absent`` ; si tous sont absents, retourne celui du
dernier essayé (sa raison est la plus spécifique disponible). Typiquement :
``CostsV1ProjectionAdapter`` (document déjà publié, préféré s'il est
disponible) puis ``SnowflakeWarehouseCostsAdapter`` (requête Snowflake en
direct, coût de connexion réseau à chaque appel — en repli seulement)."""

from __future__ import annotations

from .costs import CostScope, CostSnapshot, CostsProviderProtocol


class FallbackCostsProvider:
    def __init__(self, *providers: CostsProviderProtocol) -> None:
        if not providers:
            raise ValueError("au moins un fournisseur de coûts est requis")
        self._providers = providers

    def get(self, scope: CostScope, id_: str, *, window: str | None) -> CostSnapshot:
        last: CostSnapshot | None = None
        for provider in self._providers:
            snapshot = provider.get(scope, id_, window=window)
            if snapshot.status != "absent":
                return snapshot
            last = snapshot
        assert last is not None  # au moins un fournisseur, donc au moins un essai
        return last
