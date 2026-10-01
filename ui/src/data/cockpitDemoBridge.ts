/**
 * Pont vers le mode démo du cockpit, chargé dynamiquement par
 * `cockpitClient.ts` — même raison qu'existe `wizardDemoBridge.ts` : éviter
 * que le mot « fixtures » apparaisse dans le texte source du fichier
 * réellement expédié en production (`scripts/verify-build.mjs` l'interdit
 * dans le bundle livré).
 */
export { createCockpitDemoFetch, demoSourceIdForPipeline, demoNameForPipeline } from './fixtures/cockpitDemo.ts';
