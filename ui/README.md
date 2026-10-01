# Quadringent — instrument opérateur

Cette UI est l’instrument de preuve en lecture seule du control plane Quadringent
pour un flux IBM i → Snowflake. Elle répond d’abord à trois questions : quel est
le verdict prioritaire, quelle preuve le soutient et quelle est la prochaine
action sûre. Une capture active ne devient jamais implicitement une livraison
Snowflake confirmée.

## Démarrage

```bash
npm install
npm run dev
npm test
npm run typecheck
npm run build
```

La suite de tests couvre les helpers de rendu, le routeur, le shell, les écrans,
le contrat strict, le client control plane, le contrôleur SSE et les états
accessibles. Elle ne dépend pas d’un serveur ou d’un port durable.

## Routes produit

Le routeur hash expose uniquement les vues suivantes :

- `#/overview`
- `#/pipelines`
- `#/pipeline/<id>`
- `#/pipeline/<id>/overview`
- `#/pipeline/<id>/live`
- `#/incidents`
- `#/usage`
- `#/setup` (assistant d'onboarding)
- `#/flux/<id>` (alias toléré de `#/pipeline/<id>`)

Seuls les onglets détail `overview` et `live` sont publiés. Integrity, Runs et
Configuration restent absents tant que le backend ne fournit pas leurs preuves.

Les anciens alias techniques restent tolérés en lecture pour la compatibilité,
mais le chemin canonique du cockpit est `#/pipeline/...`.

## Architecture UI

- `src/data/controlPlaneClient.ts` parle au backend same-origin `/v1` via REST
  et SSE.
- `src/data/controlPlaneController.ts` garde l’état de session, la révision, le
  cache et la connexion SSE séparée de l’état des données.
- `src/data/useControlPlane.ts` ne fait qu’adapter le contrôleur à React.
- `src/App.tsx` compose les vues produit à partir de helpers purs testables.
- `src/screens/` porte les quatre surfaces globales, la synthèse pipeline et sa vue Mesures.
- `src/components/AppShell.tsx` fournit la barre produit légère, la navigation
  basse mobile, le contexte de preuve, le skip link et l’état de connexion.
- `src/domain/proofFocus.ts` résout, de façon fail-closed, le verdict, la première
  rupture recevable et la portée de preuve avant tout choix de présentation.
- `src/components/WorkspaceProofline.tsx` et `PipelineTopology.tsx` composent ce
  résultat en un point de preuve asymétrique : amont compact, rupture dominante,
  destination toujours visible.
- `src/components/OperatorEmptyState.tsx` sépare l’absence confirmée de la
  lecture indisponible sans inventer de wizard de configuration.
- `src/productViewModels.ts` contient les copies et view-models réutilisables.

## Système visuel

Le design system prend la forme d’un instrument calme :

- canvas presque blanc, topbar compacte et très peu de surfaces autonomes ;
- Geist variable pour la hiérarchie produit, IBM Plex Mono uniquement pour les
  preuves et horodatages techniques ; les polices sont servies localement ;
- ambre pour les limites de preuve, rouge pour la rupture, bleu uniquement pour
  une mesure live fraîche ; jamais pour la connexion ou une simulation ;
- le point de preuve est l’objet signature partagé par l’accueil, l’inventaire
  et le détail, sans stepper de cinq cartes égales ;
- aucun mouvement décoratif ni animation d’entrée ; les retours d’interaction
  restent brefs et `prefers-reduced-motion` les neutralise ;
- la structure est portée par l’espace, la typographie et quelques surfaces,
  sans quadrillage, séparateurs pleine largeur, glassmorphism ni gradient ;
- cibles tactiles à 44 px ;
- mode `prefers-reduced-motion` sans animation ;
- tokens actifs dans `src/styles/product-tokens.css` et `src/styles/product-shell.css`.

## Parcours opérateur

Le chemin principal est volontairement unique :

1. lire le verdict de la vue d’ensemble ;
2. ouvrir le pipeline prioritaire proposé ;
3. inspecter le point de preuve et ouvrir une étape dans une sheet accessible ;
4. basculer sur « Mesures » pour la valeur, le verdict de tendance canonique,
   la fraîcheur, la série sans interpolation et les compteurs réellement exposés.

Incidents et Usage sont des registres secondaires. Ils n’agrègent pas des
unités incompatibles et distinguent « aucun élément dans le snapshot confirmé »
de « impossible à confirmer ».

## Fixture de démonstration

Le fixture de démonstration vit hors `public`, dans :

`ui/fixtures/console-dev.json`

Le build de production est gardé par `scripts/verify-build.mjs` : si `dist`
contient encore `console-dev.json` ou une trace du chemin, le build échoue.

Le développement ne lit pas directement cette fixture. Démarrer d'abord le
control plane local avec une provenance `simulation` ou `historical`, puis
laisser Vite proxifier `/v1`. Le runbook exact est
`../docs/product/quadringent-local-console.md`.

## Garde de livraison

- `npm test`
- `npm run typecheck`
- `npm run build`
- vérification `dist` sans fixture
- `PYTHONPATH=src python3 scripts/verify_quadringent_vertical.py --ui-dist ui/dist`
  depuis la racine du dépôt

Les chiffres de réussite restent intangibles au build ; ce qui compte, c’est la
qualité des preuves et la stabilité du cockpit rendu.

La validation décrite ici porte sur une verticale locale en lecture seule.
Elle ne constitue pas une certification commerciale globale ; la preuve
exécutée ici reste locale et simulée/historique. Elle ne certifie ni connectivité IBM i,
ni chargement Snowflake, ni déploiement PROD tant que le backend n’observe pas
les checkpoints et la réconciliation attendus.

### Régression clavier desktop du diagnostic

Après démarrage du control plane local selon le runbook ci-dessus et build
de l'UI, exécuter depuis la racine du dépôt :

```sh
QUADRINGENT_BROWSER_URL=http://127.0.0.1:8853/ \
QUADRINGENT_PIPELINE_ID=review \
node ui/scripts/verify-diagnostic-browser.mjs
```

Remplacer `review` par l'identifiant du pipeline simulé effectivement exposé.
Le script nécessite Playwright et son Chromium installés dans l'environnement
de validation. Il n'installe rien. `QUADRINGENT_PLAYWRIGHT_MODULE` accepte une URL
de module ESM si Playwright n'est pas résolvable localement ;
`QUADRINGENT_BROWSER_EXECUTABLE` permet d'utiliser un navigateur Chromium existant.
Seules les URL HTTP loopback sans credentials ni query sont acceptées.

Contrôles à 1440 × 1000 : Tab jusqu'au raccourci, activation Entrée, focus et
visibilité du diagnostic, hash inchangé, aucun débordement horizontal,
navigation vers Mesures, absence du raccourci sans cible, aucune erreur
page/console/réseau pertinente. Ce test ne lance aucune ingestion et ne
prouve pas la fraîcheur des données. Il n'est pas encore intégré à la CI.
