import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { infrastructureCostsFor } from './operator.ts';
import type { Pipeline } from './controlPlane.ts';
const now=new Date('2026-09-22T12:00:00Z');
const pipeline={quality:{evidenceKind:'live'},infrastructureCosts:{collectedAt:now.toISOString(),namespace:'quadringent-test',
 storage:{observedAt:'2026-09-21T08:00:00Z',bytes:10737418240,pricePerGibMonth:'0.037',monthlyRunRate:'0.37'},
 cluster:{start:'2026-09-21T00:00:00Z',end:'2026-09-22T00:00:00Z',allocatedAmount:'0.25',clusterAmount:'3',idleAmount:'2',currency:'USD'},
}} as unknown as Pipeline;
test('le stockage est un run-rate et le cluster garde son attribution et sa période',()=>{
 const view=infrastructureCostsFor(pipeline,now);
 assert.match(view.lines[0].value,/10/);assert.match(view.lines[1].label,/volume constant/);
 assert.match(view.lines[1].detail,/0,037/);
 assert.match(view.lines[2].value,/0,25/);assert.match(view.lines[3].value,/3,00/);
 assert.match(view.note,/OpenCost/);assert.match(view.note,/inutilisées/);
});
test('une absence, une preuve ancienne ou simulée ne devient pas un coût nul',()=>{
 for(const p of [{...pipeline,infrastructureCosts:null},{...pipeline,quality:{evidenceKind:'simulation'}},
  {...pipeline,infrastructureCosts:{...pipeline.infrastructureCosts!,collectedAt:'2026-09-19T00:00:00Z'}}]){
  const view=infrastructureCostsFor(p as Pipeline,now);assert.ok(view.lines.every(l=>l.value==='Non mesuré'));
 }
});
