import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const apiSource = readFileSync(new URL('../js/api.js', import.meta.url), 'utf8');
const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');
const appStyles = readFileSync(new URL('../css/app.css', import.meta.url), 'utf8');

test('administrator console uses authorized user and workspace membership APIs', () => {
  for (const method of [
    'listUsers',
    'listRoles',
    'createManagedUser',
    'updateUserRole',
    'updateUserStatus',
    'listWorkspaceMembers',
    'grantWorkspaceMember',
    'revokeWorkspaceMember',
  ]) {
    assert.match(apiSource, new RegExp(`\\b${method}\\s*\\(`));
  }
  assert.match(apiSource, /\/auth\/users/);
  assert.match(apiSource, /\/workspace\/\$\{encodeURIComponent\(String\(workspaceId\)\)\}\/members/);
  assert.equal(apiSource.includes('oss://'), false);
});

test('administrator console is an admin-only navigation area with user and member controls', () => {
  assert.match(appSource, /VIEWS = new Set\(\[[^\]]*'admin'/);
  assert.match(appSource, /data-view="admin"/);
  assert.match(appSource, /canManageUsers/);
  assert.match(appSource, /activeView === 'admin'/);
  assert.match(appSource, /showManagedUserDialog/);
  assert.match(appSource, /showWorkspaceMemberDialog/);
  assert.match(appSource, /mustChangePassword/);
});

test('administrator console localizes stable role identifiers without changing their values', () => {
  assert.match(appSource, /function roleLabel\(role\)/);
  assert.match(appSource, /:label="roleLabel\(role\)" :value="role"/);
  assert.match(appSource, /roleLabel\(scope\.row\.role\)/);
});

test('administrator console exposes user activation state and status actions', () => {
  assert.match(apiSource, /updateUserStatus\(userId, isActive\)/);
  assert.match(appSource, /setManagedUserActive\(scope\.row, !scope\.row\.is_active\)/);
  assert.match(appSource, /scope\.row\.is_active \? t\('active'\) : t\('inactive'\)/);
});

test('workspace membership UI models scope without a workspace-level role', () => {
  assert.match(appSource, /workspaceMemberForm = reactive\(\{ user_id: null \}\)/);
  assert.match(appSource, /grantWorkspaceMember\(workspaceId, \{\s*user_id: userId,?\s*\}\)/);
  assert.doesNotMatch(appSource, /workspaceMemberForm\.access_level/);
  assert.doesNotMatch(appSource, /scope\.row\.access_level/);
  assert.doesNotMatch(appSource, /t\('accessLevel'\)/);
});

test('workspace membership heading keeps its title and workspace selector on one row', () => {
  assert.match(appSource, /class="panel-heading admin-members-heading"/);
  assert.match(appSource, /class="admin-members-workspace-select"/);
  assert.match(appStyles, /\.panel-heading\.admin-members-heading > \.admin-members-scope\s*\{[^}]*align-items:\s*center/);
  assert.match(appStyles, /\.admin-members-scope h2\s*\{[^}]*white-space:\s*nowrap/);
  assert.match(appStyles, /\.admin-members-heading \.panel-actions\s*\{[^}]*flex-wrap:\s*nowrap/);
});

test('administrator can reset a user password and is shown the temporary one once', () => {
  assert.match(appSource, /resetManagedUserPassword\(scope\.row\)/);
  assert.match(appSource, /QuicDataAPI\.resetUserPassword\(managedUser\.id\)/);
  assert.match(appSource, /resetPasswordOnce', \{ password:/);
  assert.match(appSource, /resetPassword: '重置密码'/);
  assert.match(appSource, /resetPassword: 'Reset password'/);
  assert.match(apiSource, /resetUserPassword\(userId\)[\s\S]*\/reset-password`/);
});
