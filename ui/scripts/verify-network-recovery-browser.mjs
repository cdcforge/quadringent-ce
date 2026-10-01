import assert from 'node:assert/strict';

// Only the isolated browser context loses its network; no backend mutation.
const target = new URL(process.env.QUADRINGENT_BROWSER_URL ?? 'http://127.0.0.1:8857/');
assert.equal(target.protocol, 'http:');
assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(target.hostname), 'Loopback URL required');
assert.ok(!target.username && !target.password && !target.search, 'No URL credentials or query allowed');
target.hash = `/pipeline/${encodeURIComponent(process.env.QUADRINGENT_PIPELINE_ID ?? 'dev-sale')}/overview`;
const outageMode = process.env.QUADRINGENT_OUTAGE_MODE ?? 'failed-read';
assert.ok(['failed-read', 'passive'].includes(outageMode), 'Use failed-read or passive outage mode');
const { chromium } = await import(process.env.QUADRINGENT_PLAYWRIGHT_MODULE ?? 'playwright');
const browser = await chromium.launch({ headless: true,
  ...(process.env.QUADRINGENT_BROWSER_EXECUTABLE ? { executablePath: process.env.QUADRINGENT_BROWSER_EXECUTABLE } : {}) });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce' });
try {
  const page = await context.newPage();
  await page.addInitScript(() => {
    window.__forgeNetworkEvents = [];
    const record = (type) => window.__forgeNetworkEvents.push({ type, at: Date.now() });
    for (const type of ['online', 'offline']) window.addEventListener(type, () => record(type));
    const NativeEventSource = window.EventSource;
    window.EventSource = class extends NativeEventSource {
      constructor(...args) {
        super(...args);
        record('sse-created');
        for (const type of ['open', 'error', 'stream.cursor']) this.addEventListener(type, () => record(`sse-${type}`));
      }
    };
  });
  const errors = [];
  page.on('pageerror', () => errors.push('uncaught JavaScript error'));
  await page.goto(target.href);
  const connection = page.locator('.product-topbar .connection__label');
  await connection.getByText('API/SSE connecté', { exact: true }).waitFor();
  await page.getByRole('button', { name: 'Aller au diagnostic', exact: true }).waitFor();
  const before = await page.locator('h1').innerText();

  await context.setOffline(true);
  if (outageMode === 'failed-read') {
    await page.getByRole('button', { name: 'Actualiser la preuve', exact: true }).click();
    await connection.getByText('API indisponible', { exact: true }).waitFor({ timeout: 5000 });
  } else {
    // No click at all: the browser offline event must invalidate connectivity.
    await connection.getByText('Reconnexion en cours', { exact: true }).waitFor({ timeout: 5000 });
  }
  assert.match(await page.locator('h1').innerText(), /^Dernière /);
  assert.match(await page.locator('main').innerText(), /conserv|hors ligne/i);

  // Register before network restoration so a fast response cannot be missed.
  const recoveredResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.origin === target.origin && url.pathname === '/v1/overview'
      && [200, 304].includes(response.status());
  }, { timeout: 30000 });
  const started = Date.now();
  await context.setOffline(false);
  try {
    await recoveredResponse;
  } catch (error) {
    console.log(JSON.stringify({ result: 'FAIL', phase: 'automatic REST recovery',
      online: await page.evaluate(() => navigator.onLine),
      events: await page.evaluate(() => window.__forgeNetworkEvents),
      connection: await connection.innerText(),
      uncaughtErrors: errors.length }));
    throw error;
  }
  await connection.getByText('API/SSE connecté', { exact: true }).waitFor({ timeout: 30000 });
  assert.equal(await page.locator('h1').innerText(), before, 'Recovery must preserve the observed verdict');
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ result: 'PASS', outageMode, recoveredWithinMs: Date.now() - started,
    scope: 'Desktop: browser outage, automatic recovery without refresh, verdict preserved',
    limits: 'Expected offline network errors; no backend restart or silent network blackhole certified' }));
} finally {
  try { await context.setOffline(false); } finally { await browser.close(); }
}
