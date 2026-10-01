# Décision — historique et miroir Snowflake

Date : 23 septembre 2026. Statut : acceptée.

## Contexte

Par table source, le produit livre une table d’historique (tous les
changements horodatés) et une table miroir (une ligne par clé, mêmes colonnes
et types que la source). Cible : quelques secondes entre la modification IBM i
et la visibilité dans Snowflake.

## Mesures (compte Snowflake de qualification, 103 événements réels rejoués)

Latences mesurées du dépôt d’un événement au client Snowflake jusqu’à sa
visibilité ; écart d’envoi 5 s.

| Voie | Médiane | 95e centile | Remarque |
|---|---:|---:|---|
| Historique — Snowpipe Streaming (SDK Python, architecture haute performance) | 5,2 s | 7,2 s | Livraison par canal et jeton d’offset |
| Miroir B — MERGE émis par le chargeur après chaque flush | 6,3 s | 8,3 s | Aucun objet planifié, aucun droit supplémentaire |
| Miroir A — stream + tâche déclenchée (intervalle minimal 10 s) | ≈ 4,5 s (incrémental) | ≈ 8,7 s | Exige `EXECUTE TASK` pour le rôle propriétaire |
| Miroir C — table dynamique | — | ≈ 41 s | `TARGET_LAG` minimal d’une minute (limite plateforme) |

Rapprochement : les trois miroirs sont conformes à l’oracle (110 clés, suppressions
effectives, zéro écart). Mesure IBM i → brut durable séparée : médiane 4,5 s,
95e centile 8 s. Bout en bout estimé IBM i → miroir B : de l’ordre de 11 s en
médiane et 15 s au 95e centile ; le cockpit affiche le retard réel mesuré.

## Décision

- Historique : Snowpipe Streaming, un canal nommé de façon stable par flux, reprise
  au dernier jeton d’offset connu.
- Miroir : option B par défaut ; option A proposée en mode expert quand le site
  préfère découpler le chargeur (le provisionnement doit alors accorder
  `EXECUTE TASK`). Pas de table dynamique pour le miroir temps réel.
- Déduplication par `event_id` obligatoire en lecture et dans le MERGE : un
  jeton d’offset rejoué ou un changement de nom de canal réinsère les lignes
  (constaté sur le même canal comme sur un canal neuf).

## Limites

Un seul compte, une seule table, faible volume ; coûts Snowpipe Streaming non
encore visibles au moment de la mesure (vues de consommation différées). La
qualification continue répète la mesure.
