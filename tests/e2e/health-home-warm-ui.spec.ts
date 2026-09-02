import { expect, Page, test } from "@playwright/test";

type Viewport = { name: "desktop" | "tablet" | "mobile"; width: number; height: number };
type RuntimeFailures = { console: string[]; page: string[]; responses: string[] };

const viewports: Viewport[] = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "tablet", width: 768, height: 1024 },
  { name: "mobile", width: 390, height: 844 },
];
const baseURL = process.env.PHR_E2E_BASE_URL;
const authenticatedState = process.env.PHR_E2E_STORAGE_STATE;
const onboardingState = process.env.PHR_E2E_ONBOARDING_STORAGE_STATE;
const uploadFixture = process.env.PHR_E2E_UPLOAD_FIXTURE;
const partialUploadSavedFixture = process.env.PHR_E2E_PARTIAL_UPLOAD_SAVED_FIXTURE;
const partialUploadFailedFixture = process.env.PHR_E2E_PARTIAL_UPLOAD_FAILED_FIXTURE;
const documentId = process.env.PHR_E2E_DOCUMENT_ID;
const trendCode = process.env.PHR_E2E_TREND_CODE;
const syntheticQuery = process.env.PHR_E2E_QUERY;
const emptyQuery = process.env.PHR_E2E_EMPTY_QUERY;
const archiveFilterType = process.env.PHR_E2E_FILTER_TYPE;
const archiveFilterStatus = process.env.PHR_E2E_FILTER_STATUS;
const archiveFilterYear = process.env.PHR_E2E_FILTER_YEAR;
const archiveFilterMonth = process.env.PHR_E2E_FILTER_MONTH;
const policyUnavailableURL = process.env.PHR_E2E_POLICY_UNAVAILABLE_URL;
const deleteAccountStorageState = process.env.PHR_E2E_DELETE_ACCOUNT_STORAGE_STATE;
const visualBaselines = process.env.PHR_E2E_VISUAL_BASELINES === "1";
const releaseMode = process.env.PHR_E2E_RELEASE === "1";

function requireReleaseInput(value: unknown, name: string): asserts value {
  if (!value && releaseMode) {
    throw new Error(`${name} is required when PHR_E2E_RELEASE=1; release browser checks must not be skipped`);
  }
  test.skip(!value, `${name} is unavailable; local contract run is explicitly skipped`);
}

function observeRuntime(page: Page, isExpectedResponse?: (response: { status(): number; url(): string }) => boolean): RuntimeFailures {
  const failures: RuntimeFailures = { console: [], page: [], responses: [] };
  page.on("console", (message) => {
    if (message.type() === "error") failures.console.push(message.text());
  });
  page.on("pageerror", (error) => failures.page.push(error.message));
  page.on("response", (response) => {
    if (response.status() >= 400 && !isExpectedResponse?.(response)) {
      failures.responses.push(`${response.status()} ${new URL(response.url()).pathname}`);
    }
  });
  return failures;
}

async function expectNoPageOverflow(page: Page) {
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth))
    .toBe(true);
}

async function expectPageBasics(page: Page, path: string) {
  const response = await page.goto(path, { waitUntil: "networkidle" });
  expect(response?.status()).toBe(200);
  expect(new URL(page.url()).pathname).toBe(new URL(path, page.url()).pathname);
  await expect(page.locator("main")).toBeVisible();
  await expect(page.locator("h1")).toHaveCount(1);
  await expectNoPageOverflow(page);
}

async function expectViewportMatrix(page: Page, path: string) {
  for (const viewport of viewports) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await expectPageBasics(page, path);
    await page.emulateMedia({ forcedColors: "active" });
    await expectNoPageOverflow(page);
    await page.emulateMedia({ reducedMotion: "reduce", forcedColors: "none" });
    const motion = await page.evaluate(() =>
      Array.from(document.querySelectorAll("*"), (element) => {
        const style = getComputedStyle(element);
        return Math.max(parseFloat(style.animationDuration) || 0, parseFloat(style.transitionDuration) || 0);
      }).some((duration) => duration > 0.05),
    );
    expect(motion).toBe(false);
    await page.emulateMedia({ reducedMotion: "no-preference", forcedColors: "none" });
  }
}

async function expectKeyboardFocus(page: Page) {
  await page.keyboard.press("Tab");
  await expect
    .poll(() => page.evaluate(() => {
      const active = document.activeElement as HTMLElement | null;
      if (!active || active === document.body) return false;
      const style = getComputedStyle(active);
      return style.outlineStyle !== "none" || style.boxShadow !== "none";
    }))
    .toBe(true);
}

async function expectFocusableMainControls(page: Page) {
  const unfocusable = await page.evaluate(() =>
    Array.from(document.querySelectorAll("main a, main button, main input, main select, main textarea"))
      .filter((element) => {
        const style = getComputedStyle(element);
        return style.display !== "none" && style.visibility !== "hidden" && !(element as HTMLInputElement).disabled;
      })
      .filter((element) => (element as HTMLElement).tabIndex < 0)
      .map((element) => element.outerHTML.slice(0, 120)),
  );
  expect(unfocusable).toEqual([]);
}

async function expectLabeledControls(page: Page) {
  const unlabeled = await page.evaluate(() =>
    Array.from(document.querySelectorAll("main input, main select, main textarea"))
      .filter((element) => {
        const input = element as HTMLInputElement;
        if (input.type === "hidden" || input.disabled) return false;
        if (element.getAttribute("aria-label") || element.getAttribute("aria-labelledby")) return false;
        const id = element.getAttribute("id");
        return !id || !Array.from(document.querySelectorAll("main label")).some((label) => label.htmlFor === id);
      })
      .map((element) => element.outerHTML.slice(0, 120)),
  );
  expect(unlabeled).toEqual([]);
}

async function expectErrorSummarySemantics(page: Page) {
  for (const summary of await page.locator('main [role="alert"]:visible').all()) {
    await expect(summary).toHaveAttribute("tabindex", "-1");
    const labelledBy = await summary.getAttribute("aria-labelledby");
    expect(labelledBy).toBeTruthy();
    await expect(page.locator(`#${labelledBy}`)).toHaveCount(1);
  }
}

async function expectTouchTargets(page: Page) {
  const controls = page.locator("main a:visible, main button:visible, main input:visible, main select:visible, main textarea:visible");
  for (const control of await controls.all()) {
    const box = await control.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.width).toBeGreaterThanOrEqual(44);
    expect(box!.height).toBeGreaterThanOrEqual(44);
  }
}

async function expectNoExternalFonts(page: Page) {
  const externalFontRequests = await page.evaluate(() =>
    performance.getEntriesByType("resource")
      .map((entry) => {
        const resource = entry as PerformanceResourceTiming;
        const url = new URL(resource.name, window.location.href);
        const fontExtension = /\.(?:woff2?|ttf|otf|eot)(?:$|\?)/i.test(url.pathname);
        const knownFontHost = /fonts\.(?:googleapis|gstatic)\.com|use\.typekit\.net/i.test(url.hostname);
        return resource.initiatorType === "font" || fontExtension || knownFontHost
          ? { href: url.href, external: url.origin !== window.location.origin }
          : null;
      })
      .filter((entry): entry is { href: string; external: boolean } => entry?.external === true)
      .map((entry) => entry.href),
  );
  expect(externalFontRequests).toEqual([]);
}

async function captureScreens(page: Page, path: string, name: string) {
  for (const viewport of viewports.filter(({ name: viewportName }) => viewportName === "desktop" || viewportName === "mobile")) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await expectPageBasics(page, path);
    await expect(page).toHaveScreenshot(`${name}-${viewport.name}.png`, {
      fullPage: true,
      animations: "disabled",
      mask: [page.locator("time"), page.locator("[data-dynamic]")],
    });
  }
}

test.describe("health-home-warm-ui anonymous pages", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("login, first use, reset, policy and deleted-account pages are responsive", async ({ page }) => {
    requireReleaseInput(baseURL, "PHR_E2E_BASE_URL");
    const expectedExpiredStages = new Set([
      "/login/first-use/verify/",
      "/login/first-use/password/",
      "/login/forgot-password/new-password/",
    ]);
    const failures = observeRuntime(page, (response) => (
      response.status() === 400 && expectedExpiredStages.has(new URL(response.url()).pathname)
    ));
    for (const path of ["/login/", "/login/first-use/", "/login/forgot-password/", "/login/forgot-password/verify/", "/privacy/", "/account-deleted/"]) {
      await expectViewportMatrix(page, path);
    }
    const resetPassword = await page.goto("/login/forgot-password/new-password/", { waitUntil: "networkidle" });
    expect(resetPassword?.status()).toBe(400);
    await expect(page.locator("main")).toBeVisible();
    await expect(page.locator("h1")).toHaveCount(1);
    await expectNoPageOverflow(page);
    await expectErrorSummarySemantics(page);
    await expectFocusableMainControls(page);
    for (const path of ["/login/first-use/verify/", "/login/first-use/password/"]) {
      const stage = await page.goto(path, { waitUntil: "networkidle" });
      expect(stage?.status()).toBe(400);
      await expect(page.locator("main")).toBeVisible();
      await expect(page.locator("h1")).toHaveCount(1);
      await expectErrorSummarySemantics(page);
      await expectNoPageOverflow(page);
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await expectPageBasics(page, "/login/");
    await expect(page.locator('label[for="id_phone"]')).toBeVisible();
    await expect(page.locator('label[for="id_password"]')).toBeVisible();
    await expect(page.locator("#id_phone")).toHaveAttribute("autocomplete", /tel/);
    await expect(page.locator("#id_password")).toHaveAttribute("autocomplete", "current-password");
    const passwordToggle = page.locator('button[data-password-toggle][aria-controls="id_password"]');
    await expect(passwordToggle).toHaveAttribute("type", "button");
    await expect(passwordToggle).toHaveAttribute("aria-pressed", "false");
    await passwordToggle.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator("#id_password")).toHaveAttribute("type", "text");
    await page.keyboard.press("Space");
    await expect(page.locator("#id_password")).toHaveAttribute("type", "password");
    await expectTouchTargets(page);
    await expectFocusableMainControls(page);
    await expectLabeledControls(page);
    await expectNoExternalFonts(page);
    await expectKeyboardFocus(page);
    expect(failures).toEqual({ console: [], page: [], responses: [] });
  });
});

test.describe("health-home-warm-ui authenticated pages", () => {
  test.beforeEach(() => {
    requireReleaseInput(baseURL, "PHR_E2E_BASE_URL");
    requireReleaseInput(authenticatedState, "PHR_E2E_STORAGE_STATE");
  });

  test("home archive profile privacy and account-delete confirmation keep IA and runtime clean", async ({ page }) => {
    requireReleaseInput(syntheticQuery, "PHR_E2E_QUERY");
    requireReleaseInput(emptyQuery, "PHR_E2E_EMPTY_QUERY");
    requireReleaseInput(archiveFilterType, "PHR_E2E_FILTER_TYPE");
    requireReleaseInput(archiveFilterStatus, "PHR_E2E_FILTER_STATUS");
    requireReleaseInput(archiveFilterYear, "PHR_E2E_FILTER_YEAR");
    requireReleaseInput(archiveFilterMonth, "PHR_E2E_FILTER_MONTH");
    const failures = observeRuntime(page);
    for (const path of ["/", "/records/", "/me/", "/onboarding/sensitive-information/", "/me/delete-account/"]) {
      await expectViewportMatrix(page, path);
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await expectPageBasics(page, "/me/");
    for (const heading of ["当前健康档案", "用户偏好", "通知设置", "隐私与敏感信息", "退出登录"]) {
      await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
    }
    await expect(page.locator(".profile-danger-zone")).toBeVisible();
    await expect(page.locator('form[method="post"][action="/logout/"]')).toHaveCount(2);
    const notificationToggle = page.locator("[data-notification-toggle]");
    await expect(notificationToggle).toHaveAttribute("aria-expanded", "false");
    await notificationToggle.click();
    await expect(notificationToggle).toHaveAttribute("aria-expanded", "true");
    await expect(page.locator("[data-notification-panel]")).toBeVisible();
    await expect(page.locator("[data-notification-toast]")).toHaveAttribute("aria-live", "polite");
    await notificationToggle.click();
    await expect(notificationToggle).toHaveAttribute("aria-expanded", "false");
    const tasksResponse = await page.goto("/tasks/", { waitUntil: "networkidle" });
    expect(tasksResponse?.status()).toBe(200);
    await expect(page).toHaveURL(/\/#home-tasks-title$/);
    await expect(page.locator("#home-tasks-title")).toBeVisible();
    const notifications = await page.request.get("/api/notifications/");
    expect(notifications.status()).toBe(200);
    expect(notifications.headers()["content-type"]).toContain("application/json");
    await page.goto("/records/", { waitUntil: "networkidle" });
    await page.locator("#records-query").fill(syntheticQuery!);
    await page.locator("#records-type").selectOption(archiveFilterType!);
    await page.locator("#records-status").selectOption(archiveFilterStatus!);
    await page.locator("#records-year").fill(archiveFilterYear!);
    await page.locator("#records-month").fill(archiveFilterMonth!);
    await page.locator("form[role=search] button[type=submit]").click();
    await page.waitForLoadState("networkidle");
    const filteredURL = new URL(page.url());
    expect(filteredURL.pathname).toBe("/records/");
    expect(Object.fromEntries(["q", "type", "status", "year", "month"].map((key) => [key, filteredURL.searchParams.get(key)]))).toEqual({
      q: syntheticQuery,
      type: archiveFilterType,
      status: archiveFilterStatus,
      year: archiveFilterYear,
      month: archiveFilterMonth,
    });
    await expect(page.locator(".records-result-count")).toBeVisible();
    expect(await page.locator(".record-card").count()).toBeGreaterThan(0);
    const nextPage = page.getByRole("link", { name: "下一页", exact: true });
    await expect(nextPage).toBeVisible();
    const nextURL = new URL((await nextPage.getAttribute("href"))!, page.url());
    for (const key of ["q", "type", "status", "year", "month"]) {
      expect(nextURL.searchParams.get(key)).toBe(filteredURL.searchParams.get(key));
    }
    expect(nextURL.searchParams.get("page")).toBe("2");
    await expect(page.locator(".records-clear")).toHaveAttribute("href", "/records/");
    await page.goto("/records/", { waitUntil: "networkidle" });
    await page.locator("#records-query").fill(emptyQuery!);
    await page.locator("form[role=search] button[type=submit]").click();
    await page.waitForLoadState("networkidle");
    expect(new URL(page.url()).searchParams.get("q")).toBe(emptyQuery);
    await expect(page.locator(".records-empty")).toContainText("没有找到相关资料，换个关键词试试。");
    await expect(page.locator(".records-result-count")).toHaveCount(0);
    await expect(page.getByRole("link", { name: "查看全部资料", exact: true })).toHaveAttribute("href", "/records/");
    const malformedDates = await page.goto(`/records/?q=${encodeURIComponent(syntheticQuery!)}&year=not-a-year&month=99`, { waitUntil: "networkidle" });
    expect(malformedDates?.status()).toBe(200);
    await expectNoPageOverflow(page);
    await expectTouchTargets(page);
    await expectFocusableMainControls(page);
    await expectLabeledControls(page);
    await expectNoExternalFonts(page);
    await expectKeyboardFocus(page);
    expect(failures).toEqual({ console: [], page: [], responses: [] });
  });

  test("upload preserves server-rendered status contracts", async ({ page }) => {
    requireReleaseInput(uploadFixture, "PHR_E2E_UPLOAD_FIXTURE");
    const failures = observeRuntime(page);
    await expectViewportMatrix(page, "/uploads/new/");
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator("#upload-file-input").setInputFiles(uploadFixture!);
    await expect(page.locator("[data-file-row]")).toHaveCount(1);
    await page.locator("[data-start-upload]").click();
    await expect(page.locator("[data-file-status]")).toHaveAttribute(
      "data-state",
      /PROCESSING|ORGANIZED|ORIGINAL_ONLY|PROCESSING_FAILED|UPLOAD_FAILED|EXACT_DUPLICATE/,
      { timeout: 60_000 },
    );
    await expect(page.locator("[data-file-status]")).not.toHaveText("");
    await expect(page.locator("[data-file-status] [data-status-label]")).not.toHaveText("");
    await expect(page.locator("[data-file-status] [data-status-icon]:not([hidden])")).toHaveCount(1);
    await expect(page.locator("[data-leave-notice]")).not.toHaveText("");
    await expect(page.locator("[data-live-region]")).toHaveAttribute("aria-live", "polite");
    await expectLabeledControls(page);
    await expectTouchTargets(page);
    expect(failures).toEqual({ console: [], page: [], responses: [] });
  });

  test("deterministic partial upload keeps per-file failure and saved-result semantics", async ({ page }) => {
    requireReleaseInput(partialUploadSavedFixture, "PHR_E2E_PARTIAL_UPLOAD_SAVED_FIXTURE");
    requireReleaseInput(partialUploadFailedFixture, "PHR_E2E_PARTIAL_UPLOAD_FAILED_FIXTURE");
    const failures = observeRuntime(page);
    await expectPageBasics(page, "/uploads/new/");
    await page.locator("#upload-file-input").setInputFiles([partialUploadSavedFixture!, partialUploadFailedFixture!]);
    await expect(page.locator("[data-file-row]")).toHaveCount(2);
    await page.locator("[data-start-upload]").click();
    await expect(page.locator('[data-file-status][data-state="UPLOAD_FAILED"]')).toBeVisible({ timeout: 60_000 });
    await expect(page.locator('[data-file-status][data-state="ORIGINAL_ONLY"], [data-file-status][data-state="ORGANIZED"]')).toBeVisible({ timeout: 60_000 });
    await expect(page.locator("[data-batch-summary]")).toBeVisible();
    await expect(page.locator('[data-file-row]:has([data-state="UPLOAD_FAILED"]) [data-file-result]')).toContainText("原件尚未保存");
    await expect(page.locator('[data-file-row]:has([data-state="UPLOAD_FAILED"]) [data-file-error]')).not.toHaveText("");
    expect(failures).toEqual({ console: [], page: [], responses: [] });
  });

  test("detail viewer and trends retain original/source actions", async ({ page }) => {
    requireReleaseInput(documentId, "PHR_E2E_DOCUMENT_ID");
    requireReleaseInput(trendCode, "PHR_E2E_TREND_CODE");
    const failures = observeRuntime(page);
    await expectViewportMatrix(page, `/records/${documentId}/`);
    await expect(page.getByRole("link", { name: "下载原件", exact: true })).toBeVisible();
    const viewer = await page.goto(`/records/${documentId}/viewer/`, { waitUntil: "networkidle" });
    expect(viewer?.status()).toBe(200);
    await expect(page.locator("[data-viewer]")).toBeVisible();
    const deleteConfirmation = await page.goto(`/records/${documentId}/delete/`, { waitUntil: "networkidle" });
    expect(deleteConfirmation?.status()).toBe(200);
    await expect(page.locator('form[method="post"] button[type="submit"]')).toBeVisible();
    await expectViewportMatrix(page, `/trends/${encodeURIComponent(trendCode!)}/`);
    await expect(page.locator(".trend-points")).toBeVisible();
    await expectNoExternalFonts(page);
    expect(failures).toEqual({ console: [], page: [], responses: [] });
  });
});

test.describe("health-home-warm-ui onboarding fixture", () => {
  test.use({ storageState: onboardingState });

  test("onboarding consent form keeps labels, policy links and server submission", async ({ page }) => {
    requireReleaseInput(baseURL, "PHR_E2E_BASE_URL");
    requireReleaseInput(onboardingState, "PHR_E2E_ONBOARDING_STORAGE_STATE");
    const failures = observeRuntime(page);
    await expectViewportMatrix(page, "/onboarding/");
    await expect(page.locator("#id_display_name")).toBeVisible();
    for (const field of ["privacy", "sensitive_data", "upload_authority"]) {
      await expect(page.locator(`#id_${field}`)).toBeVisible();
      await expect(page.locator(`label[for="id_${field}"]`)).toBeVisible();
    }
    await expect(page.locator('form[method="post"] input[name="csrfmiddlewaretoken"]')).toHaveCount(1);
    await expect(page.locator('a[href="/privacy/"]')).toBeVisible();
    await expect(page.locator('a[href="/onboarding/sensitive-information/"]')).toBeVisible();
    await expectLabeledControls(page);
    await expectFocusableMainControls(page);
    await expectTouchTargets(page);
    expect(failures).toEqual({ console: [], page: [], responses: [] });
  });
});

test.describe("health-home-warm-ui policy failure", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("policy-unavailable fixture is a generic 503 without public footer", async ({ page }) => {
    requireReleaseInput(baseURL, "PHR_E2E_BASE_URL");
    requireReleaseInput(policyUnavailableURL, "PHR_E2E_POLICY_UNAVAILABLE_URL");
    const targetURL = new URL(policyUnavailableURL!, baseURL!).href;
    const failures = observeRuntime(page, (response) => response.status() === 503 && new URL(response.url()).href === targetURL);
    const response = await page.goto(targetURL, { waitUntil: "networkidle" });
    expect(response?.status()).toBe(503);
    await expect(page.locator("h1")).toHaveText("服务暂时不可用");
    await expect(page.locator("footer")).toHaveCount(0);
    await expect(page.locator("main")).toBeVisible();
    await expectNoPageOverflow(page);
    expect(failures).toEqual({ console: [], page: [], responses: [] });
  });
});

test.describe("health-home-warm-ui disposable account deletion", () => {
  test("POST confirmation stops access and lands on neutral account-deleted page", async ({ browser }) => {
    requireReleaseInput(baseURL, "PHR_E2E_BASE_URL");
    requireReleaseInput(deleteAccountStorageState, "PHR_E2E_DELETE_ACCOUNT_STORAGE_STATE");
    const context = await browser.newContext({ baseURL: baseURL!, storageState: deleteAccountStorageState!, locale: "zh-CN" });
    const page = await context.newPage();
    const failures = observeRuntime(page);
    const confirmation = await page.goto("/me/delete-account/", { waitUntil: "networkidle" });
    expect(confirmation?.status()).toBe(200);
    await expect(page.locator("#account-delete-warning")).toContainText("立即退出并停止");
    await expect(page.locator(".account-delete-card")).toContainText("不可逆且不可恢复");
    await expect(page.locator('form[method="post"] input[name="confirmation"]')).toHaveValue("delete-account");
    await page.locator('form[method="post"] button[type="submit"]').click();
    await expect(page).toHaveURL(/\/account-deleted\/$/);
    await expect(page.locator("h1")).toHaveText("账号访问已停止");
    const protectedResponse = await context.request.get("/", { maxRedirects: 0 });
    expect([302, 303]).toContain(protectedResponse.status());
    expect(protectedResponse.headers().location).toMatch(/\/login\//);
    expect(failures).toEqual({ console: [], page: [], responses: [] });
    await context.close();
  });
});

test.describe("health-home-warm-ui visual baselines", () => {
  test.skip(
    !visualBaselines && !releaseMode,
    "Set PHR_E2E_VISUAL_BASELINES=1 only when deterministic release fixtures are available",
  );
  test.beforeEach(() => {
    requireReleaseInput(baseURL, "PHR_E2E_BASE_URL");
    if (releaseMode && !visualBaselines) {
      throw new Error("PHR_E2E_VISUAL_BASELINES=1 is required for release visual checks");
    }
  });

  test.describe("public visual baselines", () => {
    test.use({ storageState: { cookies: [], origins: [] } });

    test("public visual baselines", async ({ page }) => {
      await captureScreens(page, "/login/", "login");
      await captureScreens(page, "/login/first-use/", "first-use");
    });
  });

  test.describe("authenticated visual baselines", () => {
    test.beforeEach(() => {
      requireReleaseInput(authenticatedState, "PHR_E2E_STORAGE_STATE");
    });

    test("core visual baselines", async ({ page }) => {
      await captureScreens(page, "/", "home");
      await captureScreens(page, "/records/", "archive");
      await captureScreens(page, "/uploads/new/", "upload");
      await captureScreens(page, "/me/", "profile");
      requireReleaseInput(documentId, "PHR_E2E_DOCUMENT_ID");
      requireReleaseInput(trendCode, "PHR_E2E_TREND_CODE");
      await captureScreens(page, `/records/${documentId}/`, "detail");
      await captureScreens(page, `/trends/${encodeURIComponent(trendCode!)}/`, "trend");
    });
  });
});
