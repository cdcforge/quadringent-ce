"""Control plane v2 — chantier 3 (fondation Postgres/FastAPI).

Ce paquet ajoute une surface ``/v2`` à côté du serveur ``/v1`` existant
(``quadringent_control_plane.server``), sans le modifier. Périmètre couvert
ici (voir ``docs/plans/2026-09-23-control-plane-v2-contract.md``, §9.2,
tâches 1, 2, 3, 5 et 6) :

- schéma Postgres versionné (Alembic) et connexion (SQLAlchemy Core) ;
- modèle Source v2 (CRUD read/create/test, secret jamais en clair) ;
- modèle Destination v2 (paire de clés RSA + script SQL Snowflake) ;
- machine à états déclarée du pipeline (``declared_state``) ;
- enveloppe d'action générique (``dry_run``, ``Idempotency-Key``,
  ``before``/``after``/``verify``).

Tout le reste du contrat (découverte de tables, confirmations, jetons
d'agent, utilisateurs, audit v2, SSE étendu, webhooks, coûts v2, migration
v1→v2, proxy v1→v2, MCP, CLI) est hors périmètre de ce chantier.
"""

from __future__ import annotations
