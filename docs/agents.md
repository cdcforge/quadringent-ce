# Connecter un agent (Claude, Codex, autre client MCP)

Ce document explique comment connecter un agent — Claude Code, Claude
Desktop, Codex ou tout autre client [MCP](https://modelcontextprotocol.io)
— au control plane Quadringent, et ce qu'il peut y faire. Référence
technique complète : `docs/api-v2.md` (sections « Serveur MCP » et
« CLI »). Voir aussi le contrat de conception,
`docs/plans/2026-09-23-control-plane-v2-contract.md` (§4, §9.2 tâches
18-21).

## Deux façons de se connecter

### 1. Streamable HTTP direct — `/mcp`

Le control plane expose un serveur MCP **in-process** sous `/mcp`
(streamable HTTP), à côté de `/v2`. C'est la voie à préférer pour tout
client MCP qui sait parler streamable HTTP nativement.

```json
{
  "mcpServers": {
    "quadringent": {
      "url": "https://control-plane.example.test/mcp",
      "headers": {
        "Authorization": "Bearer qdt_op_..."
      }
    }
  }
}
```

(La syntaxe exacte de configuration dépend du client — Claude Code, Claude
Desktop, Codex... consultez leur documentation pour « serveur MCP distant
en streamable HTTP » ou « HTTP-based MCP server ».)

### 2. Pont stdio — `quadringent mcp --stdio`

Pour un client qui ne lance que des serveurs MCP en stdio (process local),
la CLI `quadringent` fait le pont :

```json
{
  "mcpServers": {
    "quadringent": {
      "command": "quadringent",
      "args": ["mcp", "--stdio"],
      "env": {
        "QUADRINGENT_URL": "https://control-plane.example.test",
        "QUADRINGENT_TOKEN": "qdt_op_..."
      }
    }
  }
}
```

Le pont construit exactement le même serveur (mêmes outils, mêmes
ressources) que le montage in-process — seul le transport change (voir
`docs/api-v2.md#quadringent-mcp---stdio`). Nécessite l'extra `api` :
`pip install -e ".[api]"` (ou l'image Docker qui l'inclut déjà).

## Jetons d'agent : portée et scopes

Un agent s'authentifie avec un **jeton d'agent** (`Authorization: Bearer
qdt_<scope>_<valeur>`), jamais avec un mot de passe utilisateur. Trois
scopes, cumulatifs :

| Scope | Peut |
|---|---|
| `read` | Lister/lire (sources, tables, pipelines, confirmations, audit, coûts) |
| `operate` | + actions non sensibles (pause/resume, choix de clé, rafraîchissement de tables) et création d'actions sensibles (qui restent en attente de confirmation) |
| `admin` | + gestion des jetons d'agent, des utilisateurs, des webhooks |

Un jeton peut aussi porter une **restriction de source**
(`source_restriction`) — l'agent ne peut agir que sur les sources listées,
toute autre renvoie `403 wrong_environment` — et une liste
**d'actions pré-autorisées** (`pre_authorized_actions`) qui lui permet
d'approuver *lui-même* une confirmation qu'il a créée, à condition que
l'action figure explicitement dans cette liste (voir « Confirmations »
ci-dessous — c'est la seule exception au principe « un agent ne peut
jamais approuver sa propre action sensible »).

Créer un jeton (nécessite un jeton `admin` existant, ou la première
configuration en mode développement/loopback — voir
`docs/api-v2.md#identité-tâches-8-9`) :

```bash
quadringent tokens create --name "agent-onboarding" --scope operate \
  --source-restriction src_abc123 \
  --never-expires
```

La valeur en clair n'est affichée **qu'une seule fois**, à la création —
elle n'est ensuite jamais relisible (seul son hash est stocké).

## Ce qu'un agent peut faire

Liste complète des outils : `docs/api-v2.md#outils`. En résumé, un agent
peut :

- Découvrir des sources et leurs tables (`list_sources`, `test_source`,
  `list_tables`, `refresh_tables`), déclarer une stratégie de clé
  (`choose_table_key`).
- Consulter et piloter des pipelines (`list_pipelines`, `get_pipeline`,
  `pause_pipeline`/`resume_pipeline`).
- Mettre en pause/reprendre au niveau source, destination, ou toute
  l'organisation (`pause_source`/`resume_source`,
  `pause_destination`/`resume_destination`, `pause_all`/`resume_all`).
- Consulter les coûts (`get_costs` — renvoie `{"status": "absent"}` dans
  ce chantier, aucun service de coûts `/v2` n'est encore câblé) et le
  journal d'audit (`get_audit`).

Toujours essayer `dry_run=true` d'abord sur une action d'écriture : la
réponse contient le plan (`would_transition`, `would_apply`...) sans
aucun effet de bord persisté.

## Confirmations : ce qu'un agent ne peut jamais faire seul

`restart_initial_copy`, `replay_journal_range`, `remove_table`,
`pause_all` et `resume_all` sont des **actions sensibles**. Un agent qui
les appelle sans confirmation valide obtient :

```json
{
  "status": "pending_confirmation",
  "confirmation_id": "conf_xyz789",
  "approve_url": "/v2/confirmations/conf_xyz789/approve",
  "reason": "confirmation requise (id=conf_xyz789) — voir /v2/confirmations/conf_xyz789"
}
```

L'action **n'est pas exécutée**. Pour qu'elle le soit :

1. Un humain approuve — via l'interface web (cockpit), la CLI
   (`quadringent confirmations approve conf_xyz789`), ou un lien signé à
   usage unique envoyé par email/webhook.
2. L'agent rejoue l'appel avec `confirmation_token: "conf_xyz789"` (ou,
   côté CLI, `--confirmation-token conf_xyz789`) — l'action s'exécute
   alors, et la confirmation est consommée (non rejouable).

Seule exception : un jeton d'agent dont `pre_authorized_actions` déclare
explicitement l'`action_ref` concerné (ex. `"pipeline.pause_pipeline"`)
peut approuver sa propre confirmation — décision prise à la création du
jeton par un administrateur, jamais par l'agent lui-même.

## Audit : tout appel d'agent est tracé

Chaque appel d'outil MCP (et chaque commande CLI) produit une ligne dans
`audit_records` avec :

- `actor_kind: "agent"` (par opposition à `"human"` pour une action
  humaine — cockpit, CLI avec session utilisateur).
- `actor_id`/`actor_display` : l'identifiant/le nom du jeton d'agent.
- `mcp_client` : les métadonnées `clientInfo` du client MCP (nom/version —
  ex. `"claude-code/1.2.3"`), quand l'appel vient de `/mcp` ; `null` pour
  un appel REST/CLI direct.
- `dry_run`, `status` (`succeeded`/`failed`/`pending_confirmation`),
  `before`/`after` (l'état avant/après l'action, si pertinent).

Interroger l'audit (humain ou agent, scope `admin`) :

```bash
quadringent audit tail --actor-kind agent --limit 50
```

ou, côté MCP, l'outil `get_audit`. Voir `docs/api-v2.md#audit-tâche-11`
pour le schéma complet et les filtres disponibles.

## Exemples

### Onboarding minimal (Claude Code, via `/mcp`)

1. `list_sources` → repérer la source à onboarder.
2. `test_source(source_id)` → vérifier qu'elle déchiffre correctement.
3. `list_tables(source_id)` → voir les tables découvertes et leur
   `readiness` (`ready`/`not_journaled`/`images_incomplete`/`no_key`).
4. `refresh_tables(source_id)` si le catalogue semble périmé.
5. `choose_table_key(table_id, key_strategy="unique_index", key_columns=["ID"])`
   pour chaque table à répliquer.
6. `list_pipelines()` / `get_pipeline(pipeline_id)` pour suivre l'état
   déclaré (la création de pipeline elle-même reste hors périmètre de ce
   chantier — voir `docs/api-v2.md#machine-à-états-déclarée-du-pipeline`).

### Retrait d'une table (action sensible)

```
agent: remove_table(pipeline_id="pipe_abc123")
  -> {"status": "pending_confirmation", "confirmation_id": "conf_xyz789", ...}

humain (cockpit ou CLI): quadringent confirmations approve conf_xyz789

agent: remove_table(pipeline_id="pipe_abc123", confirmation_token="conf_xyz789")
  -> {"before": {...}, "after": {"declared_state": "stopped"}, ...}
```

Un test bout-en-bout scripté de ce scénario complet (agent → confirmation
→ approbation humaine → agent rejoue) existe dans
`tests/test_v2_mcp_e2e_onboarding.py` et vérifie que l'audit distingue
bien les deux acteurs.

## Jeton d'agent de secours (`quadringent agent-token`)

Le chemin normal : un admin crée les jetons d'agent depuis l'UI ou
`POST /v2/agent-tokens`. Pour l'automatisation d'une installation (reprise,
agent de qualification, perte du compte admin), la CLI émet aussi un jeton
**depuis le Pod** :

```bash
quadringent agent-token --name <release> --namespace <ns> --label <nom> \
  --scope operate --days 7 --write-config ~/.config/quadringent/agent.json
```

- Racine de confiance : le droit Kubernetes `pods/exec` sur le namespace.
  Qui l'a lit déjà la base, la clé de chiffrement et le pepper des jetons :
  il est de fait administrateur de Quadringent. **Réservez `pods/exec` sur ce
  namespace aux administrateurs de l'installation.**
- Garde-fous : durée de 1 à 90 jours (30 par défaut), portée explicite
  (`operate` par défaut), jeton jamais affiché (fichier `0600` uniquement),
  entrée `agent_token.create` dans le journal d'audit (acteur
  `kubectl-exec`), révocable comme tout jeton.
