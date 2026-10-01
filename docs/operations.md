# Exploiter et reprendre

Lire `run.state`, les erreurs source, le checkpoint et les preuves de continuité
avant toute action. `phase` décrit l’avancement des tables ; elle ne prouve pas
qu’un lecteur soit actif. Une sonde fraîche doit confirmer que la cause de l’arrêt
est résolue, sans lancer des sign-on répétés pour vérifier un compte bloqué.

Les actions offertes sont `prepare`, `start`, `pause`, `resume`, `refresh`.
Le service publie leurs capacités ; l’interface ne crée aucun bouton pour une
capacité indisponible. Les actions mutantes demandent une confirmation explicite.
Le reçu sépare intention enregistrée, exécution et effet observé par relecture.

## Lecteur garé et Job terminé

Une reprise de lecteur garé requiert checkpoint valide, continuité prouvée,
sonde catalogue de moins de 900 s (jamais future), préparation durable cohérente
et relecture réussie prouvant l’absence de l’ancien Job. Le nom de Job reste
dérivé de l’intention ; deux demandes concurrentes ne peuvent pas créer deux
lecteurs sous des noms différents.

Le modèle de lecteur impose un TTL après fin (60 s par défaut si le modèle
n’en fournit pas). L’exemple utilise 60 s. Le contrôleur Kubernetes TTL retire
le Job **après** sa fin ; le control plane ne reçoit aucun droit de suppression.
Tant que le Job est présent, actif ou terminé, la reprise garée est indisponible.
Un Job Complete ou Failed ne donne jamais un reçu RUNNING. Le TTL ne touche
ni brut S3, ni checkpoint, ni audit. Les logs du pod doivent être collectés avant
nettoyage si leur conservation est requise.

Après nettoyage, l’action recrée le même nom au checkpoint durable. Le reçu
`reader_relaunched` prouve un Job relu non terminal ; il ne prouve pas encore
un événement arrivé dans Snowflake. Attendre les nouvelles preuves de livraison.
Si le contrôleur TTL est absent, si le Job historique n’a pas de TTL ou si la
continuité est perdue, la reprise reste bloquée pour intervention autorisée.
Aucune boucle de relance automatique ni bouton générique de redémarrage de pod.

## Fraîcheur du journal et sommeil adaptatif

Le catalogue complet des receivers (`QSYS2.JOURNAL_RECEIVER_INFO`, tous les
receivers récents) coûte plusieurs secondes sur un IBM i partagé ; il est donc
réutilisé jusqu'à `AS400_RECEIVER_CATALOG_CACHE_SECONDS` / `AS400_RECEIVER_CATALOG_CACHE_POLLS`
(60 s / 30 polls par défaut). Sans autre mécanisme, une entrée écrite juste
après un rafraîchissement pouvait rester invisible jusqu'à une minute.

**Sonde de tail** (`AS400_TAIL_PROBE`, activée par défaut) : à chaque poll où
le catalogue est encore en cache, le worker Java répond à une commande `tail`
bornée à une seule ligne SQL filtrée sur `STATUS = 'ATTACHED'` — bien moins
coûteuse qu'un catalogue complet. Si la sonde voit le même receiver ATTACHED
avec un `LAST_SEQUENCE_NUMBER` plus grand, le cache est mis à jour en place
(jamais en arrière) : la nouvelle entrée devient visible au poll suivant sans
catalogue complet. Si la sonde voit un receiver ATTACHED différent (rotation),
le cache est invalidé et le prochain poll relit le catalogue complet. Un échec
de sonde (timeout, exception, aucun receiver ATTACHED) laisse le cache
inchangé — comportement historique inchangé. `AS400_TAIL_PROBE=false` revient
à l'ancien comportement (catalogue complet uniquement, sur expiration du
cache).

**Sommeil oisif adaptatif** : au repos (aucune activité), l'attente entre deux
polls repart à `AS400_MIN_POLL_SECONDS` (1 s par défaut) après toute publication
ou rattrapage, puis double à chaque poll oisif consécutif jusqu'au plafond
`AS400_POLL_SECONDS`. Un flux calme ne paie donc le plafond qu'après plusieurs
polls sans activité, et retrouve une latence de l'ordre de la seconde dès
qu'une écriture reprend.

À mesurer en direct sur l'IBM i cible (hors périmètre de ce développement
hors ligne) : coût réel de la sonde `tail` comparé au catalogue complet, et
latence bout en bout (écriture IBM i → publication brute) en p50/p95 avec la
sonde et le sommeil adaptatif actifs.

## Audit

`actions.jsonl` vit sur le volume d’état flotte : intention, identifiant de requête,
action, liaison, auteur, horodatage, reçu et effet observé. Permissions fichier
0600, synchronisation disque avant exécution, refus des liens symboliques.
Sauvegarder ce volume ; le journal local n’est pas un registre inviolable.
La rotation et la rétention sont à définir par le site ; surveiller l’espace disque.

Avec proxy authentifié, l’auteur est son identité vérifiée. Sans authentification,
la seule attribution possible est `local-port-forward`, pas une personne humaine.
`audit_unavailable` signifie aucune action exécutée. `audit_result_unavailable`
signifie que l’action a pu se produire mais que son résultat n’a pas été écrit :
relire l’état avant toute nouvelle demande. Les capacités sont alors masquées
jusqu’à remise en état et redémarrage contrôlé.
