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
const documentId = process.env.PHR_E2E_DOCUMENT_ID;
const trendCode = process.env.PHR_E2E_TREND_CODE;
const syntheticQuery = process.env.PHR_E2E_QUERY;
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
      .map((entry) => entry.name)
      .filter((name) => /fonts\.(googleapis|gstatic)\.com|use\.typekit\.net/i.test(name)),
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
    const failures = observeRuntime(page, (response) => (
      response.status() === 400
      && new URL(response.url()).pathname === "/login/forgot-password/new-password/"
    ));
    for (const path of ["/login/", "/login/first-use/", "/login/forgot-password/", "/login/forgot-password/verify/", "/privacy/", "/onboarding/sensitive-information/", "/account-deleted/"]) {
      await expectViewportMatrix(page, path);
    }
    const resetPassword = await page.goto("/login/forgot-password/new-password/", { waitUntil: "networkidle" });
    expect(resetPassword?.status()).toBe(400);
    await expect(page.locator("main")).toBeVisible();
    await expect(page.locator("h1")).toHaveCount(1);
    await expectNoPageOverflow(page);
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
    const tasksResponse = await page.goto("/tasks/", { waitUntil: "networkidle" });
    expect(tasksResponse?.status()).toBe(200);
    await expect(page).toHaveURL(/\/#home-tasks-title$/);
    await expect(page.locator("#home-tasks-title")).toBeVisible();
    const notifications = await page.request.get("/api/notifications/");
    expect(notifications.status()).toBe(200);
    expect(notifications.headers()["content-type"]).toContain("application/json");
    await page.goto("/records/", { waitUntil: "networkidle" });
    await page.locator("#records-query").fill(syntheticQuery!);
    const typeValue = await page.locator("#records-type option").evaluateAll((options) => options.map((option) => (option as HTMLOptionElement).value).find(Boolean));
    if (typeValue) await page.locator("#records-type").selectOption(typeValue);
    const statusValue = await page.locator("#records-status option").evaluateAll((options) => options.map((option) => (option as HTMLOptionElement).value).find(Boolean));
    if (statusValue) await page.locator("#records-status").selectOption(statusValue);
    await page.locator("form[role=search] button[type=submit]").click();
    await page.waitForLoadState("networkidle");
    expect(new URL(page.url()).searchParams.get("q")).toBe(syntheticQuery);
    await expect(page.locator(".records-result-count, .records-empty")).toHaveCount(1);
    const malformedDates = await page.goto(`/records/?q=${encodeURIComponent(syntheticQuery!)}&year=not-a-year&month=99`, { waitUntil: "networkidle" });
    expect(malformedDates?.status()).toBe(200);
    await expectNoPageOverflow(page);
    await expectTouchTargets(page);
    await expectNoExternalFonts(page);
    await expectKeyboardFocus(page);
    expect(failures).toEqual({ console: [], page: [], responses: [] });
  });

  test("upload and search preserve server-rendered status contracts", async ({ page }) => {
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
    await expect(page.locator("[data-leave-notice]")).not.toHaveText("");
    await expectTouchTargets(page);
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

test.describe("health-home-warm-ui visual baselines", () => {
  test.skip(!visualBaselines, "Set PHR_E2E_VISUAL_BASELINES=1 only when deterministic release fixtures are available");
  test.beforeEach(() => {
    requireReleaseInput(baseURL, "PHR_E2E_BASE_URL");
  });

  test("public visual baselines", async ({ page }) => {
    await captureScreens(page, "/login/", "login");
    await captureScreens(page, "/login/first-use/", "first-use");
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
