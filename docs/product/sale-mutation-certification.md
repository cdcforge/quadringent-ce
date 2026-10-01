# Vérification bornée des mutations

Ce guide décrit le contrat technique du vérificateur. Les exemples emploient la
table fictive `SALE` ; ils ne constituent aucune preuve sur un système client.
La table et ses colonnes de clé doivent être déclarées dans la configuration du
site et vérifiées sur son catalogue avant toute qualification.

Le contrôle couvre les opérations C/U/D, les images `u_before` et `u_after`,
une valeur `NULL`, une évolution additive de schéma et le replay idempotent.
Il refuse une image non objet, une clé absente, nulle ou non scalaire, une
identité d’événement forgée, un ordre non strict ou plusieurs receivers sans
chaîne explicite. Le rapport ne restitue aucune valeur métier.

Une fenêtre coupée entre les images avant/après reste incomplète. Un ordre
compatible et des nombres équilibrés ne prouvent pas l’appariement
transactionnel. Un PASS valide le raw fourni et le réducteur local ; il ne
remplace pas le rapprochement indépendant IBM i/Snowflake.

## Exécution locale synthétique

Depuis le clone, après `pip install .` dans l’environnement de développement :

```sh
set -a
. examples/local.env
set +a
python scripts/quadringent_sale_mutation_gate.py
```

Sans entrée, le script utilise seulement une fixture. Le code retour `2` et
`CONTRACT_PASS_LIVE_PENDING` sont attendus : un contrat synthétique ne certifie
pas une source IBM i.

Sur une fenêtre JSONL réelle, bornée et autorisée, utiliser la configuration
privée du site et `--input /chemin/fenetre.jsonl`. `--input -` lit l’entrée
standard. Les limites sont 32 MiB et 10 000 événements ; elles bornent le
contrôle et ne qualifient ni le débit ni un soak.

| Code | Sens |
| --- | --- |
| 0 | Toutes les preuves demandées sont présentes dans la fenêtre |
| 1 | Violation de périmètre, clé, ordre ou replay |
| 2 | Preuve incomplète ou contrat synthétique seulement |
| 3 | Fichier, événement ou configuration invalide |

## Reprise et qualification du site

`python scripts/raw_checkpoint_fault_matrix.py` vérifie localement les pannes
avant payload, avant manifeste et avant checkpoint. Le checkpoint ne doit pas
avancer sans raw durable ; le replay doit réutiliser ce raw sans doublon.
Ce contrôle ne prouve pas un redémarrage Kubernetes ni une reprise distante.

La qualification du site exige des créations, mises à jour et suppressions
propriétaires autorisées, des clés complètes, des valeurs nulles préservées,
une évolution de schéma, les mêmes identités dans le raw et la destination,
un replay sans doublon et un checkpoint monotone après panne. Toute preuve
manquante reste explicitement incomplète. Voir [l’installation](install-client.md)
et [les limites de sécurité](../../SECURITY.md).
