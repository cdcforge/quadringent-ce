import assert from 'node:assert/strict';
import test from 'node:test';
import { shouldRedirectToWizard } from './useFirstRunRedirect.ts';

test('redirects only from the overview/index home with zero sources declared', () => {
  assert.equal(shouldRedirectToWizard('overview', 0), true);
  assert.equal(shouldRedirectToWizard('index', 0), true);
});

test('never redirects once at least one source exists', () => {
  assert.equal(shouldRedirectToWizard('overview', 1), false);
  assert.equal(shouldRedirectToWizard('overview', 5), false);
});

test('never redirects from any other screen, even with zero sources', () => {
  assert.equal(shouldRedirectToWizard('pipelines', 0), false);
  assert.equal(shouldRedirectToWizard('setup', 0), false);
  assert.equal(shouldRedirectToWizard('cockpit', 0), false);
  assert.equal(shouldRedirectToWizard('wizard', 0), false);
});
