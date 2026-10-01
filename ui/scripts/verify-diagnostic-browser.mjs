import assert from 'node:assert/strict';

// Explicit opt-in: this check must never discover a remote environment.
const target = new URL(process.env.QUADRINGENT_BROWSER_URL ?? 'http://127.0.0.1:8853/');
assert.equal(target.protocol, 'http:');
assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(target.hostname), 'Loopback URL required');
assert.ok(!target.username && !target.password && !target.search, 'No URL credentials or query allowed');
const pipelineId = process.env.QUADRINGENT_PIPELINE_ID ?? 'review';
const { chromium } = await import(process.env.QUADRINGENT_PLAYWRIGHT_MODULE ?? 'playwright');
const browser = await chromium.launch({ headless: true,
  ...(process.env.QUADRINGENT_BROWSER_EXECUTABLE ? { executablePath: process.env.QUADRINGENT_BROWSER_EXECUTABLE } : {}) });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce' });
  const errors = [];
  page.on('pageerror', () => errors.push('pageerror'));
  page.on('console', (message) => { if (message.type() === 'error') errors.push('console.error'); });
  page.on('response', (response) => { if (response.status() >= 400) errors.push(`HTTP ${response.status()}`); });
  page.on('requestfailed', (request) => {
    if (request.failure()?.errorText !== 'net::ERR_ABORTED') errors.push('requestfailed');
  });
  target.hash = `/pipeline/${encodeURIComponent(pipelineId)}/overview`;
  await page.goto(target.href);
  const shortcut = page.getByRole('button', { name: 'Aller au diagnostic', exact: true });
  await shortcut.waitFor();
  let reached = false;
  for (let index = 0; index < 40; index++) {
    await page.keyboard.press('Tab');
    reached = await shortcut.evaluate((element) => document.activeElement === element);
    if (reached) break;
  }
  assert.ok(reached, 'Diagnostic shortcut must be reachable by keyboard');
  await page.keyboard.press('Enter');
  const observed = await page.evaluate(() => ({
    focus: document.activeElement?.id,
    top: document.activeElement?.getBoundingClientRect().top,
    bottom: document.activeElement?.getBoundingClientRect().bottom,
    hash: location.hash,
    overflow: document.documentElement.scrollWidth > innerWidth,
  }));
  assert.equal(observed.focus, 'proof-object-title');
  assert.equal(observed.hash, target.hash);
  assert.ok(observed.top >= 0 && observed.bottom <= 1000, 'Diagnostic title must be visible');
  assert.equal(observed.overflow, false);
  await page.getByRole('link', { name: 'Mesures', exact: true }).click();
  await page.locator('.live-proof-context').waitFor();
  assert.equal(await shortcut.count(), 0, 'No shortcut without a diagnostic target');
  assert.equal(new URL(page.url()).hash, `#/pipeline/${encodeURIComponent(pipelineId)}/live`);
  assert.deepEqual(errors, [], 'No page, console or network errors');
  console.log('PASS desktop diagnostic: tab order, focus, visibility, route, measurements, browser errors');
} finally {
  await browser.close();
}
