import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
const app = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');
test('assignment entry points and computed preview are exposed', () => { assert.match(app, /openMiningAssignModeDialog\(scope\.row\)/); assert.match(app, /openMiningAssignModeDialog\(miningSelectedTask\)/); assert.match(app, /v-if="canAssignPackage\(scope\.row\)"/); assert.match(app, /QuicDataMiningUtils\.taskAssignDone/); assert.match(app, /miningPackageCountPreview/); assert.doesNotMatch(app, /miningAssignOnlyUnassigned/); });
