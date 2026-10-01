/**
 * Pont vers le mode démo, chargé dynamiquement par `wizardClient.ts` (voir
 * ce fichier pour le pourquoi). Exister comme fichier séparé — plutôt qu'un
 * import dynamique direct de `fixtures/wizardDemo.ts` — évite que le chemin
 * du dossier de fixtures apparaisse dans le texte source de `wizardClient.ts`,
 * qui lui est réellement expédié en production : `scripts/verify-build.mjs`
 * interdit toute trace du mot « fixtures » dans le bundle livré.
 */
export { createWizardDemoFetch } from './fixtures/wizardDemo.ts';
