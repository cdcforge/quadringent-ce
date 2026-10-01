// Preuve locale backend → SSE → navigateur, destination explicitement simulée.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';

const root=fileURLToPath(new URL('../../', import.meta.url));
const {chromium}=await import(process.env.QUADRINGENT_PLAYWRIGHT_MODULE || 'playwright');
const child=spawn(process.env.QUADRINGENT_PYTHON || 'python3',['tests/chain_browser_fixture.py'],{cwd:root,stdio:['pipe','pipe','inherit']});
const lines=createInterface({input:child.stdout})[Symbol.asyncIterator]();
async function message() {
  let timer;
  try {
    const next=await Promise.race([lines.next(),new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('fixture response timeout')),20000);})]);
    if (next.done) throw new Error('fixture exited');
    return JSON.parse(next.value);
  } finally {clearTimeout(timer);}
}
let browser;
try {
  const fixture=await message();
  assert.equal(fixture.simulation,true);
  const url=new URL(fixture.url);
  assert.equal(url.hostname,'127.0.0.1');
  browser=await chromium.launch({headless:true,...(process.env.QUADRINGENT_BROWSER_EXECUTABLE ? {executablePath:process.env.QUADRINGENT_BROWSER_EXECUTABLE} : {})});
  const page=await browser.newPage({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
  await page.addInitScript(()=>{
    window.__chainRevisions=[];
    const Original=window.EventSource;
    window.EventSource=class extends Original {
      constructor(url,options) {
        super(url,options);
        this.addEventListener('projection.updated',event=>window.__chainRevisions.push(event.lastEventId));
      }
    };
  });
  const errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.on('console',entry=>{if(entry.type()==='error') errors.push(entry.text());});
  page.on('response',response=>{if(response.status()>=400) errors.push('HTTP '+response.status());});
  await page.goto(fixture.url+'/#/pipeline/dev-sale/overview');
  const block=page.locator('main');
  await page.getByText('Démonstration — ces chiffres ne viennent pas de votre système.',{exact:true}).waitFor();
  const baseline=await page.locator('h1').innerText();
  const initial=await (await page.request.get(fixture.url+'/v1/overview')).json();
  assert.equal(initial.pipelines[0].window_delivery.chain.matched_windows,1);
  const output=await mkdtemp('/tmp/quadringent-chain-page-');
  const states=[];
  for (const command of ['restore','hide','restore']) {
    const refresh=page.waitForResponse(response=>new URL(response.url()).pathname==='/v1/overview' && [200,304].includes(response.status()));
    child.stdin.write(command+'\n');
    const update=await message();
    const response=await refresh;
    assert.equal(response.status(),200);
    const overview=await response.json();
    assert.equal(overview.revision,update.revision);
    assert.deepEqual(overview.pipelines[0].counters,initial.pipelines[0].counters);
    await page.waitForFunction(revision=>window.__chainRevisions.includes(String(revision)),update.revision);
    const chain=overview.pipelines[0].window_delivery.chain;
    assert.equal(chain.declared_windows,2);
    assert.equal(chain.matched_windows,command==='restore'?2:1);
    assert.equal(chain.evidence_kind,'simulation');
    assert.equal(await page.locator('h1').innerText(),baseline);
    assert.match(await block.innerText(),/Démonstration — ces chiffres ne viennent pas de votre système/);
    assert.match(await block.innerText(),/Non mesuré/);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:output+'/'+update.revision+'-'+command+'-1440.png',fullPage:true});
    states.push({command,revision:update.revision,matched:chain.matched_windows,declared:chain.declared_windows});
  }
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({simulation:true,output,states,errors}));
} finally {
  try {if(browser) await browser.close();}
  finally {
    if(!child.stdin.destroyed) child.stdin.end('quit\n');
    await new Promise((resolve,reject)=>{
      if(child.exitCode!==null || child.signalCode!==null || !child.pid) return resolve();
      const term=setTimeout(()=>child.kill('SIGTERM'),5000);
      const kill=setTimeout(()=>child.kill('SIGKILL'),7000);
      const deadline=setTimeout(()=>reject(new Error('fixture cleanup timeout')),9000);
      child.once('exit',()=>{clearTimeout(term);clearTimeout(kill);clearTimeout(deadline);resolve();});
    });
  }
}
