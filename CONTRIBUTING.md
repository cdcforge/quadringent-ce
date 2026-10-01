# Contribuer

Décrire le comportement attendu et sa preuve de réussite. Pour un défaut,
fournir un scénario synthétique sans secret ni donnée client. Les contributions
sont soumises sous Apache-2.0, comme le reste du projet. Chaque contributeur
conserve ses droits d’auteur ; aucun CLA séparé ni transfert de droits n’est
demandé. Ne proposer que du code que l’on a le droit de distribuer sous cette
licence et préserver les avis des composants tiers.

## Vérifications

Python 3.12+, Node 24 ou 26, Java 21, Maven 3.9+, Helm 4.1.4, Git,
Gitleaks 8.30.1.
Les tests ne contactent pas IBM i, AWS ou Snowflake. Docker peut fournir le JDK.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev,api]'
npm ci --prefix ui
python -m pytest -q
npm test --prefix ui
npm run typecheck --prefix ui
npm run build --prefix ui
python -m ruff check src scripts research tests
python -m mypy
python scripts/check_publication.py
python scripts/sync_version.py --check
sh scripts/test_java_all.sh
```

**pytest est le seul lanceur Python**, y compris pour les classes unittest.
`unittest discover` omet des garanties. Comparer les collectes avec
`python -m pytest --collect-only -q` ; la CI conserve la sienne en artefact.
Les sous-tests sont indiqués séparément des éléments collectés.

`python scripts/local_acceptance.py --timeout-seconds 300` regroupe aussi les
contrôles historiques complémentaires. Ses gates runtime non vérifiés ne sont
pas une qualification de site.

## Principes

Écrire d’abord un test pour tout changement de comportement. Conserver mesure,
provenance, fraîcheur, TLS et moindre pouvoir. Adapter un texte attendu n’autorise
pas à supprimer une garantie moteur. Français pour docs, commentaires et textes ;
identifiants de code en anglais. Ruff vérifie progressivement les noms non
résolus ; mypy couvre les modules déclarés dans pyproject.toml, pas tout le dépôt.

L’UI se valide à 1440 px, au clavier et à la souris : états vide, ancien, simulé,
confirmations et capacités. Les scripts navigateur acceptent
`QUADRINGENT_PLAYWRIGHT_MODULE` pour une installation locale de Playwright.

Une PR décrit le problème, le résultat, les commandes exécutées et leurs limites.
Préserver les changements sans rapport. Ne jamais ajouter values réelles, tokens,
dumps, certificats de site ou logs de production. Les failles se signalent en
privé selon [SECURITY.md](SECURITY.md).
