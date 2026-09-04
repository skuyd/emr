# Health Home Warm UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate every public and authenticated user page to the approved “健康之家” warm-paper design system while preserving server-rendered business behavior, privacy boundaries, and progressive enhancement.

**Architecture:** Centralize visual tokens in `tokens.css`, reusable controls and content patterns in `components.css`, and shell layout in the two base stylesheets. Django template partials own repeated semantic structures, while existing page templates retain their view context and JavaScript data attributes. Page CSS controls only local layout; the original viewer stylesheet is preserved and receives only additive compatibility rules if runtime evidence proves they are needed.

**Tech Stack:** Django 5.2 templates, semantic HTML, modern CSS, inline SVG icons, vanilla JavaScript, pytest/pytest-django, Playwright

**Spec:** `docs/specs/2026-08-31-health-home-warm-ui-auth-design.md`

## Global Constraints

- Product name is `健康之家`; the subtitle is `家庭健康档案`.
- Use the exact core colors `#F7F1E7`, `#EEE3D4`, `#FFFCF7`, `#25322D`, `#6C766F`, `#B85C3F`, `#8F402A`, `#47685B`, and `#DCE7DF`.
- Use only local system serif/sans-serif stacks; load no external font, UI framework, icon package, or SPA runtime.
- Desktop authenticated navigation is `首页 / 健康档案 / 健康趋势 / 我的`; mobile adds a prominent middle `上传` destination.
- `/tasks/` remains reachable from task status and notifications but is not primary navigation.
- Preserve existing URLs, CSRF, permissions, Django form names, notification polling, upload status, source-location, original viewing, download, feedback, deletion, and safe-return behavior.
- Preserve existing unrelated worktree changes, especially `static/css/viewer.css`, `deploy/bootstrap_dev_env.py`, `tests/deploy/test_bootstrap_dev_env.py`, `tests/browser/test_ac02_upload_browser.py`, and processing-worker files.
- Original documents remain visually and semantically primary; automated results always include the exact trust note from the spec.
- JavaScript enhances only existing interactions; navigation, forms, search, confirmations, and original access work without it.
- Validate 1440px, 768px, and 390px viewports with no page-level horizontal overflow or obscured controls.
- Text and controls meet WCAG AA contrast; keyboard focus is visible; touch targets are at least 44px; state never depends on color alone.
- Respect `prefers-reduced-motion`; functional icons are simple inline SVGs, not emoji.

---

### Task 1: Warm-paper tokens and reusable semantic components

**Files:**
- Modify: `static/css/tokens.css`
- Create: `static/css/components.css`
- Create: `templates/components/_brand.html`
- Create: `templates/components/_icon.html`
- Create: `templates/components/_status_badge.html`
- Create: `templates/components/_form_errors.html`
- Create: `templates/components/_empty_state.html`
- Create: `templates/components/_record_card.html`
- Modify: `tests/test_project_configuration.py`
- Create: `tests/accessibility/test_component_markup.py`
- Create: `tests/ui/test_design_tokens.py`

**Interfaces:**
- Consumes: document/status objects and Django bound forms.
- Produces: stable `.button`, `.field`, `.status-badge`, `.notice`, `.empty-state`, `.record-card`, `.brand-lockup` classes and reusable template includes.

- [ ] **Step 1: Write failing token and component-contract tests**

```python
def test_warm_design_tokens_are_exact():
    css = Path("static/css/tokens.css").read_text(encoding="utf-8")
    for token in (
        "--color-paper: #f7f1e7", "--color-paper-deep: #eee3d4",
        "--color-surface: #fffcf7", "--color-ink: #25322d",
        "--color-muted: #6c766f", "--color-primary: #b85c3f",
        "--color-primary-dark: #8f402a", "--color-sage: #47685b",
        "--color-sage-soft: #dce7df",
    ):
        assert token in css.lower()


def test_status_badge_uses_icon_text_and_color():
    source = Path("templates/components/_status_badge.html").read_text(encoding="utf-8")
    assert "status-badge__icon" in source
    assert "{{ label }}" in source
    assert 'aria-hidden="true"' in source
```

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest tests/ui/test_design_tokens.py tests/accessibility/test_component_markup.py tests/test_project_configuration.py -q`

Expected: FAIL because shared CSS/partials and exact tokens are missing.

- [ ] **Step 3: Replace the token file with the approved system**

```css
:root {
  --color-paper: #f7f1e7;
  --color-paper-deep: #eee3d4;
  --color-surface: #fffcf7;
  --color-ink: #25322d;
  --color-muted: #6c766f;
  --color-primary: #b85c3f;
  --color-primary-dark: #8f402a;
  --color-sage: #47685b;
  --color-sage-soft: #dce7df;
  --color-focus: #8f402a;
  --color-danger: #8f2f24;
  --color-warning-ink: #674512;
  --color-warning-bg: #f8ebd3;
  --color-success-ink: #315f4c;
  --color-success-bg: #dce7df;
  --font-display: Georgia, "Songti SC", "STSong", "SimSun", serif;
  --font-body: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
  --space-1: .5rem;
  --space-2: 1rem;
  --space-3: 1.5rem;
  --space-4: 2rem;
  --radius-control: 14px;
  --radius-card: 22px;
  --shadow-card: 0 18px 48px rgb(37 50 45 / 9%);
  --line: rgb(49 64 58 / 14%);
}
```

- [ ] **Step 4: Implement shared controls and partials**

`components.css` defines 44px minimum buttons/links/inputs, primary/secondary/text/danger variants, card borders/shadows, error-summary focus, field errors linked with `aria-describedby`, state variants for `processing/saved/organized/original/failed`, empty states, visually-hidden text, and forced-colors support. `_icon.html` selects only named inline SVG paths; `_status_badge.html` combines the SVG and label; `_form_errors.html` renders a focusable top summary plus nearby errors.

- [ ] **Step 5: Verify GREEN**

Run: `python -m pytest tests/ui/test_design_tokens.py tests/accessibility/test_component_markup.py tests/test_project_configuration.py -q`

Expected: exact tokens, discoverable assets, semantic states, and persistent error associations pass.

### Task 2: Public and authenticated page shells

**Files:**
- Modify: `templates/base_public.html`
- Modify: `templates/base_app.html`
- Create: `templates/components/_app_navigation.html`
- Create: `templates/components/_notification_center.html`
- Create: `templates/components/_patient_identity.html`
- Modify: `static/css/public.css`
- Modify: `static/css/app-shell.css`
- Modify: `static/css/notifications.css`
- Modify: `static/js/app-shell.js`
- Modify: `tests/accessibility/test_shell_markup.py`
- Modify: `tests/notifications/test_views.py`

**Interfaces:**
- Consumes: `current_section`, `request.patient`, navigation task counts, unread notifications, and existing notification data attributes.
- Produces: public centered-card shell, desktop top navigation, compact mobile top bar, fixed mobile bottom navigation, patient identity, and task/notification discovery paths.

- [ ] **Step 1: Write failing shell assertions**

```python
def test_authenticated_shell_has_exact_primary_navigation(client, patient_account):
    client.force_login(patient_account)
    content = client.get("/").content.decode()
    for label in ("首页", "健康档案", "健康趋势", "我的"):
        assert f">{label}<" in content
    assert '>任务<' not in content
    assert 'href="/tasks/"' in content
    assert "的健康档案" in content
    assert 'class="mobile-nav"' in content
    assert '>上传<' in content
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/accessibility/test_shell_markup.py tests/notifications/test_views.py -q`

Expected: FAIL because the current shell still uses a sidebar and primary task item.

- [ ] **Step 3: Build the public shell**

Render the shared brand lockup, warm background, centered `main`, privacy/sensitive-information footer links, skip link, `components.css`, and page blocks. Titles default to `健康之家｜家庭健康档案`; authenticated requests visiting public policy pages retain a visible route back to `我的`.

- [ ] **Step 4: Build desktop and mobile app navigation**

Desktop uses a sticky 72–82px top bar with brand, four primary links, notification button, and patient pill. Mobile uses a 64–68px header and a fixed safe-area-aware five-item bottom bar; upload is centered, circular/raised, and has visible text. The app main container reserves bottom-nav space and never exceeds the viewport width. Keep `/tasks/` linked from the task summary and notification destinations.

- [ ] **Step 5: Preserve notification enhancement and verify GREEN**

Move existing `data-notification-*` elements into the partial without changing URLs or polling behavior. Run:

`python -m pytest tests/accessibility/test_shell_markup.py tests/notifications tests/patients/test_profile.py -q`

Expected: one current navigation item, existing notification behavior, skip links, landmarks, and profile routes all pass.

### Task 3: Authentication, onboarding, and public-information pages

**Files:**
- Modify: `templates/accounts/login.html`
- Modify: `templates/accounts/login_mfa.html`
- Modify: `templates/accounts/first_use_phone.html`
- Modify: `templates/accounts/first_use_verify.html`
- Modify: `templates/accounts/set_password.html`
- Modify: `templates/accounts/forgot_password.html`
- Modify: `templates/accounts/reset_verify.html`
- Modify: `templates/accounts/reset_password.html`
- Modify: `templates/accounts/privacy.html`
- Modify: `templates/patients/onboarding.html`
- Modify: `templates/patients/sensitive_information.html`
- Modify: `templates/patients/policy_unavailable.html`
- Modify: `templates/patients/account_deleted.html`
- Modify: `static/js/login.js`
- Modify: `tests/accounts/test_login_views.py`
- Modify: `tests/accounts/test_first_use.py`
- Modify: `tests/accounts/test_password_reset.py`
- Modify: `tests/accessibility/test_shell_markup.py`

**Interfaces:**
- Consumes: authentication-plan forms/context and shared form-error/button components.
- Produces: consistent warm public cards, direct Chinese copy, password visibility controls, and no-JavaScript form submission.

- [ ] **Step 1: Write failing copy and accessibility assertions**

```python
@pytest.mark.parametrize("path, heading", [
    ("/login/", "欢迎回到健康之家"),
    ("/login/first-use/", "第一次使用健康之家"),
    ("/login/forgot-password/", "重新设置密码"),
])
def test_public_auth_heading_and_brand(client, path, heading):
    content = client.get(path).content.decode()
    assert heading in content
    assert "健康之家" in content
    assert "家庭健康档案" in content
    assert "<label" in content
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/accounts/test_login_views.py tests/accounts/test_first_use.py tests/accounts/test_password_reset.py tests/accessibility/test_shell_markup.py -q`

Expected: FAIL on old copy/missing form semantics.

- [ ] **Step 3: Migrate all auth forms to one semantic structure**

Each page has one `h1`, concise context copy, a top error summary with `tabindex="-1"`, nearby field errors, persistent labels, correct `autocomplete`, primary submit, and textual back/alternate-flow links. Password fields use a `type="button"` visibility control with `aria-pressed`, `aria-controls`, and a text label that switches between `显示密码` and `隐藏密码`; submission remains fully functional when JS is unavailable.

- [ ] **Step 4: Migrate onboarding and information pages**

Onboarding uses inclusive copy (`为自己或家人建立健康档案`), grouped policy cards, visible checkbox focus, and current form names. Privacy/sensitive/policy-error/deletion-complete pages use the same brand/footer, explain what is currently safe and what the next action is, and preserve authenticated return paths.

- [ ] **Step 5: Verify GREEN**

Run: `python -m pytest tests/accounts tests/patients/test_onboarding_views.py tests/accessibility -q`

Expected: all auth behavior remains intact and every public form passes label/error/focus assertions.

### Task 4: Home page, status rows, and recent documents

**Files:**
- Modify: `templates/patients/home.html`
- Modify: `templates/documents/_task_card.html`
- Modify: `templates/documents/_recent_document.html`
- Modify: `static/css/home.css`
- Modify: `apps/documents/selectors.py`
- Modify: `tests/documents/test_home.py`
- Modify: `tests/accessibility/test_home_markup.py`
- Modify: `static/js/task-status.js`
- Modify: `tests/js/service-worker.test.mjs`

**Interfaces:**
- Consumes: `recent_documents`, `home_task_cards`, task polling data attributes, and browser-notification preference.
- Produces: exact home hero copy, a prominent upload card, task status section, and recent cards with date/name/institution/status/original link.

- [ ] **Step 1: Write failing home-content tests**

```python
def test_home_explains_original_first_upload(client, onboarded_account):
    client.force_login(onboarded_account)
    content = client.get("/").content.decode()
    assert "把自己和家人的健康资料，安心收在一起" in content
    assert "支持图片和 PDF 批量选择" in content
    assert "无需补录医疗信息" in content
    assert "原件先保存，再自动整理" in content
    assert 'href="/uploads/new/"' in content
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/documents/test_home.py tests/accessibility/test_home_markup.py -q`

Expected: FAIL on hero copy and recent metadata.

- [ ] **Step 3: Extend selector presentation data without changing domain rules**

Return a presentation dataclass for recent cards with report/upload date label, display filename, institution fallback, page/type summary, canonical status key/label, and original/detail URL. Extend task item projections only with display status keys needed by `_status_badge.html`; do not alter batch/document state transitions.

- [ ] **Step 4: Implement the warm home layout**

Use a two-column hero at 1440px, stacked hero by 768px, 22–24px upload card, serif headline, terracotta action, three explicit assurances, then task status and recent document sections. Empty copy is exactly `这里还没有资料。上传图片或 PDF 后，原件会先安全保存。` Task failures say whether the original is saved and expose the existing retry/detail route.

- [ ] **Step 5: Preserve polling attributes and verify GREEN**

Keep `data-task-card`, status URL, terminal flag, item IDs, live region, and notification preference attributes unchanged. Run:

`python -m pytest tests/documents/test_home.py tests/accessibility/test_home_markup.py -q`

Run: `npm run test:js`

Expected: server rendering and task polling pass with no contract drift.

### Task 5: Upload workflow and per-file outcomes

**Files:**
- Modify: `templates/documents/upload.html`
- Modify: `static/css/upload.css`
- Modify: `static/js/upload.js`
- Modify: `tests/documents/test_upload_views.py`
- Modify: `tests/accessibility/test_upload_markup.py`
- Modify carefully: `tests/browser/test_ac02_upload_browser.py`

**Interfaces:**
- Consumes: existing batch/create/content/remove/status APIs and all current `data-upload-*` selectors.
- Produces: three-step visual flow, warm dropzone, individual file status rows, and explicit partial-failure summary.

- [ ] **Step 1: Write failing upload markup/state tests**

```python
def test_upload_page_has_three_explicit_steps(client, onboarded_account):
    client.force_login(onboarded_account)
    content = client.get("/uploads/new/").content.decode()
    assert "把新资料放进健康之家" in content
    for step in ("选择文件", "确认上传", "查看保存和整理状态"):
        assert step in content
    assert "部分文件未能上传，请查看下面的文件状态" in content
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/documents/test_upload_views.py tests/accessibility/test_upload_markup.py -q`

Expected: FAIL on new hierarchy/state copy.

- [ ] **Step 3: Restyle without changing API/data contracts**

Keep file input accept/multiple, CSRF, template row, concurrency, progress, retry, remove, duplicate, leave notice, batch summary, and live-region selectors. Add numbered step indicators; use a 20–24px dashed upload surface; map states to shared badges: `正在上传`, `已保存`, `自动整理中`, `已整理`, `仅原件`, `处理失败`.

- [ ] **Step 4: Make partial failure explicit in JavaScript**

When a batch mixes terminal success and failure, set the batch summary to `部分文件未能上传，请查看下面的文件状态。已保存的原件不受影响。` and leave every row’s own result visible. Do not replace per-row output with a toast.

- [ ] **Step 5: Verify GREEN including the preserved dirty browser test**

Run: `python -m pytest tests/documents/test_upload_views.py tests/accessibility/test_upload_markup.py tests/browser/test_ac02_upload_browser.py -q`

Run: `npm run test:js`

Expected: all API behavior, individual results, progressive status, and upload browser flow pass; pre-existing edits in the browser test remain present.

### Task 6: Health archive search, filters, dates, and original-first cards

**Files:**
- Modify: `templates/documents/records.html`
- Modify: `templates/components/_record_card.html`
- Modify: `static/css/records.css`
- Modify: `apps/documents/archive.py`
- Modify: `apps/documents/views.py`
- Modify: `tests/documents/test_records.py`
- Create: `tests/accessibility/test_records_markup.py`

**Interfaces:**
- Consumes: existing query/type/status filtering, pagination, report metadata, and search snippets.
- Produces: title `收好的健康资料`, accessible keyword/type/status/date browsing, warm timeline/card groups, and explicit original/reprocess actions.

- [ ] **Step 1: Write failing archive presentation tests**

```python
def test_archive_card_exposes_required_metadata_and_original_action(client, document):
    response = client.get("/records/")
    content = response.content.decode()
    assert "收好的健康资料" in content
    assert document.display_filename in content
    assert "打开原件" in content
    assert "报告日期" in content or "日期未识别" in content
    assert "资料类型" in content
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/documents/test_records.py tests/accessibility/test_records_markup.py -q`

Expected: FAIL on title/action/date navigation.

- [ ] **Step 3: Add date browsing to the existing server-side query**

Accept bounded `year` and `month` GET values, filter on existing document date metadata only, retain them through pagination and clear links, and ignore malformed values without a 500. Do not change stored health-record fields or ownership filtering.

- [ ] **Step 4: Build timeline/cards with explicit actions**

Search stays a labeled server form; type/status/date controls remain native. Each result shows filename, report date/fallback upload date, institution fallback, type/pages, shared status badge, matched snippet when present, and an `打开原件` link to the viewer. Failed processing exposes a separate POST-backed detail/reprocess path, never a client-only action. Empty copy uses `没有找到相关资料，换个关键词试试。`.

- [ ] **Step 5: Verify GREEN**

Run: `python -m pytest tests/documents/test_records.py tests/accessibility/test_records_markup.py tests/security/test_tenant_isolation.py -q`

Expected: filtering/pagination/tenant scope and required metadata/actions pass.

### Task 7: Original-first detail, viewer, feedback, delete, and trends

**Files:**
- Modify: `templates/documents/detail.html`
- Modify: `templates/documents/_viewer_panel.html`
- Modify: `templates/documents/viewer.html`
- Modify: `templates/documents/delete_confirm.html`
- Modify: `templates/documents/trend.html`
- Modify: `static/css/detail.css`
- Modify: `static/css/trend.css`
- Modify only if evidence requires: `static/css/viewer.css`
- Modify: `tests/documents/test_detail_viewer.py`
- Modify: `tests/documents/test_deletion.py`
- Modify: `tests/labs/test_trends.py`
- Create: `tests/accessibility/test_detail_trend_markup.py`

**Interfaces:**
- Consumes: current detail context, iframe viewer, evidence IDs, page-image/original download endpoints, feedback/reprocess/delete POSTs, and trend points.
- Produces: desktop original-left/results-right, mobile original-first, exact trust note, source links, neutral trend copy, and server-rendered danger confirmation.

- [ ] **Step 1: Write failing trust and source tests**

```python
def test_detail_keeps_original_first_and_exact_trust_copy(client, document):
    content = client.get(f"/records/{document.pk}/").content.decode()
    assert content.index("原始报告") < content.index("自动整理结果")
    assert (
        "自动整理结果可能不准确，请以原始报告为准。"
        "这里只帮助查找，不提供诊断或治疗建议。"
    ) in content
    assert "下载原件" in content


def test_trend_copy_is_neutral_and_points_link_to_sources(client, trend):
    content = client.get(trend.url).content.decode()
    assert "看看指标随时间的变化" in content
    assert "查看来源原件" in content
    assert "不提供诊断、治疗或疗效结论" in content
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/documents/test_detail_viewer.py tests/documents/test_deletion.py tests/labs/test_trends.py tests/accessibility/test_detail_trend_markup.py -q`

Expected: FAIL on order, exact trust copy, download action, and trend heading.

- [ ] **Step 3: Reorder detail for original primacy**

Desktop grid places a viewer panel first and automated results second; below 75rem it stacks with the viewer first. Keep iframe/evidence query behavior, page/zoom/rotate/fullscreen controls, source links, OCR details, feedback, retry, and delete. Add the existing original-download URL as `下载原件`; keep the full viewer available. Use the exact fixed trust note and label automated content `自动整理结果`.

- [ ] **Step 4: Protect the existing viewer and danger flows**

Do not rewrite `viewer.css`; first make layout containment in shell/detail CSS. Add viewer CSS only for a demonstrated 390/768 overflow or focus defect and retain all pre-existing lines. Delete pages remain GET confirmation plus CSRF POST, state what remains safe before confirmation, and use shared danger controls.

- [ ] **Step 5: Restyle trends with text-equivalent data**

Use terracotta/sage high-contrast line/axes/points, keep the ordered text list as the complete non-color equivalent, preserve every evidence link, and replace experimental/diagnostic wording with the neutral copy required by the spec. The chart container owns narrow-screen overflow rather than the page.

- [ ] **Step 6: Verify GREEN**

Run: `python -m pytest tests/documents/test_detail_viewer.py tests/documents/test_deletion.py tests/labs tests/accessibility/test_detail_trend_markup.py -q`

Run: `node --test tests/js/*.test.mjs`

Expected: source positioning, viewer tools, downloads, feedback, deletion, and trend sources all pass.

### Task 8: “我的”, settings, privacy, and cautious operations

**Files:**
- Modify: `templates/patients/profile.html`
- Modify: `templates/patients/delete_account_confirm.html`
- Modify: `static/css/profile.css`
- Modify: `static/js/profile.js`
- Modify: `tests/patients/test_profile.py`
- Modify: `tests/accounts/test_account_deletion.py`
- Create: `tests/accessibility/test_profile_markup.py`

**Interfaces:**
- Consumes: current patient, preference, quota, notification, feedback, logout, and deletion context.
- Produces: identity/preferences/notification/privacy/logout sections plus isolated danger zone.

- [ ] **Step 1: Write failing profile information-architecture tests**

```python
def test_profile_contains_identity_preferences_privacy_logout_and_danger_zone(client, patient):
    content = client.get("/me/").content.decode()
    for label in ("当前健康档案", "用户偏好", "通知设置", "隐私与敏感信息", "退出登录"):
        assert label in content
    assert 'class="profile-danger-zone"' in content
    assert 'href="/me/delete-account/"' in content
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/patients/test_profile.py tests/accessibility/test_profile_markup.py -q`

Expected: FAIL on new section labels/logout placement.

- [ ] **Step 3: Recompose existing controls without changing form endpoints**

Create warm cards for current archive identity/name, display preferences/quota, browser notification settings, privacy/sensitive links, product feedback, and a POST logout control. Preserve every form name, action, CSRF token, status anchor, hidden notification field, and `data-notification-*` attribute.

- [ ] **Step 4: Isolate dangerous actions and verify confirmation semantics**

Use `.profile-danger-zone` with restrained high-contrast styling, separate explanatory copy, and the existing account-deletion confirmation link. The confirmation page remains server-rendered and explicitly says access stops immediately and deletion is irreversible.

- [ ] **Step 5: Verify GREEN**

Run: `python -m pytest tests/patients/test_profile.py tests/accounts/test_account_deletion.py tests/accessibility/test_profile_markup.py -q`

Expected: preferences, notification, feedback, logout, deletion, and semantics pass.

### Task 9: Responsive, accessibility, visual baselines, and complete regression

**Files:**
- Create: `tests/e2e/health-home-warm-ui.spec.ts`
- Create through Playwright update: `tests/e2e/health-home-warm-ui.spec.ts-snapshots/*`
- Modify: `playwright.config.ts`
- Modify: `tests/browser/test_ac00_ac01_browser.py`
- Modify carefully: `tests/browser/test_ac02_upload_browser.py`
- Modify: `docs/verification/browser-accessibility.md`
- Create: `docs/verification/health-home-warm-ui.md`

**Interfaces:**
- Consumes: all prior UI/auth tasks and deterministic seeded fixtures.
- Produces: Chrome/Edge visual baselines and runtime/accessibility evidence at 1440, 768, and 390 widths; WebKit remains compatibility reference.

- [ ] **Step 1: Add a deterministic runtime observer and viewport matrix**

```typescript
const viewports = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "tablet", width: 768, height: 1024 },
  { name: "mobile", width: 390, height: 844 },
];

async function expectNoPageOverflow(page: Page) {
  await expect.poll(() => page.evaluate(() =>
    document.documentElement.scrollWidth <= document.documentElement.clientWidth
  )).toBe(true);
}
```

- [ ] **Step 2: Cover all required pages and states**

Exercise login, first use, forgot/reset, onboarding, home, upload including partial failure, archive search/filter/empty, detail, original viewer, trend, tasks/status destination, notifications, profile, privacy/sensitive information, document/account delete confirmations, policy unavailable, and account-deleted. Fail on console errors, page errors, or unexpected 4xx/5xx responses.

- [ ] **Step 3: Capture approved visual baselines**

Use `expect(page).toHaveScreenshot()` for desktop and mobile home, archive, detail, upload, trend, login, first-use, and profile screens. Mask only dynamic timestamps/UUIDs; do not mask navigation, status, forms, primary actions, original/result ordering, or responsive layout. Generate separate Chrome and Edge baselines; run WebKit without accepting it as Safari evidence.

- [ ] **Step 4: Run contrast, keyboard, touch, and reduced-motion checks**

Assert one `h1`, visible skip target/focus ring, tab reachability of every main action, labels/error associations, live regions, 44px computed control bounds at 390px, status icon+text, no external font requests, and zero animation/transition duration under `prefers-reduced-motion: reduce` for nonessential motion.

- [ ] **Step 5: Run complete verification**

Run: `python -m pytest -q`

Run: `npm run test:js`

Run: `npm run test:e2e:chrome`

Run: `npm run test:e2e:edge`

Run: `npm run test:e2e:webkit-reference`

Run: `python manage.py makemigrations --check --dry-run`

Run: `python manage.py check`

Expected: all automated suites pass, no unexpected runtime failures occur, and visual diffs are empty after the intentional baseline creation.

- [ ] **Step 6: Record evidence and perform a dirty-worktree audit**

Record commands, versions, viewport/browser results, screenshot paths, and any justified skips without credentials or health data. Compare `git diff` against the initial dirty-file list and prove unrelated bootstrap, viewer, browser-test, and processing-worker changes were preserved.

## Plan self-review

- Spec sections 1–8 and 11–13 map to Tasks 1–9; authentication behavior is implemented by the companion authentication plan and its pages are visually completed in Task 3.
- Public/app shells, exact navigation, home, archive, upload, detail/viewer, trend, tasks discovery, notifications, profile, privacy, onboarding, dangerous confirmations, empty/error states, responsive/accessibility requirements, and screenshot baselines each have direct implementation and evidence steps.
- Shared components own global visual rules; page styles do not redefine tokens.
- Existing business URLs, form names, data attributes, permission scopes, and JavaScript contracts stay explicit in every migration task.
- The plan never treats the static prototype as production markup and never overwrites unrelated dirty work.
