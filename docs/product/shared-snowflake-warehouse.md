# Utiliser un warehouse Snowflake existant

Déclarer `site.snowflakeWarehouse` dans les values Helm, ou
`QUADRINGENT_SNOWFLAKE_WAREHOUSE` dans l'environnement local. Le rôle du
vérificateur doit disposer du privilège `USAGE` sur ce warehouse.

Une valeur explicite signifie que le warehouse est géré hors de Quadringent.
Les installations, pauses et retours arrière du produit ne le créent ni ne
le suspendent. Les noms des tables, pipes et rôles restent inchangés.
L'autosuspension et le dimensionnement restent sous le contrôle de son
propriétaire. Sans cette valeur, le comportement historique du warehouse
dédié est conservé.

Le metering global d'un warehouse partagé ne mesure pas le coût du produit.
La collecte indique donc `SharedWarehouseAttributionRequired`, sans montant
ni zéro artificiel. Une attribution par utilisateur ou query tag est nécessaire
pour chiffrer les requêtes Quadringent ; elle ne comprend pas à elle seule
les crédits d'inactivité ou les services cloud du warehouse partagé.
