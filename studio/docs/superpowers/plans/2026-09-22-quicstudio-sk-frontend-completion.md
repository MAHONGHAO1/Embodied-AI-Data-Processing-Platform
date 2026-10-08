# QuicStudio 外部工具接入前端收尾实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 补齐平台 Web 前端在「外部工具接入」规格上缺失的三块能力：令牌自助管理全屏页、数采审核包内不合格归类（全屏审核页）、审核「仅算法标注/低置信度」筛选。

**架构：** 令牌管理为纯前端（后端 `/api/v1/tokens` 四端点已完备）；数采审核从 680px 抽屉迁到独立 `intake-review` 全屏 view（同工作台模式），抽屉只留只读详情；审核筛选在 `/review-work-items` 列表 payload 补 `source`/`confidence` 两个只读字段，前端审核队列加客户端过滤。

**技术栈：** FastAPI + SQLAlchemy（后端仅 1 处列表 payload 只读字段）；前端 vanilla Vue + Element Plus 单文件 `app.js` + `api.js` + `demo-data.js`；测试 `node --test frontend/tests/*.test.mjs` + `make demo-check` + pytest。

**规格：** [2026-09-22 外部工具接入前端收尾规格](../specs/2026-09-22-quicstudio-sk-frontend-completion-design.md)

---

## 执行状态（每轮更新）

- 规格：`docs/superpowers/specs/2026-09-22-quicstudio-sk-frontend-completion-design.md`（已批准）
- 分支：`feature/data-backend-completion`
- 测试命令：前端 `node --test frontend/tests/<file>`；demo `make demo-check`；
  后端 `TEST_DATABASE_URL='postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test' TEST_REDIS_URL='redis://127.0.0.1:6379/15' backend/.venv/bin/python -m pytest <路径> -q`

## 文件结构

| 文件 | 职责 |
|---|---|
| `frontend/js/api.js`（修改） | 新增 `listApiTokens` / `createApiToken` / `rotateApiToken` / `revokeApiToken` |
| `frontend/js/app.js`（修改） | 令牌全屏 view、`intake-review` 审核 view、审核队列筛选栏；i18n zh/en |
| `frontend/js/access-policy.js`（修改） | `VIEW_RULES` 增加 `intake-review`（复用 `episode:read`） |
| `frontend/js/demo-data.js`（修改） | mock `/tokens` 四端点；数据包详情补 admission 字段；`reviewWorkItems` 补 source/confidence |
| `scripts/demo-smoke.mjs`（修改） | VIEWS 增加 `intake-review`（带 `?package_id=5102`） |
| `backend/data/routers/review_work_items.py`（修改） | `_item_payload` 补 `source` / `confidence` 只读字段 |
| `frontend/tests/api-tokens-console.test.mjs`（新建） | 令牌 API + 演示数据断言 |
| `frontend/tests/intake-review-console.test.mjs`（新建） | 数采审核页 API + 演示数据断言 |
| `frontend/tests/annotation-queue.test.mjs`（修改） | 追加审核来源筛选断言 |
| `backend/tests/test_review_work_items_api.py`（修改） | 追加 review 列表 payload 断言 |

---

## 任务 1：令牌管理 — API 客户端方法与 demo mock

**文件：**
- 修改：`frontend/js/api.js`
- 修改：`frontend/js/demo-data.js`
- 测试：`frontend/tests/api-tokens-console.test.mjs`（新建）

- [ ] **步骤 1：编写失败的测试**

创建 `frontend/tests/api-tokens-console.test.mjs`：

```js
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const apiSource = readFileSync(new URL('../js/api.js', import.meta.url), 'utf8');
const demoSource = readFileSync(new URL('../js/demo-data.js', import.meta.url), 'utf8');

function createDemoApi() {
  const sandbox = {
    window: { location: { hostname: '127.0.0.1', origin: 'http://127.0.0.1:8090', search: '?demo=1' } },
    URL,
    URLSearchParams,
    AbortController,
    console,
    structuredClone,
    setTimeout,
    clearTimeout,
    fetch: () => { throw new Error('demo mode must not call the network'); },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(`${demoSource}\n;globalThis.QuicDataDemo = QuicDataDemo;`, sandbox, { filename: 'demo-data.js' });
  vm.runInContext(`${apiSource}\n;globalThis.QuicDataAPI = QuicDataAPI;`, sandbox, { filename: 'api.js' });
  return sandbox.QuicDataAPI;
}

test('api.js exposes the four token endpoints', () => {
  for (const method of ['listApiTokens', 'createApiToken', 'rotateApiToken', 'revokeApiToken']) {
    assert.match(apiSource, new RegExp(`\\b${method}\\s*\\(`));
  }
});

test('demo mode issues, lists without secret, rotates, and revokes tokens', async () => {
  const api = createDemoApi();
  await api.login('demo@local.preview', 'x');
  const created = await api.createApiToken({ name: 'duance-运维机', expires_in_days: 90 });
  assert.match(created.secret, /^qs_[A-Za-z0-9]+_[A-Za-z0-9]+$/);
  const listed = await api.listApiTokens();
  assert.ok(listed.items.length >= 1);
  assert.equal('secret' in listed.items[0], false);
  const rotated = await api.rotateApiToken(created.id);
  assert.match(rotated.secret, /^qs_/);
  assert.notEqual(rotated.secret, created.secret);
  const revoked = await api.revokeApiToken(created.id);
  assert.equal(revoked.revoked_at != null, true);
  assert.equal('secret' in revoked, false);
});
```

- [ ] **步骤 2：运行测试验证失败**

运行：`node --test frontend/tests/api-tokens-console.test.mjs`
预期：FAIL（`listApiTokens` 等未定义 / 断言不通过）

- [ ] **步骤 3：实现 api.js 四个方法**

在 `frontend/js/api.js` 的 `changePassword` 方法（约 :234）附近追加：

```js
    listApiTokens() {
      return request('GET', '/tokens');
    },
    createApiToken(body) {
      return request('POST', '/tokens', { body });
    },
    rotateApiToken(id) {
      return request('POST', `/tokens/${encodeURIComponent(String(id))}/rotate`);
    },
    revokeApiToken(id) {
      return request('DELETE', `/tokens/${encodeURIComponent(String(id))}`);
    },
```

- [ ] **步骤 4：实现 demo-data.js 的 `/tokens` mock**

在 `frontend/js/demo-data.js` 顶层（`dataPackages` 数组附近，约 :400）新增：

```js
  const apiTokens = [
    { id: 9001, name: 'duance-运维机', key_id: 'k8f2x9', expires_at: null, rotated_at: null, last_used_at: '2026-09-21T08:00:00Z', revoked_at: null, created_at: '2026-06-15T02:00:00Z' },
  ];
```

在 `read` 函数中（`/data-packages` 分支之前，约 :754 上方）新增：

```js
    if (path === '/tokens') return { items: apiTokens.map((item) => { const { secret, ...rest } = item; return rest; }), total: apiTokens.length };
```

在 `write` 函数（`/data-packages` POST 处理之后，约 :1394 附近）新增：

```js
    if (method === 'POST' && path === '/tokens') {
      const id = nextId();
      const secret = `qs_${('t' + Date.now().toString(36)).slice(0, 8)}_${Math.random().toString(36).slice(2, 10)}`;
      const expiresInDays = body.expires_in_days ?? 90;
      const expiresAt = expiresInDays >= 3650 ? null : new Date(Date.now() + expiresInDays * 86400000).toISOString();
      const row = { id, name: body.name || '未命名令牌', key_id: secret.split('_')[1], expires_at: expiresAt, rotated_at: null, last_used_at: null, revoked_at: null, created_at: nowIso() };
      apiTokens.unshift(row);
      return { ...row, secret };
    }
    const tokenRevokeMatch = path.match(/^\/tokens\/(\d+)$/);
    if (method === 'DELETE' && tokenRevokeMatch) {
      const row = apiTokens.find((item) => Number(item.id) === Number(tokenRevokeMatch[1]));
      if (row) row.revoked_at = nowIso();
      const view = row ? { ...row } : { id: Number(tokenRevokeMatch[1]) };
      delete view.secret;
      return view;
    }
    const tokenRotateMatch = path.match(/^\/tokens\/(\d+)\/rotate$/);
    if (method === 'POST' && tokenRotateMatch) {
      const row = apiTokens.find((item) => Number(item.id) === Number(tokenRotateMatch[1]));
      if (row) { row.rotated_at = nowIso(); row.last_used_at = null; }
      const secret = `qs_${(row ? row.key_id : 'rot')}_${Math.random().toString(36).slice(2, 10)}`;
      return { ...(row ? { ...row } : { id: Number(tokenRotateMatch[1]) }), secret };
    }
```

注意：`read` 列表分支用 `({ secret, ...rest }) => rest` 排除 secret（demo 行本无 secret，`key_id` 保留供 UI 展示）。

- [ ] **步骤 5：运行测试验证通过**

运行：`node --test frontend/tests/api-tokens-console.test.mjs`
预期：PASS

- [ ] **步骤 6：Commit**

```bash
git add frontend/js/api.js frontend/js/demo-data.js frontend/tests/api-tokens-console.test.mjs
git commit -m "feat(frontend): add self service api token client and demo data"
```

---

## 任务 2：令牌管理 — 全屏 view UI 与 i18n

**文件：**
- 修改：`frontend/js/app.js`
- 测试：`frontend/tests/api-tokens-console.test.mjs`（追加 UI 断言）

- [ ] **步骤 1：编写失败的测试**

在 `api-tokens-console.test.mjs` 追加：

```js
const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');

test('app.js wires the account menu to a full-screen token view', () => {
  assert.match(appSource, /command="api-tokens"/);
  assert.match(appSource, /showApiTokens/);
  assert.match(appSource, /openApiTokens\(/);
  assert.match(appSource, /loadApiTokens\(/);
  assert.match(appSource, /apiTokens:/);
});
```

- [ ] **步骤 2：运行测试验证失败**

运行：`node --test frontend/tests/api-tokens-console.test.mjs`
预期：FAIL（app.js 未接线）

- [ ] **步骤 3：实现 app.js 状态与函数**

在 ref 声明区（`showChangePassword`，约 :2414）附近追加：

```js
      const showApiTokens = ref(false);
      const apiTokens = ref([]);
      const apiTokensLoading = ref(false);
      const tokenForm = reactive({ name: '', expires_in_days: 90 });
      const tokenSecret = ref(null);
      const tokenSecretVisible = ref(false);
```

在 `openChangePassword` / `cancelPasswordChange`（约 :2893）附近追加：

```js
      async function openApiTokens() {
        showApiTokens.value = true;
        tokenSecret.value = null;
        tokenSecretVisible.value = false;
        await loadApiTokens();
      }
      function cancelApiTokens() {
        showApiTokens.value = false;
        tokenSecret.value = null;
        tokenSecretVisible.value = false;
      }
      async function loadApiTokens() {
        apiTokensLoading.value = true;
        try {
          const data = await QuicDataAPI.listApiTokens();
          apiTokens.value = Array.isArray(data?.items) ? data.items : [];
        } catch (error) {
          errorMessage(error);
        } finally {
          apiTokensLoading.value = false;
        }
      }
      async function createApiToken() {
        const name = tokenForm.name.trim();
        if (!name) {
          ElMessage.error(t('apiTokenNameRequired'));
          return;
        }
        try {
          const created = await QuicDataAPI.createApiToken({ name, expires_in_days: Number(tokenForm.expires_in_days) });
          tokenSecret.value = created.secret;
          tokenSecretVisible.value = true;
          tokenForm.name = '';
          tokenForm.expires_in_days = 90;
          await loadApiTokens();
        } catch (error) {
          errorMessage(error);
        }
      }
      async function rotateApiToken(row) {
        try {
          await ElMessageBox.confirm(t('apiTokenRotateConfirm'), t('apiTokens'), {
            confirmButtonText: t('apiTokenRotate'), cancelButtonText: t('cancel'), type: 'warning',
          });
          const rotated = await QuicDataAPI.rotateApiToken(row.id);
          tokenSecret.value = rotated.secret;
          tokenSecretVisible.value = true;
          await loadApiTokens();
        } catch (error) {
          if (error !== 'cancel') errorMessage(error);
        }
      }
      async function revokeApiToken(row) {
        try {
          await ElMessageBox.confirm(t('apiTokenRevokeConfirm', { name: row.name }), t('apiTokens'), {
            confirmButtonText: t('apiTokenRevoke'), cancelButtonText: t('cancel'), type: 'danger',
          });
          await QuicDataAPI.revokeApiToken(row.id);
          ElMessage.success(t('apiTokenRevoked'));
          await loadApiTokens();
        } catch (error) {
          if (error !== 'cancel') errorMessage(error);
        }
      }
      function copyTokenSecret() {
        if (!tokenSecret.value) return;
        const text = tokenSecret.value;
        if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
          navigator.clipboard.writeText(text);
        }
        ElMessage.success(t('apiTokenCopied'));
      }
      function apiTokenStatus(row) {
        if (row?.revoked_at) return { label: t('apiTokenRevoked'), type: 'info' };
        if (row?.expires_at && new Date(row.expires_at).getTime() < Date.now()) return { label: t('apiTokenExpired'), type: 'danger' };
        return { label: t('apiTokenActive'), type: 'success' };
      }
```

在 `handleAccountCommand`（约 :4611）追加分支：

```js
        if (command === 'api-tokens') void openApiTokens();
```

- [ ] **步骤 4：实现 app.js 模板与 i18n**

账号菜单 `el-dropdown-menu`（约 :11350）在「修改密码」后追加：

```html
                      <el-dropdown-item divided command="api-tokens">{{ t('apiTokens') }}</el-dropdown-item>
```

全屏 view 条件链（`showChangePassword` section 之后，约 :11288）追加：

```html
        <section v-else-if="showApiTokens" class="login-view" aria-label="API 令牌">
          <div class="login-copy"><strong>QuicStudio</strong><span>{{ t('apiTokens') }}</span></div>
          <section class="surface-panel table-panel" style="width: min(960px, 92vw); max-height: 78vh; overflow: auto;">
            <div class="panel-heading">
              <div><h2>{{ t('apiTokens') }}</h2></div>
              <div class="panel-actions">
                <div class="list-filter-bar" style="display: flex; gap: 8px;">
                  <el-input v-model="tokenForm.name" :placeholder="t('apiTokenName')" style="max-width: 220px;" />
                  <el-select v-model="tokenForm.expires_in_days" style="width: 130px;">
                    <el-option :label="'30 ' + t('daysUnit')" :value="30" />
                    <el-option :label="'90 ' + t('daysUnit')" :value="90" />
                    <el-option :label="'180 ' + t('daysUnit')" :value="180" />
                    <el-option :label="'365 ' + t('daysUnit')" :value="365" />
                    <el-option :label="t('apiTokenNever')" :value="3650" />
                  </el-select>
                  <el-button size="small" type="primary" @click="createApiToken">{{ t('apiTokenCreate') }}</el-button>
                  <el-button size="small" @click="cancelApiTokens">{{ t('backToConsole') }}</el-button>
                </div>
              </div>
            </div>
            <div v-if="tokenSecretVisible && tokenSecret" class="drawer-section" style="padding: 16px;">
              <el-alert type="warning" :closable="false" show-icon :title="t('apiTokenOnce')" style="margin-bottom: 10px;" />
              <div style="display: flex; gap: 8px; align-items: center;">
                <code style="flex: 1; word-break: break-all; padding: 10px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px;">{{ tokenSecret }}</code>
                <el-button size="small" type="primary" @click="copyTokenSecret">{{ t('apiTokenCopy') }}</el-button>
              </div>
            </div>
            <el-table v-if="apiTokens.length" :data="apiTokens" class="data-table" size="small" v-loading="apiTokensLoading">
              <el-table-column prop="name" :label="t('apiTokenName')" min-width="150" />
              <el-table-column prop="key_id" label="key_id" width="120" />
              <el-table-column :label="t('apiTokenCreated')" width="150"><template #default="scope">{{ formatDate(scope.row.created_at) }}</template></el-table-column>
              <el-table-column :label="t('apiTokenExpires')" width="150"><template #default="scope">{{ scope.row.expires_at ? formatDate(scope.row.expires_at) : t('apiTokenNever') }}</template></el-table-column>
              <el-table-column :label="t('apiTokenLastUsed')" width="150"><template #default="scope">{{ scope.row.last_used_at ? formatDate(scope.row.last_used_at) : '—' }}</template></el-table-column>
              <el-table-column :label="t('status')" width="90"><template #default="scope"><el-tag size="small" :type="apiTokenStatus(scope.row).type">{{ apiTokenStatus(scope.row).label }}</el-tag></template></el-table-column>
              <el-table-column :label="t('operation')" width="160" fixed="right">
                <template #default="scope">
                  <el-button link type="primary" @click="rotateApiToken(scope.row)">{{ t('apiTokenRotate') }}</el-button>
                  <el-button link type="danger" @click="revokeApiToken(scope.row)">{{ t('apiTokenRevoke') }}</el-button>
                </template>
              </el-table-column>
            </el-table>
            <el-empty v-else :description="t('emptyApiTokens')" />
          </section>
        </section>
```

i18n zh（`MESSAGES['zh-CN']`，约 :17 附近追加）：

```js
      apiTokens: 'API 令牌', apiTokenCreate: '创建令牌', apiTokenOnce: '该令牌仅显示一次，关闭后无法再次查看。', apiTokenCopy: '复制',
      apiTokenName: '名称', apiTokenNameRequired: '请输入令牌名称', apiTokenExpires: '过期时间', apiTokenExpiresLabel: '有效期',
      apiTokenNever: '永不过期', apiTokenLastUsed: '最后使用', apiTokenActive: '有效', apiTokenRevoked: '已吊销',
      apiTokenExpired: '已过期', apiTokenRotate: '轮换', apiTokenRotateConfirm: '轮换将生成新的 secret，旧 secret 在一小时内仍有效。继续？',
      apiTokenRevokeConfirm: '确定吊销令牌「{name}」？吊销后立即失效。', apiTokenRevoked: '令牌已吊销', apiTokenCopied: '已复制',
      emptyApiTokens: '暂无令牌。创建一枚令牌供外部工具（duance / 算法）调用平台。', backToConsole: '返回控制台', daysUnit: '天',
```

i18n en（`MESSAGES['en-US']`，约 :183 附近追加）：

```js
      apiTokens: 'API tokens', apiTokenCreate: 'Create token', apiTokenOnce: 'This token is shown only once and cannot be viewed again.', apiTokenCopy: 'Copy',
      apiTokenName: 'Name', apiTokenNameRequired: 'Token name is required', apiTokenExpires: 'Expires', apiTokenExpiresLabel: 'Validity',
      apiTokenNever: 'Never expires', apiTokenLastUsed: 'Last used', apiTokenActive: 'Active', apiTokenRevoked: 'Revoked',
      apiTokenExpired: 'Expired', apiTokenRotate: 'Rotate', apiTokenRotateConfirm: 'Rotation issues a new secret; the old secret stays valid for one hour. Continue?',
      apiTokenRevokeConfirm: 'Revoke token "{name}"? It becomes invalid immediately.', apiTokenRevoked: 'Token revoked', apiTokenCopied: 'Copied',
      emptyApiTokens: 'No tokens yet. Create one so external tools (duance / algorithms) can call the platform.', backToConsole: 'Back to console', daysUnit: 'days',
```

创建表单直接内嵌在面板头部（上面模板已含），**不新增** `tokenFormVisible` ref 与独立弹窗。

在 setup 返回对象（约 :11206 附近 `showChangePassword` 所在行）追加导出：

```js
        showApiTokens, apiTokens, apiTokensLoading, tokenForm, tokenSecret, tokenSecretVisible,
        openApiTokens, cancelApiTokens, loadApiTokens, createApiToken, rotateApiToken, revokeApiToken,
        copyTokenSecret, apiTokenStatus,
```

- [ ] **步骤 5：运行测试验证通过**

运行：`node --test frontend/tests/api-tokens-console.test.mjs`
预期：PASS

- [ ] **步骤 6：Commit**

```bash
git add frontend/js/app.js frontend/tests/api-tokens-console.test.mjs
git commit -m "feat(frontend): add self service api token full screen view"
```

---

## 任务 3：审核算法筛选 — 后端 review payload

**文件：**
- 修改：`backend/data/routers/review_work_items.py`
- 测试：`backend/tests/test_review_work_items_api.py`

- [ ] **步骤 1：编写失败的测试**

在 `backend/tests/test_review_work_items_api.py` 末尾追加：

```python
def test_review_list_exposes_algorithm_source_and_confidence(client, db_session):
    workspace, batch, ann_item, rev_item, annotator, reviewer = _seed_submitted_review(db_session)
    ann_item.draft_json = {
        "mode": "partitioned",
        "segments": [],
        "source": {
            "kind": "algorithm",
            "name": "ego-vl",
            "version": "1.3.0",
            "run_id": "run-001",
            "confidence": 0.82,
        },
        "review_required": True,
    }
    db_session.commit()

    listed = client.get(
        "/api/v1/review-work-items",
        headers=_headers_for(reviewer),
        params={"workspace_id": workspace.id},
    )
    assert listed.status_code == 200
    payload = next(item for item in listed.json()["data"]["items"] if item["id"] == rev_item.id)
    assert payload["source"]["kind"] == "algorithm"
    assert payload["confidence"] == 0.82


def test_review_list_omits_source_when_missing(client, db_session):
    workspace, _batch, ann_item, rev_item, _annotator, reviewer = _seed_submitted_review(db_session)
    ann_item.draft_json = {"mode": "whole"}
    db_session.commit()

    listed = client.get(
        "/api/v1/review-work-items",
        headers=_headers_for(reviewer),
        params={"workspace_id": workspace.id},
    )
    payload = next(item for item in listed.json()["data"]["items"] if item["id"] == rev_item.id)
    assert payload["source"] is None
    assert payload["confidence"] is None
```

- [ ] **步骤 2：运行测试验证失败**

运行：`TEST_DATABASE_URL='postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test' TEST_REDIS_URL='redis://127.0.0.1:6379/15' backend/.venv/bin/python -m pytest backend/tests/test_review_work_items_api.py -q -k source`
预期：FAIL（payload 无 `source` / `confidence` 键）

- [ ] **步骤 3：实现最少代码**

在 `backend/data/routers/review_work_items.py` `_item_payload`（:73-87）返回值中追加：

```python
    source = (item.annotation_item.draft_json or {}).get("source")
    return {
        # ... 现有键 ...
        "source": source,
        "confidence": (source or {}).get("confidence"),
    }
```

- [ ] **步骤 4：运行测试验证通过**

运行：`TEST_DATABASE_URL='postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test' TEST_REDIS_URL='redis://127.0.0.1:6379/15' backend/.venv/bin/python -m pytest backend/tests/test_review_work_items_api.py -q`
预期：PASS（含全部既有用例）

- [ ] **步骤 5：Commit**

```bash
git add backend/data/routers/review_work_items.py backend/tests/test_review_work_items_api.py
git commit -m "feat(review): expose annotation source and confidence in review list"
```

---

## 任务 4：审核算法筛选 — 前端筛选栏与来源列

**文件：**
- 修改：`frontend/js/app.js`
- 修改：`frontend/js/demo-data.js`
- 测试：`frontend/tests/annotation-queue.test.mjs`

- [ ] **步骤 1：编写失败的测试**

在 `frontend/tests/annotation-queue.test.mjs` 末尾追加：

```js
test('review queue surfaces algorithm source and low-confidence filter', () => {
  assert.match(appSource, /reviewFilterState/);
  assert.match(appSource, /source\.kind === 'algorithm'/);
  assert.match(appSource, /confidence.*threshold/);
  assert.match(appSource, /lowConfidence/);
});
```

并确保该测试文件已 `readFileSync` app.js（若没有则补 `const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');`）。

- [ ] **步骤 2：运行测试验证失败**

运行：`node --test frontend/tests/annotation-queue.test.mjs`
预期：FAIL（app.js 无 reviewFilterState）

- [ ] **步骤 3：实现 demo-data.js review 样例**

`frontend/js/demo-data.js` `reviewWorkItems`（约 :518）给两条补 `source`：

```js
    { id: 7101, workspace_id: 1, data_batch_id: 6601, assignee_user_id: 34, status: 'pending_review', updated_at: '2026-08-20T09:10:00Z', episode_uid: 'EGO-20260811-0008', task_label: taskLabels[1], reason: null,
      source: { kind: 'algorithm', name: 'ego-vl', version: '1.3.0', run_id: 'run-001', confidence: 0.82 } },
```

（在 7101 行补 `source`；7102 等保留无 source 以验证「人工标注」分支。）

- [ ] **步骤 4：实现 app.js 筛选状态与队列过滤**

在 `reviewQueueRows` computed（约 :1809）附近追加状态与过滤：

```js
      const reviewFilterState = reactive({ source: '', lowConfidence: false, threshold: 0.7 });
      function reviewSourceKind(row) {
        const source = row?.source || (row?.work_item?.source) || null;
        if (source && source.kind === 'algorithm') return 'algorithm';
        if (source && source.kind === 'human') return 'human';
        return 'human';
      }
      function reviewRowConfidence(row) {
        const source = row?.source || null;
        return typeof source?.confidence === 'number' ? source.confidence : null;
      }
      const reviewFilteredRows = computed(() => {
        let rows = reviewQueueRows.value;
        if (reviewFilterState.source === 'algorithm') {
          rows = rows.filter((row) => reviewSourceKind(row) === 'algorithm');
        } else if (reviewFilterState.source === 'human') {
          rows = rows.filter((row) => reviewSourceKind(row) === 'human');
        }
        if (reviewFilterState.lowConfidence) {
          const threshold = Number(reviewFilterState.threshold) || 0.7;
          rows = rows.filter((row) => {
            const confidence = reviewRowConfidence(row);
            return confidence != null && confidence < threshold;
          });
        }
        return rows;
      });
      const reviewFilterActive = computed(() => (
        Boolean(reviewFilterState.source) || reviewFilterState.lowConfidence
      ));
      function clearReviewFilters() {
        Object.assign(reviewFilterState, { source: '', lowConfidence: false, threshold: 0.7 });
      }
      function reviewSourceTag(row) {
        const source = row?.source || null;
        if (source && source.kind === 'algorithm') {
          const confidence = typeof source.confidence === 'number' ? ` · ${Math.round(source.confidence * 100)}%` : '';
          return { label: `${source.name || '算法'}@${source.version || '?'}${confidence}`, type: 'warning' };
        }
        return { label: t('reviewHumanSource'), type: 'info' };
      }
```

将审核治理队列表格的 `:data` 从 `governanceAnnotationQueueRows` 替换为 `reviewFilteredRows`——但该 computed 同时服务 annotation 与 review 两阶段。改为：在 `governanceQueueActive` 内当 `queueStage === 'review'` 时用 `reviewFilteredRows`，annotation 阶段保持原样：

在模板治理表格（约 :12692）`el-table :data` 改为：

```html
                      <el-table v-if="governanceFilteredRows.length" :data="governanceFilteredRows" class="data-table" fit row-key="row_key" v-loading="loading.queue">
```

并在 `reviewFilteredRows` 之前追加通用投影：

```js
      const governanceFilteredRows = computed(() => (
        queueStage.value === 'review' ? reviewFilteredRows.value : governanceAnnotationQueueRows.value
      ));
```

在治理表格上方（`<section class="surface-panel table-panel">` 内部、`el-table` 之前）追加筛选栏（仅 review 阶段显示）：

```html
                    <div v-if="queueStage === 'review'" class="list-filter-bar work-queue-filter-bar">
                      <el-select v-model="reviewFilterState.source" clearable :placeholder="t('reviewSourceFilter')" style="min-width: 150px;">
                        <el-option :label="t('reviewSourceAll')" value="" />
                        <el-option :label="t('reviewSourceAlgorithm')" value="algorithm" />
                        <el-option :label="t('reviewSourceHuman')" value="human" />
                      </el-select>
                      <el-checkbox v-model="reviewFilterState.lowConfidence">{{ t('reviewLowConfidence') }}</el-checkbox>
                      <el-select v-if="reviewFilterState.lowConfidence" v-model="reviewFilterState.threshold" style="width: 110px;">
                        <el-option label="< 0.6" :value="0.6" />
                        <el-option label="< 0.7" :value="0.7" />
                        <el-option label="< 0.8" :value="0.8" />
                      </el-select>
                      <el-button v-if="reviewFilterActive" @click="clearReviewFilters">{{ t('clearFilters') }}</el-button>
                    </div>
```

（筛选纯客户端 computed，控件**不触发** `reloadWorkQueueFromFirstPage`，与既有 `annotationFilterState` 同模式。）

治理表格追加「来源」列（`assignee` 列之后）：

```html
                        <el-table-column :label="t('reviewSource')" width="150"><template #default="scope"><el-tag size="small" :type="reviewSourceTag(scope.row).type">{{ reviewSourceTag(scope.row).label }}</el-tag></template></el-table-column>
```

i18n zh 追加：

```js
      reviewSourceFilter: '标注来源', reviewSourceAll: '全部标注', reviewSourceAlgorithm: '仅算法标注', reviewSourceHuman: '仅人工标注',
      reviewLowConfidence: '仅看低置信度', reviewSource: '来源', reviewHumanSource: '人工',
```

i18n en 追加：

```js
      reviewSourceFilter: 'Source', reviewSourceAll: 'All annotations', reviewSourceAlgorithm: 'Algorithm only', reviewSourceHuman: 'Human only',
      reviewLowConfidence: 'Low confidence only', reviewSource: 'Source', reviewHumanSource: 'Human',
```

setup 返回对象追加：

```js
        reviewFilterState, reviewFilteredRows, governanceFilteredRows, reviewFilterActive, clearReviewFilters, reviewSourceTag,
```

- [ ] **步骤 5：运行测试验证通过**

运行：`node --test frontend/tests/annotation-queue.test.mjs`
预期：PASS

- [ ] **步骤 6：Commit**

```bash
git add frontend/js/app.js frontend/js/demo-data.js frontend/tests/annotation-queue.test.mjs
git commit -m "feat(review): filter review queue by algorithm source and low confidence"
```

---

## 任务 5：数采审核 — 路由、权限与演示数据

**文件：**
- 修改：`frontend/js/access-policy.js`
- 修改：`frontend/js/app.js`
- 修改：`frontend/js/demo-data.js`
- 修改：`scripts/demo-smoke.mjs`
- 测试：`frontend/tests/intake-review-console.test.mjs`（新建）

- [ ] **步骤 1：编写失败的测试**

创建 `frontend/tests/intake-review-console.test.mjs`：

```js
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');
const policySource = readFileSync(new URL('../js/access-policy.js', import.meta.url), 'utf8');
const demoSource = readFileSync(new URL('../js/demo-data.js', import.meta.url), 'utf8');
const apiSource = readFileSync(new URL('../js/api.js', import.meta.url), 'utf8');

test('access policy authorizes the intake-review view', () => {
  assert.match(policySource, /intake-review\s*:\s*\[/);
});

test('app.js registers the intake-review view and review interaction', () => {
  assert.match(appSource, /'intake-review'/);
  assert.match(appSource, /openIntakeReview\(/);
  assert.match(appSource, /rejectedEpisodeIds/);
  assert.match(appSource, /submitIntakeApprove\(/);
  assert.match(appSource, /admission_reason/);
  assert.match(appSource, /privacy_sensitive/);
});

function createDemoApi() {
  const sandbox = {
    window: { location: { hostname: '127.0.0.1', origin: 'http://127.0.0.1:8090', search: '?demo=1' } },
    URL, URLSearchParams, AbortController, console, structuredClone, setTimeout, clearTimeout,
    fetch: () => { throw new Error('demo mode must not call the network'); },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(`${demoSource}\n;globalThis.QuicDataDemo = QuicDataDemo;`, sandbox, { filename: 'demo-data.js' });
  vm.runInContext(`${apiSource}\n;globalThis.QuicDataAPI = QuicDataAPI;`, sandbox, { filename: 'api.js' });
  return sandbox.QuicDataAPI;
}

test('demo package detail carries admission grouping for review', async () => {
  const api = createDemoApi();
  await api.login('demo@local.preview', 'x');
  const detail = await api.getDataPackage(5102, 1);
  assert.ok(detail.admission_counts, 'admission_counts missing');
  assert.ok(detail.admission_counts.failed > 0, 'demo needs a failed episode for review');
  assert.ok(Array.isArray(detail.episodes) && detail.episodes.length >= 3);
  const failed = detail.episodes.find((item) => item.admission_status === 'failed');
  assert.ok(failed && failed.admission_reason, 'failed episode must carry a reason');
  const sensitive = detail.episodes.find((item) => item.privacy_sensitive === true);
  assert.ok(sensitive, 'demo needs a privacy-sensitive episode');
  assert.ok(detail.intake_review === null || detail.intake_review.verdict, 'intake_review shape ok');
});

test('demo intake review accepts rejected episode ids', async () => {
  const api = createDemoApi();
  await api.login('demo@local.preview', 'x');
  const detail = await api.getDataPackage(5102, 1);
  const rejected = detail.episodes.slice(0, 1).map((item) => item.id);
  const result = await api.reviewIntakePackage(5102, { workspace_id: 1, verdict: 'approved', rejected_episode_ids: rejected });
  assert.equal(result.status, 'intake_approved');
  assert.deepEqual(Array.from(result.rejected_episode_ids || []).map(Number).sort(), rejected.map(Number).sort());
});
```

- [ ] **步骤 2：运行测试验证失败**

运行：`node --test frontend/tests/intake-review-console.test.mjs`
预期：FAIL（policy 无 intake-review；demo 数据缺 admission 字段）

- [ ] **步骤 3：实现 access-policy**

`frontend/js/access-policy.js` `VIEW_RULES`（:24-48）追加：

```js
    'intake-review': ['episode:read'],
```

（不加入 `NAVIGATION_VIEWS`，保持「不占侧边栏」。）

- [ ] **步骤 4：实现 demo-data 数据包详情**

`frontend/js/demo-data.js` 包详情分支（约 :763-777）改为带归类字段：

```js
    const packageMatch = path.match(/^\/data-packages\/(\d+)$/);
    if (packageMatch) {
      const pkg = dataPackages.find((item) => Number(item.id) === Number(packageMatch[1]));
      if (!pkg) return {};
      const task = collectionTasks.find((item) => Number(item.id) === Number(pkg.collection_task_id)) || {};
      const project = collectionProjects.find((item) => Number(item.id) === Number(pkg.collection_project_id)) || {};
      const episodes = [
        { id: 4300, episode_uid: 'EGO-20260811-0007', modality: 'ego', duration_s: 125, admission_status: 'passed', validity_status: 'valid', preview_available: true, privacy_sensitive: false },
        { id: 4301, episode_uid: 'EGO-20260811-0008', modality: 'ego', duration_s: 96, admission_status: 'failed', validity_status: 'unverified', preview_available: false, privacy_sensitive: false, admission_reason: 'integrity_failed' },
        { id: 4302, episode_uid: 'EGO-20260813-0011', modality: 'ego', duration_s: 140, admission_status: 'running', validity_status: 'unverified', preview_available: false, privacy_sensitive: true },
        { id: 4310, episode_uid: 'DRV-20260812-0001', modality: 'ego', duration_s: 62, admission_status: 'passed', validity_status: 'valid', preview_available: true, privacy_sensitive: false },
      ];
      const counts = {
        ready: episodes.filter((item) => item.admission_status === 'passed').length,
        failed: episodes.filter((item) => item.admission_status === 'failed').length,
        running: episodes.filter((item) => item.admission_status === 'running').length,
        reviewed: pkg.intake_reviewed ? episodes.length : 0,
      };
      return {
        ...enrichDataPackage(pkg),
        collection_task: { id: task.id, name: task.name },
        collection_project: { id: project.id, name: project.name },
        responsible_collector: collectorProfiles.find((item) => Number(item.id) === Number(pkg.responsible_collector_id)) || null,
        device: collectionDevices.find((item) => Number(item.id) === Number(pkg.device_id)) || null,
        captured_duration_hours: '2.00',
        intake_valid_duration_hours: pkg.intake_valid_duration_hours || '0.00',
        admission_counts: counts,
        episodes: episodes.map((item) => ({ ...item, duration_hours: (item.duration_s / 3600).toFixed(2) })),
        intake_review: pkg.intake_review || null,
      };
    }
```

- [ ] **步骤 5：实现 demo-data intake-review 写路由**

`write` 中 `/data-packages/{id}` POST（约 :1370-1386）的 verdict 分支改为记录 rejected 明细：

```js
        const verdict = body.verdict || (body.approved === false || body.decision === 'reject' ? 'rejected' : 'approved');
        pkg.status = verdict === 'rejected' ? 'voided' : 'intake_approved';
        pkg.reviewed_at = nowIso();
        pkg.intake_review = {
          verdict,
          reviewer_user_id: 9000,
          accepted_episode_ids: [],
          rejected_episode_ids: Array.isArray(body.rejected_episode_ids) ? body.rejected_episode_ids.map(Number) : [],
          excluded_episodes: [],
          reason: body.reason || '',
          reviewed_at: nowIso(),
        };
```

- [ ] **步骤 6：实现 app.js 路由注册与导航分支**

`frontend/js/app.js` `VIEWS`（:11）追加 `'intake-review'`：

```js
  const VIEWS = new Set(['overview', 'intake', 'batches', 'work-queue', 'workbench', 'intake-review', 'resources', 'assets', 'buildData', 'datasets', 'trainDash', 'trainJobs', 'trainNew', 'trainDatasets', 'trainModels', 'trainResources', 'trainSystem', 'miningTasks', 'miningCloud', 'miningCuts', 'miningDash', 'miningConfig', 'admin', 'settings']);
```

`navigate`（:6885）在 `batches` 分支后追加：

```js
        if (nextView === 'intake-review') {
          const id = Number(intakeReviewPackageId.value || 0);
          if (id > 0) void loadIntakeReviewPackage(id);
        }
```

状态声明（`dataPackageDrawerVisible` 附近，约 :10008）追加：

```js
      const intakeReviewPackageId = ref(null);
      const intakeReviewPackage = ref(null);
      const intakeReviewLoading = ref(false);
      const rejectedEpisodeIds = ref([]);
```

`initialize`（:8731 `ensureActiveViewData(initialView)` 之后）追加深链接分支，支持页面刷新直达 `#/intake-review?package_id=<id>`：

```js
        if (initialView === 'intake-review') {
          const id = Number(initialRoute.query.package_id || intakeReviewPackageId.value || 0);
          if (id > 0) {
            intakeReviewPackageId.value = id;
            await loadIntakeReviewPackage(id);
          }
        }
```

- [ ] **步骤 7：实现 app.js 载入与交互函数**

在 `openDataPackageDrawer`（约 :10155）附近追加：

```js
      function enterIntakeReview(row) {
        const id = row?.id || packageDetail.value?.id;
        if (!id) return;
        intakeReviewPackageId.value = id;
        rejectedEpisodeIds.value = [];
        dataPackageDrawerVisible.value = false;
        navigate('intake-review');
      }
      async function loadIntakeReviewPackage(id) {
        intakeReviewLoading.value = true;
        try {
          const res = await QuicDataAPI.getDataPackage(id, selectedWorkspaceId.value);
          intakeReviewPackage.value = res;
        } catch (error) {
          errorMessage(error);
        } finally {
          intakeReviewLoading.value = false;
        }
      }
      function toggleEpisodeRejected(episode) {
        const ids = rejectedEpisodeIds.value.slice();
        const index = ids.indexOf(episode.id);
        if (index >= 0) ids.splice(index, 1);
        else ids.push(episode.id);
        rejectedEpisodeIds.value = ids;
      }
      function admissionReasonLabel(reason) {
        const map = {
          admission_fact_missing: t('reasonMissingFact'),
          integrity_failed: t('reasonIntegrity'),
          preview_failed: t('reasonPreview'),
          preview_unavailable: t('reasonPreview'),
          output_failed: t('reasonOutput'),
          source_fingerprint_missing: t('reasonFingerprint'),
          source_fingerprint_changed: t('reasonFingerprint'),
          validation_policy_outdated: t('reasonPolicy'),
          episode_intake_rejected: t('reasonHumanRejected'),
        };
        for (const [prefix, label] of Object.entries(map)) {
          if (String(reason || '').startsWith(prefix)) return label;
        }
        return reason || '—';
      }
      async function submitIntakeApprove() {
        const pkg = intakeReviewPackage.value;
        if (!pkg?.id) return;
        const rejectedCount = rejectedEpisodeIds.value.length;
        try {
          await ElMessageBox.confirm(
            t('intakeApprovePreview', { rejected: rejectedCount }),
            t('intakeReviewTitle'),
            { confirmButtonText: t('intakeReviewApprove'), cancelButtonText: t('cancel'), type: 'success' },
          );
          const res = await QuicDataAPI.reviewIntakePackage(pkg.id, {
            workspace_id: selectedWorkspaceId.value,
            verdict: 'approved',
            rejected_episode_ids: rejectedEpisodeIds.value,
          });
          ElMessage.success(t('intakeApproveSuccess'));
          intakeReviewPackage.value = { ...intakeReviewPackage.value, ...res };
          await loadReviewPackages();
        } catch (err) {
          if (err !== 'cancel') errorMessage(err);
        }
      }
      function backFromIntakeReview() {
        navigate('batches');
      }
      function intakeReviewCounts() {
        const counts = intakeReviewPackage.value?.admission_counts || { ready: 0, running: 0, failed: 0, reviewed: 0 };
        return counts;
      }
```

- [ ] **步骤 8：实现 app.js 门禁与工具栏排除**

- `viewRequiresWorkspace`（:1244）异常列表**不加** `intake-review`（它需要工作空间加载包）。
- 空态 `noTaskSet` 门禁（:11360）排除列表追加 `'intake-review'`。
- 工作范围工具栏排除（:11363）列表追加 `'intake-review'`。

- [ ] **步骤 9：实现 demo-smoke**

`scripts/demo-smoke.mjs` `VIEWS`（:23-28）追加 `'intake-review'`；导航 URL 构造改为：

```js
  for (const view of VIEWS) {
    consoleErrors.length = 0;
    const target = view === 'intake-review' ? '#/intake-review?package_id=5102' : `#/${view}`;
    await send('Page.navigate', { url: `http://127.0.0.1:${previewPort}/?demo=1${target}` });
```

- [ ] **步骤 10：运行测试验证通过**

运行：`node --test frontend/tests/intake-review-console.test.mjs`
预期：PASS

- [ ] **步骤 11：Commit**

```bash
git add frontend/js/access-policy.js frontend/js/app.js frontend/js/demo-data.js scripts/demo-smoke.mjs frontend/tests/intake-review-console.test.mjs
git commit -m "feat(intake): route package intake review to a full screen view"
```

---

## 任务 6：数采审核 — 全屏审核页模板与交互

**文件：**
- 修改：`frontend/js/app.js`
- 测试：`frontend/tests/intake-review-console.test.mjs`（追加模板断言）

- [ ] **步骤 1：编写失败的测试**

在 `intake-review-console.test.mjs` 追加：

```js
test('app.js renders the full screen intake review section', () => {
  assert.match(appSource, /activeView === 'intake-review'/);
  assert.match(appSource, /enterIntakeReview\(/);
  assert.match(appSource, /toggleEpisodeRejected\(/);
  assert.match(appSource, /submitIntakeApprove\(/);
  assert.match(appSource, /intakeApprovePreview/);
});
```

- [ ] **步骤 2：运行测试验证失败**

运行：`node --test frontend/tests/intake-review-console.test.mjs`
预期：FAIL（模板未渲染）

- [ ] **步骤 3：实现全屏审核页模板**

在 `batches` view（`<section v-else-if="activeView === 'batches'"`，:11578）之前插入：

```html
<section v-else-if="activeView === 'intake-review'" class="view-stack">
                  <template v-if="intakeReviewPackage">
                    <div class="page-actions page-actions-split">
                      <el-button @click="backFromIntakeReview">{{ t('backToBatches') }}</el-button>
                      <div class="page-actions-group">
                        <el-tag :type="dataPackageStatusType(intakeReviewPackage.status)">{{ dataPackageStatusLabel(intakeReviewPackage.status) }}</el-tag>
                      </div>
                    </div>
                    <section class="surface-panel table-panel" v-loading="intakeReviewLoading">
                      <div class="panel-heading">
                        <div>
                          <h2>{{ intakeReviewPackage.package_uid }}</h2>
                          <span>{{ reviewPackageProjectName(intakeReviewPackage) }} · {{ reviewPackageTaskName(intakeReviewPackage) }}</span>
                        </div>
                      </div>
                      <dl class="detail-list" style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px 16px; padding: 12px;">
                        <dt>{{ t('packageTargetDuration') }}</dt><dd>{{ intakeReviewPackage.target_duration_hours }} {{ t('hoursUnit') }}</dd>
                        <dt>{{ t('packageIntakeValidDuration') }}</dt><dd>{{ intakeReviewPackage.intake_valid_duration_hours ?? '0.00' }} {{ t('hoursUnit') }}</dd>
                        <dt>{{ t('packageResponsibleCollector') }}</dt><dd>{{ reviewPackageCollectorName(intakeReviewPackage) }}</dd>
                        <dt>{{ t('collectionDevice') }}</dt><dd>{{ reviewPackageDeviceName(intakeReviewPackage) }}</dd>
                      </dl>
                      <div style="display: flex; gap: 12px; align-items: center; padding: 8px 16px; border-top: 1px solid #e2e8f0; border-bottom: 1px solid #e2e8f0;">
                        <el-tag type="success" effect="plain">{{ t('admissionReady') }} {{ intakeReviewCounts().ready }}</el-tag>
                        <el-tag :type="intakeReviewCounts().failed ? 'danger' : 'info'" effect="plain">{{ t('admissionFailed') }} {{ intakeReviewCounts().failed }}</el-tag>
                        <el-tag type="warning" effect="plain">{{ t('admissionRunning') }} {{ intakeReviewCounts().running }}</el-tag>
                        <span v-if="intakeReviewCounts().running" class="muted" style="font-size: 12px;">{{ t('runningNotBlocking') }}</span>
                      </div>
                      <el-table v-if="(intakeReviewPackage.episodes || []).length" :data="intakeReviewPackage.episodes" class="data-table" fit :row-class-name="intakeReviewRowClass">
                        <el-table-column v-if="canReviewPackage(intakeReviewPackage)" width="56">
                          <template #default="scope">
                            <el-checkbox :model-value="rejectedEpisodeIds.includes(scope.row.id)" @change="toggleEpisodeRejected(scope.row)"></el-checkbox>
                          </template>
                        </el-table-column>
                        <el-table-column prop="episode_uid" label="Episode UID" min-width="180" />
                        <el-table-column prop="modality" :label="t('modalityLabel')" width="90" />
                        <el-table-column prop="duration_hours" :label="t('validDurationLabel')" width="100" />
                        <el-table-column :label="t('admissionStatusLabel')" width="100"><template #default="scope"><el-tag size="small" :type="scope.row.admission_status === 'passed' ? 'success' : (scope.row.admission_status === 'running' ? 'warning' : 'danger')">{{ scope.row.admission_status }}</el-tag></template></el-table-column>
                        <el-table-column :label="t('admissionReasonLabel')" min-width="160"><template #default="scope">{{ admissionReasonLabel(scope.row.admission_reason) }}</template></el-table-column>
                        <el-table-column :label="t('privacySensitive')" width="110"><template #default="scope"><el-tag v-if="scope.row.privacy_sensitive" size="small" type="danger" effect="plain">{{ t('privacySensitive') }}</el-tag></template></el-table-column>
                      </el-table>
                      <el-empty v-else :description="t('noEpisodesInPackage')" :image-size="48" />
                      <div v-if="intakeReviewPackage.intake_review" class="drawer-section" style="padding: 16px;">
                        <h4 style="margin: 0 0 8px; font-size: 14px; font-weight: 600;">{{ t('intakeReviewHistory') }}</h4>
                        <dl class="detail-list" style="padding: 12px; background: #f8fafc; border-radius: 6px; border: 1px solid #e2e8f0;">
                          <dt>{{ t('reviewStatusLabel') }}</dt><dd><el-tag size="small" :type="intakeReviewPackage.intake_review.verdict === 'approved' ? 'success' : 'danger'">{{ intakeReviewPackage.intake_review.verdict }}</el-tag></dd>
                          <dt>{{ t('acceptedEpisodes') }}</dt><dd>{{ (intakeReviewPackage.intake_review.accepted_episode_ids || []).length }}</dd>
                          <dt>{{ t('rejectedEpisodes') }}</dt><dd>{{ (intakeReviewPackage.intake_review.rejected_episode_ids || []).join(', ') || '—' }}</dd>
                          <dt>{{ t('excludedEpisodes') }}</dt><dd>{{ (intakeReviewPackage.intake_review.excluded_episodes || []).map((item) => item.episode_id + ':' + admissionReasonLabel(item.reason)).join('；') || '—' }}</dd>
                          <dt v-if="intakeReviewPackage.intake_review.reason">{{ t('rejectReasonLabel') }}</dt><dd v-if="intakeReviewPackage.intake_review.reason">{{ intakeReviewPackage.intake_review.reason }}</dd>
                        </dl>
                      </div>
                      <div v-if="canReviewPackage(intakeReviewPackage)" class="page-actions" style="padding: 16px;">
                        <el-button type="success" @click="submitIntakeApprove">{{ t('intakeReviewApprove') }}</el-button>
                        <el-button type="danger" @click="openRejectPackageDialog(intakeReviewPackage)">{{ t('intakeReviewReject') }}</el-button>
                      </div>
                    </section>
                  </template>
                  <el-empty v-else :description="t('emptyQueue')" />
                </section>
```

其中辅助函数：

```js
      function intakeReviewRowClass({ row }) {
        return row?.privacy_sensitive ? 'privacy-sensitive-row' : '';
      }
```

（`privacy-sensitive-row` 行背景高亮样式加到 `frontend/css` 中对应 `data-table` 所在样式文件；`privacy_sensitive` tag 不依赖该 CSS。）

- [ ] **步骤 4：实现 i18n**

zh 追加：

```js
      intakeReviewHistory: '审核历史', acceptedEpisodes: '已接受', rejectedEpisodes: '已标记不合格', excludedEpisodes: '已排除',
      admissionReady: '就绪', admissionFailed: '失败', admissionRunning: '处理中', runningNotBlocking: '处理中项不阻塞审核，通过时按当前事实处理',
      admissionReasonLabel: '失败原因', privacySensitive: '隐私敏感', reasonMissingFact: '未解析（文件缺失或未生成校验事实）',
      reasonIntegrity: '完整性校验失败', reasonPreview: '预览生成失败', reasonOutput: '输出校验失败',
      reasonFingerprint: '源指纹缺失/变化', reasonPolicy: '校验策略过期', reasonHumanRejected: '人工标记不合格',
      intakeApprovePreview: '通过本包？当前勾选 {rejected} 个 Episode 为不合格。', backToBatches: '返回审核队列',
```

en 追加：

```js
      intakeReviewHistory: 'Review history', acceptedEpisodes: 'Accepted', rejectedEpisodes: 'Marked unqualified', excludedEpisodes: 'Excluded',
      admissionReady: 'Ready', admissionFailed: 'Failed', admissionRunning: 'Processing', runningNotBlocking: 'Processing items do not block review; they are handled at current state on approval',
      admissionReasonLabel: 'Failure reason', privacySensitive: 'Privacy sensitive', reasonMissingFact: 'Not parsed (files missing or validation fact absent)',
      reasonIntegrity: 'Integrity check failed', reasonPreview: 'Preview generation failed', reasonOutput: 'Output verification failed',
      reasonFingerprint: 'Source fingerprint missing/changed', reasonPolicy: 'Validation policy outdated', reasonHumanRejected: 'Marked unqualified by reviewer',
      intakeApprovePreview: 'Approve this package? {rejected} Episode(s) are marked unqualified.', backToBatches: 'Back to review queue',
```

- [ ] **步骤 5：setup 返回对象追加**

```js
        intakeReviewPackageId, intakeReviewPackage, intakeReviewLoading, rejectedEpisodeIds,
        enterIntakeReview, loadIntakeReviewPackage, toggleEpisodeRejected, admissionReasonLabel,
        submitIntakeApprove, backFromIntakeReview, intakeReviewCounts, intakeReviewRowClass,
```

- [ ] **步骤 6：运行测试验证通过**

运行：`node --test frontend/tests/intake-review-console.test.mjs`
预期：PASS

- [ ] **步骤 7：Commit**

```bash
git add frontend/js/app.js frontend/tests/intake-review-console.test.mjs
git commit -m "feat(intake): render full screen intake review with unqualified marking"
```

---

## 任务 7：回归与 demo-check 收尾

**文件：**
- 修改：`frontend/tests/demo-mode-flows.test.mjs`（若 demo 数据包列表断言受影响）

- [ ] **步骤 1：全量前端测试**

运行：`node --test frontend/tests/*.test.mjs`
预期：全绿（含任务 1/2/4/5/6 新增用例）

- [ ] **步骤 2：demo 渲染检查**

运行：`make demo-check`
预期：全部视图渲染通过；`intake-review?package_id=5102` 渲染出表格与汇总，无 console 错误

- [ ] **步骤 3：后端回归**

运行：`TEST_DATABASE_URL='postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test' TEST_REDIS_URL='redis://127.0.0.1:6379/15' backend/.venv/bin/python -m pytest backend/tests/test_review_work_items_api.py backend/tests/test_annotation_batch_submit_api.py -q`
预期：PASS

- [ ] **步骤 4：规格勾选验收**

在 `docs/superpowers/specs/2026-09-22-quicstudio-sk-frontend-completion-design.md` §8 勾选验收结果，记录实跑测试数与 `make demo-check` 结果。

- [ ] **步骤 5：Commit**

```bash
git add -A
git commit -m "test(frontend): run intake review and token demo flows end to end"
```

---

## 自检

1. **规格覆盖度**：§4 令牌 → 任务 1/2；§5 数采审核全屏页 → 任务 5/6；§6 算法筛选 → 任务 3/4；§7 错误边界 → 各任务实现内覆盖（409/404 提示、secret 仅一次、running 不阻塞、无 source 视为人工、筛选无结果 empty）；§8 验收 → 任务 7。
2. **占位符扫描**：无「待定 / TODO / 后续实现」；所有步骤给出可执行代码与断言。
3. **类型一致性**：`listApiTokens/createApiToken/rotateApiToken/revokeApiToken` 在任务 1 定义、任务 2 调用；`enterIntakeReview/loadIntakeReviewPackage/toggleEpisodeRejected/submitIntakeApprove` 任务 5 定义、任务 6 模板调用；`admissionReasonLabel/reviewPackageProjectName/reviewPackageTaskName/reviewPackageCollectorName/reviewPackageDeviceName/canReviewPackage/dataPackageStatusLabel/dataPackageStatusType/openRejectPackageDialog/loadReviewPackages/reloadWorkQueueFromFirstPage` 复用既有函数，签名一致。
