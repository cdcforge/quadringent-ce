# Coût de la CI

La CI conserve ses sept jobs et leurs gates : suite Python, contrats Java,
chart, trois images réellement démarrées et gate produit. Les caches remplacent
les téléchargements ou étapes de build identiques ; les installations,
compilations nécessaires et tests restent exécutés.

## Réutilisation et permissions

- La release et la publication du site vérifient la dernière CI canonique du
  SHA exact avec `scripts/check_release_ci.py`. Elles ne reconstruisent plus
  les trois images CI et ne répètent plus la suite Python. Les sept jobs de
  `main` doivent tous avoir réussi ; un run plus récent en échec ou actif
  bloque la publication. Les scans propres à la release et au site restent
  obligatoires. Aucune CI payante n'est lancée automatiquement si la preuve manque.
- La concurrence utilise le nom du workflow et la ref Git. Un nouveau run annule
  seulement le travail obsolète du même groupe ; les PR distinctes, branches et
  workflows restent séparés. Le préfixe `ci-` évite une collision avec le groupe
  d'un workflow appelant.
- pip réutilise son cache de téléchargements, indexé par `pyproject.toml` et
  `requirements.txt`. L'installation du paquet courant reste obligatoire.
- Le job `java` réutilise les dépendances Maven indexées par `java/pom.xml`.
  Le job `tests` conserve le JDK sans cache Maven : ses tests Python ne créent
  pas le répertoire `~/.m2` à sauvegarder. Les classes du checkout courant sont
  toujours construites ; `java/target` n'est pas mis en cache.
- Les trois builds utilisent Buildx et le backend GitHub Actions v2, avec un
  scope par image/architecture. `context: .` conserve le checkout comme source ;
  les bases épinglées et les instructions `COPY` participent à l'invalidation.
  Les images sont chargées dans Docker, jamais publiées par la CI.
- `mode=min` limite l'export aux couches finales. Les stages intermédiaires Java
  et UI ne sont pas tous conservés : le gain dépend du changement et du cache.
  Les PR restaurent les caches accessibles sans exporter de cache image propre
  à leur ref. Import et export sont bornés à deux minutes ; un échec d'export
  cache ne masque aucun échec de build ou de smoke test.
- Le token garde uniquement `contents: read`, y compris sur les forks. Aucune
  permission OIDC ou écriture de packages n'est nécessaire. Le backend reçoit
  son authentification runtime de l'action Docker officielle ; aucun token
  personnalisé n'est transmis. Les build records et résumés Docker automatiques
  sont désactivés : aucun nouvel artefact non scanné n'est publié.
- Le rapport de collecte pytest, publié uniquement après son scan de secrets,
  est conservé un jour. Il sert au diagnostic du run ; les contrôles de release
  lisent les statuts CI du commit exact et ne dépendent pas de cet artefact.

Voir les documentations officielles [concurrence GitHub](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency),
[cache pip](https://github.com/actions/setup-python/blob/main/docs/advanced-usage.md#caching-packages),
[cache Maven](https://github.com/actions/setup-java/blob/v4/docs/advanced-usage.md#caching-packages-dependencies)
et [backend Docker GHA](https://docs.docker.com/build/cache/backends/gha/).

## Plafond de stockage : source, usage et date de vérification

Contrôle API effectué le 30 septembre 2026 : limite effective
`max_cache_size_gb=10` ; usage `114615330` octets (109,3 MiB), **4 caches**.
Aucun réglage de quota n'a été modifié. La limite et l'usage sont deux lectures
distinctes ; ces valeurs évolueront avec les prochains runs.

La variable de référence du plafond est **`max_cache_size_gb`**, obtenue par
`GET /repos/OWNER/REPO/actions/cache/storage-limit` et vérifiée à **10** le
**30/09/2026**. L'usage provient de `GET /repos/OWNER/REPO/actions/cache/usage`
(`active_caches_size_in_bytes`, `active_caches_count`) : il ne donne pas le quota.
Relire le plafond avant le premier run sur un nouveau dépôt ou propriétaire,
après un changement des réglages Actions/cache ou de budget, et avant une
nouvelle campagne si le dernier relevé n'est plus à jour. Aucun identifiant
client ni token n'est nécessaire dans ce document ; utiliser l'accès existant.


Conserver le plafond du repository à **10 GB ou moins**. Le workflow ne modifie
aucune limite ou politique de facturation ; `mode=min` ne constitue pas un quota.
Le défaut GitHub est 10 GB, avec éviction des caches anciens. Une extension
au-delà peut être facturée : ne pas activer de capacité supplémentaire.
[Limites et éviction GitHub](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#usage-limits-and-eviction-policy).

Lire séparément la limite configurée et l'utilisation, avec un accès existant :

```sh
gh api -H 'X-GitHub-Api-Version: 2026-03-10' \
  repos/OWNER/REPO/actions/cache/storage-limit --jq .max_cache_size_gb
gh api repos/OWNER/REPO/actions/cache/usage \
  --jq '{bytes: .active_caches_size_in_bytes, count: .active_caches_count}'
```

Ces commandes GET n'augmentent aucun droit. Si la limite n'est pas accessible,
la lire dans Settings → Actions → General → Cache size eviction limit ; ne pas
déduire le plafond d'une faible utilisation.
[API officielle de lecture](https://docs.github.com/en/rest/actions/cache#get-github-actions-cache-storage-limit-for-a-repository).

## Durées observées et coût indicatif

Lectures GET des jobs le 30 septembre 2026. Le run
`main 36722990396` (mesure historique)
est une baseline complète réussie ; le run
`PR 36733461982` (mesure historique)
a échoué pendant la suite Python et n'a pas exécuté ses étapes suivantes.
Les colonnes montrent la durée entre `started_at` et `completed_at`, hors file
d'attente, puis l'arrondi supérieur par job utilisé pour l'estimation.

| Job | main réussi (min réelles → arrondies) | PR échoué (min réelles → arrondies) | Timeout (min) |
|---|---:|---:|---:|
| tests | 6,267 → 7 | 4,950 → 5 | 20 |
| image-runtime | 1,317 → 2 | 1,350 → 2 | 15 |
| verifier-runtime | 0,617 → 1 | 0,700 → 1 | 15 |
| cockpit-runtime | 1,267 → 2 | 1,717 → 2 | 15 |
| java | 0,550 → 1 | 0,567 → 1 | 10 |
| chart | 0,150 → 1 | 0,150 → 1 | 5 |
| quadringent-product-gate | 0,483 → 1 | 0,467 → 1 | 10 |
| Somme | **10,650 → 15** | **9,900 → 13** | **90** |

Au tarif indicatif Linux x64 2 cœurs de **0,006 USD/min**, cela donne
**0,090 USD** pour main et **0,078 USD** pour la PR. Ce sont des estimations
à partir des timestamps et de l'arrondi par job, **pas une facture** : SKU réel,
quotas inclus, gratuité éventuelle et autres frais restent à rapprocher de la
facturation. Un total de consommation d'organisation ne représente pas le coût
de l'un de ces runs. [Tarif officiel et règle d'arrondi](https://docs.github.com/en/billing/reference/actions-runner-pricing).

Les timeouts sont supérieurs aux durées historiques : le job le plus long a
pris 6 min 16 s, aucun n'approche sa borne. Ils bornent les hangs futurs sans
changer les gates. Leur somme de 90 minutes correspond à un plafond indicatif
runner de 0,540 USD pour un run où tous les jobs atteindraient leur timeout,
hors autres frais et écarts de comptabilisation ; ce n'est pas une prévision.

La release possède aussi des bornes par job : validation 5 minutes, images
40 minutes, chart 10 minutes et assemblage 10 minutes. Elles limitent un
blocage ; elles ne garantissent pas un plafond de facture. Les jobs parallèles
s'additionnent, et une annulation peut prendre du temps. Pour une qualification
privée à budget limité, suivre également le cumul arrondi des minutes de tous
les jobs et garder une marge pour l'annulation et le stockage.

Sur main, l'installation Python a pris 20 s et les builds capture/cockpit/verifier
69/59/28 s. La suite Python a pris 249 s : les caches ne suppriment pas cette
suite. Aucun gain en pourcentage ou USD n'est revendiqué avant observation d'un
run avec ces nouveaux caches. Un cache froid ajoute un export et peut augmenter
le temps ; la faible durée du build verifier rend ce risque concret.

Après un prochain run déjà autorisé, comparer durées par job, cache hits,
import/export et minutes arrondies aux 15 minutes de la baseline complète.
Le temps mural ne représente pas la somme des jobs parallèles. L'annulation
n'économise que le travail restant d'un run devenu obsolète et ne rembourse pas
les minutes déjà consommées.

### Première observation avec les caches

Le 30 septembre 2026, la lecture GET des jobs de la
`PR 36743453173` (mesure historique)
confirme **sept succès**. Durées en secondes (minutes arrondies) : `tests`
654 (11), capture 113 (2), verifier 48 (1), cockpit 121 (3), Java 37 (1),
chart 9 (1), gate produit 32 (1). Somme : **1 014 s → 20 minutes**, soit
**0,120 USD indicatif** au tarif ci-dessus, contre 15 minutes / 0,090 USD
pour la baseline. Aucun gain de cache ni coût facturé n'est déduit de ce relevé.

La suite Python a passé 3 522 tests en 515,93 s selon sa sortie ; les timestamps
API de son step couvrent 520 s. L'installation Python couvre 28 s. La cause
du ralentissement de la suite reste inconnue. Le step final `setup-java` a
signalé un cache Maven sans chemin existant : ce cache est retiré du seul job
`tests`, tout en conservant le JDK et le cache Maven du job `java`.

Après sa fin, la lecture GET unique des sept jobs du
`main 36744902680` (mesure historique),
commit `43e1e97`, confirme sept succès. Durées en secondes (minutes arrondies) :
`tests` 359 (6), capture 134 (3), verifier 83 (2), cockpit 141 (3), Java 41 (1),
chart 11 (1), gate produit 38 (1). Le step de suite Python couvre 232 s et
l'installation Python 17 s.

| Run réussi | Somme des jobs (s) | Minutes arrondies | USD indicatifs |
|---|---:|---:|---:|
| Baseline main 36722990396 | 639 | 15 | 0,090 |
| PR 36743453173 | 1 014 | 20 | 0,120 |
| Main 36744902680 | 807 | 17 | 0,102 |

Le nouveau main consomme trois minutes arrondies de moins que la PR, mais deux
de plus que la baseline. Sa suite Python est plus courte ; les trois jobs image
sont plus longs que sur la baseline. Ces variations ne prouvent pas un gain
causé par les caches. Les deux nouveaux runs précèdent la suppression du cache
Maven inutilisé dans `tests` ; son effet n'est pas encore mesuré.
