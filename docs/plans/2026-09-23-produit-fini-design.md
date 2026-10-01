# Quadringent produit fini — design

Date : 23 septembre 2026. Statut : validé en séance de conception, avant plan
d’implémentation. Aucun délai n’est engagé par ce document.

## Objectif

Un utilisateur installe Quadringent en une commande sur AWS ou GCP, active son
compte, relie son IBM i à son Snowflake en trois écrans, choisit ses tables et
voit ses données arriver en quelques secondes, avec un contrôle complet et une
lecture évidente de l’état, des métriques et des coûts. Tout ce qu’un humain
peut faire ou voir est pilotable par un agent.

## Décisions

| Sujet | Décision |
|---|---|
| Déploiement | Deux modes, un seul moteur : chart Helm experte ; mode par défaut clé en main |
| Cibles du mode par défaut | VM (k3s embarqué) et cluster existant (EKS, GKE) |
| Clouds | AWS et GCP, qualifiés tous les deux |
| Exécution | Kubernetes partout ; le control plane pilote Deployments et Jobs |
| Destination | Par table : miroir à jour par clé + historique des changements |
| Chargement | Snowpipe Streaming ; brut durable conservé pour rejeu et preuve |
| Latence cible | 5 à 10 s pour l’historique ; miroir à prouver par un spike |
| Accès | Admin + lecteurs, SSO OIDC optionnel, audit par personne |
| Journalisation IBM i | Détection, explication et commandes CL prêtes ; compte en lecture seule |
| Objets Snowflake | Script SQL généré + paire de clés ; aucun secret admin transmis |
| Agents | API unique, MCP intégré, CLI JSON, jetons à portée, confirmation humaine des actions sensibles |
| Style | IBM Plex sobre, listes en bandes de papier listing, raccourcis F discrets |

## 1. Architecture et déploiement

Un artefact d’exécution : la chart `quadringent` et des images multi-architecture
signées publiées dans un registre public.

- **Control plane** : API `/v2`, UI, serveur MCP, flux d’événements. État dans
  un Postgres embarqué sur volume (connexions, tables, intentions, audit,
  secrets chiffrés).
- **Lecteurs** : un Deployment par connexion IBM i ; lecture du journal, brut
  durable, checkpoint, puis streaming vers Snowflake. Copies initiales en Jobs.
- **Collecteur** de métriques et coûts intégré au control plane.
- **Abstraction cloud** du brut et de l’état : S3 + DynamoDB, ou GCS seul.

Mode Helm expert : `values.schema.json` complet (images, ressources, stockage,
identités IRSA et Workload Identity, réseau, TLS, ingress, SSO, secrets externes,
sélecteurs de nœuds, NetworkPolicies) ; tout a une valeur par défaut raisonnable.

Mode par défaut : `quadringent install --cloud aws|gcp --target vm|cluster`
(CLI + modules Terraform versionnés). Crée stockage, état, identité à portée
minimale, VM k3s si demandé, installe la chart, affiche l’URL et le lien
d’activation admin. Aucun digest, catalogue, sidecar, modèle de Job ou
notification à fournir. La joignabilité IBM i (TLS 9471/9476/9475) est testée.

## 2. Assistant et démarrage automatique

Activation admin, puis trois écrans avec un bouton principal et une validation
en direct :

1. **Source IBM i** : hôte, compte, mot de passe. « Tester » affiche réseau,
   certificat (confiance explicite avec empreinte si CA privée), authentification,
   version et fuseau relevé depuis `QTIMZON`. Secret chiffré par le control plane.
2. **Snowflake** : identifiant de compte ; paire de clés et script SQL générés
   (rôle dédié, utilisateur de service, warehouse XS, base) ; « Vérifier ».
3. **Tables** : découverte depuis le catalogue, recherche, lignes et taille
   estimées, état de journalisation (prête, non journalisée, images incomplètes)
   avec commandes CL et « Revérifier ». Journal déduit de la table. Sans clé
   primaire : proposition d’index unique ou de RRN, conséquence expliquée.
   « Démarrer ».

Démarrage automatique par table, parallèle et plafonné : position du journal,
copie initiale cohérente, bascule exacte sur le journal (un bootstrap explicite
n’est jamais reculé), création du miroir et de l’historique. Arrivée directe sur
la vue « En direct ». Réglages avancés repliés.

## 3. Pilotage et observabilité

Trois niveaux : accueil (une ligne par connexion : état en un mot, retard,
lignes/min, coût du jour, un bandeau d’attention) ; connexion (tables en bandes :
état, retard, débit, lignes source et Snowflake, dernière arrivée ; tri, filtre) ;
table (Métriques, Dernières lignes, Journaux, Coûts, Preuves).

Contrôles au même endroit à chaque niveau : pause et reprise d’une table, de
toutes, d’une connexion, d’une destination ; relancer la copie ; rejouer une
plage ; retirer une table. Effet annoncé, confirmation pour le coûteux ou
destructif, vérification par relecture. Raccourcis F9, F12, F5, F3.

Flux d’événements sans rechargement ; graphes retard et débit 1 h, 24 h au clic.
Journaux filtrables et corrélés aux incidents, sans données de ligne ni secret.
Coûts mesurés (crédits Snowflake, stockage objet du préfixe, CPU et mémoire des
pods), estimés (projection mensuelle) ou absents, avec leur fraîcheur ; prix
déclarés ou tarifs publics datés. Alertes cockpit et webhooks.

## 4. Agent first

API `/v2` décrite en OpenAPI, consommée par l’UI, la CLI et le MCP : rien
n’existe hors de l’API. MCP intégré (outils typés alignés sur l’API, ressources
d’état et de documentation), CLI à sortie JSON, SSE et webhooks signés.

Chaque écriture accepte `dry_run` (plan, coût estimé, risques) et une clé
d’idempotence ; la réponse donne l’état avant/après et la vérification. Erreurs
à code stable avec action suivante. Jetons d’agent à portée (lecture, pilotage,
admin, restreints par connexion) ; confirmation humaine pour suppression,
recopie complète, changement de destination ou coût au-delà d’un seuil, levable
par jeton. Audit distinguant humain et agent. `llms.txt` et descriptions d’outils
autonomes.

## 5. Latence et fiabilité

Chemin : journal → lecteur (poll adaptatif 1 à 5 s) → brut durable → checkpoint
→ Snowpipe Streaming (historique) → miroir.

Corrections : lecture de la dernière entrée d’un receiver attaché (règle de
queue vivante à justifier puis remplacer) ; respect du bootstrap explicite ;
clarification du scan d’un receiver attaché après rotation ; cache du catalogue
invalidé sur changement de receiver ; budget et délai du lecteur cohérents.

Spike initial : maintenance du miroir en 5 à 10 s (tâches déclenchées, MERGE
par le lecteur sur warehouse auto-suspendu, autre). Le cockpit affiche le retard
réel de l’historique et du miroir. Conservés : un lecteur par flux sous bail,
reprise au checkpoint durable, rejeu idempotent dédupliqué, garde de sign-on.

## 6. Qualification continue

- Chaque commit : tests Python, Java, UI ; rendu et schéma de la chart.
- Chaque nuit : harnais de bout en bout contre un IBM i de qualification
  (copie, changements, arrêt/reprise, rejeu, rotation, rapprochement trois voies,
  latence), destinations GCS et S3 vers Snowflake.
- Chaque version : installation à blanc des quatre combinaisons (AWS/GCP ×
  VM/cluster), assistant piloté par un agent via MCP, vérification, désinstallation.
- Parcours UI rejoué en navigateur à 1440 px ; tableau de bord des runs.
- Un IBM i partagé gratuit distingue ses pannes d’infrastructure des échecs
  produit ; la charge exigera une cible dédiée.

## Hors périmètre de ce design

Service hébergé par l’éditeur, édition multi-locataire, sources autres qu’IBM i,
destinations autres que Snowflake.
