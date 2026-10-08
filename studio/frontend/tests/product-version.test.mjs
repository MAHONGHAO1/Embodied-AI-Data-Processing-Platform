import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const apiSource = readFileSync(new URL('../js/api.js', import.meta.url), 'utf8');
const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');
const cssSource = readFileSync(new URL('../css/app.css', import.meta.url), 'utf8');

test('console reads the product version from the public backend endpoint', () => {
  assert.match(apiSource, /async getProductVersion\(\) \{[\s\S]*?if \(demoMode\) return null;[\s\S]*?`\$\{base\}\/version`/);
  assert.match(apiSource, /typeof payload\?\.data\?\.version === 'string' \? payload\.data\.version : null/);
  assert.match(appSource, /const productVersion = ref\(''\);/);
  assert.match(appSource, /onMounted\(async \(\) => \{\n        void loadProductVersion\(\);/);
  // The frontend never hard-codes its own version; the backend module is the single source.
  assert.doesNotMatch(appSource, /1\.0\.0a1/);
});

test('product version is shown beside the brand on the login page and sidebar', () => {
  assert.match(appSource, /<div class="login-copy"><strong>QuicStudio<\/strong><span>Batch \/ Episode<\/span><small v-if="productVersion" class="product-version">v\{\{ productVersion \}\}<\/small><\/div>/);
  assert.match(appSource, /<div class="sidebar-brand"[^>]*:title="productVersion \? 'QuicStudio v' \+ productVersion : 'QuicStudio'"[^>]*>[\s\S]*?<small v-if="!sidebarCollapsed && productVersion" class="product-version">v\{\{ productVersion \}\}<\/small><\/div>/);
  assert.match(cssSource, /\.sidebar-brand \.product-version \{/);
  assert.match(cssSource, /\.login-copy \.product-version \{/);
});
