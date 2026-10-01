# Télémétrie Quadringent — opt-in, gouvernance d'abord

La télémétrie sert à savoir combien d'installations vivent, sur quelle version
— pour prioriser le support et mesurer l’adoption de l’édition communautaire. Elle est **désactivée par défaut** : rien n'est émis tant que
l'exploitant ne l'active pas explicitement.

## Ce qui est envoyé (payload exact)

Un POST JSON HTTPS par jour vers l'endpoint configuré :

```json
{"v": 1, "install": "<sha256 de l'identifiant d'installation>",
 "version": "0.2.1", "tier": "community", "tables": 13,
 "ts": 1726670000}
```

- `install` : hachage SHA-256 d'un identifiant aléatoire stable (32 caractères hex) généré localement au premier
  démarrage (`/var/lib/quadringent/telemetry-install-id`, mode 0600). Ce n'est
  ni le nom du site, ni un identifiant client — le vendeur ne peut pas le
  relier à une organisation.
- `version` : version du produit (`appVersion` de la chart).
- `tier` : toujours `community` ; aucun jeton ni statut commercial n’est consulté.
- `tables` : nombre de tables du catalogue de flotte déclaré.
- `ts` : horodatage unix de l'émission.

## Ce qui n'est JAMAIS envoyé

- noms d'hôtes, adresses IP, noms de tables, de schémas ou de bases ;
- données métier, extraits de payload, clés, secrets ou en-têtes d'identité ;
- noms d'utilisateurs ou de groupes IdP ;
- contenu des journaux IBM i ou des objets S3.

## Gouvernance

- **Opt-in** : `controlPlane.telemetry.enabled=false` par défaut ; l'activation
  est un choix explicite dans les values du site.
- **Endpoint configurable** : `controlPlane.telemetry.endpoint` peut pointer
  vers le collecteur du client (par ex. un relais interne) — `https://`
  obligatoire, toute valeur non-HTTPS retombe sur l'endpoint vendeur.
- **Borné et silencieux** : un envoi par jour, timeout 5 s, tout échec est
  ignoré — la télémétrie ne dégrade jamais le produit.
- **Auditable** : le module est `src/quadringent_control_plane/telemetry.py`,
  sans dépendance externe (stdlib). Le payload ci-dessus est le seul émis.

## Côté client : observabilité complète, indépendante de la télémétrie

L'observabilité du client (SLO, alertes webhook/SNS, snapshots, documents de
preuve publiés sur son S3) ne passe pas par la télémétrie et fonctionne à 100 %
sans elle — voir `docs/product/observability-scheduler-dev.md`.
