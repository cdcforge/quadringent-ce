# Confiance TLS du site

Aucun certificat client ne se trouve dans les images. Fournir le bundle CA
validé par votre administrateur IBM i dans un Secret Kubernetes contenant
uniquement des certificats publics PEM (jamais une clé privée). Déclarer
`as400.tlsCaSecret.name`, `as400.tlsCaSecret.key` et `as400.tlsCaFile`.
La chart monte ce bundle en lecture seule ; `TlsTrust` le charge à la connexion
et refuse un fichier absent, une clé privée ou une chaîne invalide.

Pour renouveler : vérifier les empreintes hors bande, publier le bundle avec
les deux autorités pendant la transition, remplacer la référence du Secret,
puis effectuer un redémarrage contrôlé après vérification du checkpoint.
Les JVM en cours doivent être relancées pour recharger la confiance.
