# Fenêtres de preuve pendant la capture continue

## Pourquoi ce lot est nécessaire

qual0909b a repris exactement au checkpoint + 1 et réconcilié 5856 identités
jusqu'à Snowflake. 4929 événements précédaient le départ du Job ; p95 et p99
restent FAIL. Arrêter le worker pour chaque preuve réintroduit du backlog.
Cette observation n'autorise ni l'effacement des événements retardés ni le
changement des seuils. La preuve globale B reste en échec SLO.

Constats de code et revue indépendante du 09/09/2026 :

- verification_window._collect_window exige STOPPED_BUDGET, puis liste un
  préfixe entier et compare un snapshot mutable à deux instants.
- ContinuousCaptureService.run_once publie le raw puis avance le checkpoint ;
  les scans vides avancent également le checkpoint sans raw.
- Le callback report arrive après le checkpoint. Un accumulateur de batches
  dans ce callback perdrait sa preuve en cas de crash avant le callback.
- ConsoleSnapshotBuilder encode l'état du processus avec des compteurs
  cumulatifs. Il ne doit pas être renommé artificiellement en fenêtre fermée.

## Contrat retenu

La capture demeure un processus, les fenêtres deviennent des objets de preuve
distincts. Une fenêtre fermée n'implique jamais que le processus est arrêté.
Le contrôle de lecture unique, le budget DEV et l'arrêt sûr restent imposés.

1. Reçu durable de plage : références raw/manifeste, nombre d'événements,
   empreintes, début scanné et watermark final, prédécesseur et rotation
   explicites. Un scan vide dispose lui aussi d'un reçu. Ordre obligatoire :
   raw/manifeste si présents → reçu → checkpoint. Aucun reçu fondé uniquement
   sur un callback post-checkpoint.
2. Fermeture immuable : règle annoncée avant départ, premier scan source complet
   dont la date enregistrée atteint dix minutes, après confirmation de son CAS.
   La date source et la confirmation du CAS sont distinctes. Le manifeste référence toute la chaîne des
   reçus de l'intervalle ; aucun filtre selon les latences observées. Création
   conditionnelle, contenu identique sur replay, refus d'une collision.
3. Collector explicite : nouveau parcours de fenêtre fermée. Le parcours
   historique STOPPED_BUDGET reste strict. Valider chaîne, bornes, population,
   octets et empreintes des objets exacts ; ne pas simplement accepter RUNNING
   dans l'ancien collecteur de préfixe mutable.
4. Preuve et cockpit : identité de fenêtre séparée du stream durable ; dates
   de fermeture et de réconciliation séparées de l'observation du processus.
   Une fenêtre PASS ne certifie pas la fenêtre suivante, ni la fraîcheur
   actuelle. Le rattrapage reste visible avec ses propres SLO.

## Ordre d'implémentation et tests requis

- Writer/coordinateur : tests d'échec avant reçu, après reçu avant checkpoint,
  réponse PUT perdue, replay identique/conflit, scans vides et rotation. Une
  panne du reçu ne doit pas avancer le checkpoint. Aucune activation distante
  avant intégration du collector.
- Fermeture/récupération : reconstruction après crash à partir des reçus
  durables ; aucune plage supprimée, chevauchée ou sautée ; budgets explicites
  de durée, nombre d'objets et octets. Dépassement = incomplet, jamais sampling.
- Collector/Snowflake : read-set immuable malgré l'avancement du processus,
  contrôle des identités exactes, refus des fenêtres partielles et chaîne
  rompue. Fenêtre vide = latence inconnue, jamais p95=0.
- Cockpit desktop : montrer capture actuelle et dernière livraison vérifiée
  séparément ; tester ancien, collecte en cours, breach, panne et récupération.

## Qualification et autorité

Préannoncer la durée complète de la fenêtre qualifiante (dix minutes), sa
frontière de départ et l'interdiction d'exclusions rétroactives. Tout backlog
qui appartient à ses plages entre dans ses percentiles. Garder la preuve de
rattrapage distincte, même en échec. Ne pas déplacer la frontière pour obtenir
un PASS après mesure. Le soak reste conditionné aux gates prévus.

Ce document n'augmente pas la durée de capture autorisée. Avant un nouveau
run, résoudre le budget total couvrant rattrapage et qualification, le lecteur
unique, les conditions d'arrêt et le retour à zéro. Aucun run supplémentaire
n'est lancé par ce lot de conception. Aucun secret existant ne sera tourné.

Statut : primitive de writer implémentée dans
RawFirstCaptureCoordinator.capture_receipted_window et raccordée localement à
ContinuousCaptureService via `receipted_scans=True`, désactivée par défaut et
non exposée à la CLI ni activée dans le cluster. Reçu immuable avant checkpoint, scans vides et rotations
explicites, replay, refus de fabriquer un reçu après avancement. Le checkpoint
final utilise un CAS sur le prédécesseur exact (condition DynamoDB ; flock pour
le store JSON local), sans remplacer ce prédécesseur par une lecture ultérieure.
Le résultat détaillé sépare le temps de publication raw/reçu du temps du CAS.
L'option est strictement booléenne ; le chemin historique reste inchangé.
Le worker accepte désormais un `proof_window_id` interne, associé à un intent
créé avant départ. Il vérifie la fenêtre avant tout appel source, puis après
chaque poll. Une fermeture en échec empêche le scan suivant ; la reprise
retrouve le même scan éligible à partir du reçu committé. Une fermeture réussie
est mise en cache et la capture peut continuer sans faux état d'arrêt. Ce
raccordement n'est pas encore exposé à la CLI ou activé dans le cluster.

La primitive `recover_and_seal_window` récupère localement une fermeture
existante avant de consulter le checkpoint ; sinon elle reconstruit la chaîne
exacte jusqu'au checkpoint durable et exclut le tail non committé. Elle doit
s'exécuter sous exclusion du writer, avant tout nouveau scan. Les erreurs de
permission ou une fermeture corrompue ne déclenchent aucune reconstruction.
La récupération remonte désormais un index immuable dérivé de chaque position
finale (`scan-index/end-<sha256>.json`), publié après le reçu et avant le CAS.
Cet index contient la clé et l'empreinte du reçu. La borne de 4096 couvre
uniquement la chaîne de la fenêtre récupérée : aucun LIST historique global.
Le seal revalide les empreintes collectées pendant la remontée. Un index
manquant pour un checkpoint committé est une erreur, jamais une invitation à
recréer rétroactivement une preuve. Les primitives précédemment non déployées
ne font l'objet d'aucune migration silencieuse. Ce chemin ajoute un objet et
une publication conditionnelle par scan, à inclure dans les mesures S3.
La primitive ne certifie pas l'heure du premier poll terminé avant un crash.

Le mode receipted du worker date désormais la fin du scan source immédiatement
au retour du runner et publie un reçu v2 avec `scan_completed_at` avant le CAS.
Cette date n'est pas celle de confirmation du checkpoint. Après une panne,
un reçu déjà préparé conserve sa première date : la nouvelle tentative doit
produire exactement la même population et la même plage. Les octets du reçu
et de l'index restent immuables. Aucun ajout rétroactif de date sur un reçu v1.
La fermeture v2 vérifie les dates ordonnées, les bornes, le premier scan
atteignant l'échéance (obligatoirement le dernier de la fenêtre) et l'absence
de futur par rapport à l'horloge du worker. Le lecteur revalide ces invariants.
Le champ `sealed_at` est l'heure de décision enregistrée avant les lectures
de récupération et le PUT, pas une preuve de l'heure de durabilité S3.
Cette dernière et l'autorité de l'horloge restent à vérifier en runtime.
Un poll idle sans scan n'est pas transformé en reçu vide ; l'absence de scan
éligible à l'expiration échoue sans qualification. La sélection/enchaînement
de fenêtres successives et l'adaptation Snowflake/cockpit restent à terminer.

La fermeture/récupération des métadonnées est implémentée localement dans
`proof_windows.py` : intent immuable, chaîne exacte de reçus, empreintes et
égalité avec le checkpoint à la fermeture. La récupération reste indépendante
du checkpoint courant ; après réponse PUT perdue, lire la fermeture existante
avant toute tentative de nouveau seal. La durée de 600–3600 secondes et sa
grâce de fermeture (0–120 secondes, 60 par défaut) sont inscrites au départ.
Une fermeture hors de cet intervalle est refusée. Les lectures de métadonnées
limitent effectivement les octets lus (budget + un octet de détection), avec
fermeture du flux S3 même en cas d'erreur.

Ces contrôles ne prouvent pas encore l'horloge de l'appelant ni le choix du
premier poll après échéance : ces invariants appartiennent au raccordement
worker à venir. Les octets raw et Snowflake ne sont pas réconciliés par cette
primitive ; la latence reste explicitement `unobserved`.

`closed_window_raw.collect_closed_window_raw` valide désormais localement les
octets exacts référencés : relecture avec empreinte du reçu, manifestes et
payloads sous budget cumulé, identité de batch, intervalle, population unique
et périmètre SALE. Aucun LIST du préfixe mutable. Le budget de 256 Mio borne
les octets raw lus, pas la mémoire Python après décodage. Le résultat conserve
les clés relatives et l'empreinte des identités pour le raccordement destination.
L'adaptateur local `verify_closed_window_destination` raccorde désormais cette
population à la réconciliation Snowflake read-only commune, sans snapshot
capture synthétique. Il impose une fermeture temporelle v2, un bucket/préfixe
S3 exact en DEV et une observation non antérieure à la décision. Le résultat
distingue provenance locale/S3 et conserve `process_state=not_observed`.
Une fenêtre vide retourne `destination.state=not_tested`, sans requête ni
latence inventée. La limite destination de 1000 fichiers doit être respectée
par le dimensionnement de chaque fenêtre (le plafond 4096 porte sur les reçus).
Les noms de compteurs hérités `*_after_second` ne prouvent aucun replay : ce
parcours ne charge rien, il observe la population déjà présente.
La commande `scripts/quadringent_window_verify.py --run-id <run> --window-id <window>
--proof-output <nouveau-fichier.json>` raccorde maintenant le store DEV et la
connexion Snowflake existante à cet adaptateur. Profils par défaut : example-corp-dev
et example-corp ; l'alternative OIDC impose le rôle workload dédié. Le profil local
conserve son rôle configuré ; le CLI n'effectue que les SELECT prévus et ne
modifie pas les droits du profil. Codes de sortie : 0 matched, 2 pending ou
not_tested, 3 erreur opérationnelle. Pending ne crée aucune preuve ; not_tested
écrit le résultat explicite. Aucun fichier existant n'est écrasé. Les erreurs
ne publient que leur type. Pas de boucle d'attente implicite ni capture.
Le contrat REST accepte désormais une preuve attachée dans
`window_destination_proof` et expose `window_delivery` séparément. La fenêtre
doit porter un intent v2 avec `stream_id` déclaré à sa création, immuable et
égal au flux courant. Les intents v1 restent lisibles pour les archives mais
ne sont pas projetables en preuve de livraison attachée. La fraîcheur dépend
de la fermeture de fenêtre, pas d'une nouvelle observation Snowflake ; une
preuve locale reste simulation. Aucune modification des compteurs, étapes,
horodatages ou verdicts de capture courante. Une preuve invalide reste explicite.
Le control plane accepte `--window-proof source-id=URI` pour joindre la preuve
sans écrire sur le snapshot de capture. Une URI S3 doit viser exactement
`s3://example-corp-000000000000-int-example-corp-raw/as400/sales/sale/runs/<run>/windows/<window>/destination.json`.
Les identités du contenu doivent correspondre au chemin et au flux. Un fichier
local impose la provenance simulation. Si la preuve est illisible, la capture
reste disponible avec une livraison `unavailable`. La panne de lecture capture
dégrade aussi la fraîcheur de la preuve conservée. Cette jointure est explicite
et fixe : elle ne sélectionne pas encore automatiquement la dernière fenêtre.
Le vérificateur accepte maintenant `--publish-window-proof` pour créer cet
objet séparé après vérification et sauvegarde locale exclusive. Ce flag est
absent par défaut. Une intention v2 liée au flux est obligatoire ; une preuve
locale ne peut pas être publiée. La relecture exacte est obligatoire pour
`publication_state=confirmed`. Un PUT tenté sans confirmation reste `unknown`,
même si l'objet a effectivement été créé ; la preuve locale est conservée.
Les erreurs antérieures restent `not_attempted`. Un contenu existant différent
n'est jamais remplacé. Une nouvelle vérification a une nouvelle date et peut
donc rencontrer une collision : ce n'est pas un mécanisme de rafraîchissement
de fenêtre. L'option `--resume-publication`, utilisée avec le flag publication
et le fichier local original, revalide le raw et Snowflake puis compare tous
les octets canoniques, en conservant uniquement la date d'observation initiale.
Cette date doit être comprise entre fermeture et nouvelle observation ; elle
reste une provenance du fichier, pas une observation historique réattestée.
Toute autre différence interdit la reprise. Le fichier n'est pas réécrit.
`scripts/quadringent_window_supervise.py` sélectionne désormais ce mode lorsqu'un
fichier de preuve existe. Il supervise une seule fenêtre déjà fermée :
pending et publication incertaine sont retentés, erreur définitive arrêtée,
vide conservé comme not_tested. Budget total 30..5400 s, tentative 5..120 s,
pause 1..60 s et plafond de tentatives dérivé de budget/pause ; valeurs par
défaut 300/60/10 s. Le flag enfant --await-window retourne pending si l'objet
closed.json initial est absent, avant connexion Snowflake. Une archive raw
absente après fermeture reste une erreur ; la reprise ne masque pas sa disparition.
Le subprocess est arrêté et attendu après timeout. Une erreur locale après
une tentative incertaine conserve publication_state=unknown. Ce superviseur
ne démarre pas la capture, ne crée pas de fenêtre et ne sélectionne pas la
dernière fenêtre d'un flux ; ces branchements runtime restent à effectuer.
Le worker expose maintenant `--proof-window-id` et `--proof-window-seconds`
(600 par défaut). Sans identifiant, aucun changement de comportement. Avec
identifiant, le bucket/table checkpoint DEV, schéma SALES, table SALE,
journal DEMOJRN et préfixe de run isolé sont exigés ; tout accès hors
hôte/compte CDCUSER DEV est refusé. Le marqueur de réservation
du run est vérifié et le checkpoint doit exister, même pour une fenêtre fermée.
Tout bootstrap explicite
ou tail est refusé. Avant JVM/catalogue, `prepare_window` crée l'intention ou
reprend l'intention existante sans redater et contrôle son éligibilité.
Le service active alors les reçus horodatés et sa fermeture avant/après scan.
Une fenêtre ouverte périmée est refusée, pas recréée. Le mode ne crée qu'une
fenêtre. La chart transmet ces arguments via `pilot.proofWindow.enabled`,
`id` et `seconds` (désactivé par défaut). Elle refuse hors périmètre DEV,
bootstrap autre que checkpoint, durée non entière ou non couverte par le budget
(durée de fenêtre + réserve lecteur arrondie au supérieur + 60 s de fermeture).
Le Deployment doit toujours rester à zéro pendant le pilote. Avec vérification
activée, la chart lance désormais le superviseur de fenêtre avec publication
obligatoire et SLO historiques interdits. Il dispose de capture + shutdown +
180 s de budget, avec 60 s supplémentaires avant la deadline Kubernetes.
Les arguments et la clé de publication du vérificateur historique ne sont
pas utilisés dans ce mode. Le superviseur est embarqué dans l'image verifier.
L'orchestration multi-fenêtres et le rendu cockpit restent à raccorder, sans
activation distante. Il ne faut pas fabriquer un snapshot STOPPED_BUDGET pour
réutiliser le parcours historique : la capture et la fenêtre restent distinctes.

FileObjectStore utilise désormais une création atomique exclusive
et synchronise les entrées de répertoires ; les tests locaux couvrent les
writers concurrents mais ne simulent ni panne électrique ni concurrence S3.
Un temporaire orphelin après crash ne vaut jamais reçu final.
Aucune activation avant traitement des invariants de
récupération complets. Ce lot ne remplace ni les mutations métier C/U/D ni
l'oracle source indépendant.
