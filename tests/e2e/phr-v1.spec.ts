import { expect, Page, test } from "@playwright/test";

const uploadFixture = process.env.PHR_E2E_UPLOAD_FIXTURE;
const documentId = process.env.PHR_E2E_DOCUMENT_ID;
const deleteDocumentId = process.env.PHR_E2E_DELETE_DOCUMENT_ID;
const trendCode = process.env.PHR_E2E_TREND_CODE;
const syntheticQuery = process.env.PHR_E2E_QUERY || "synthetic-performance-token";

type RuntimeFailures = {
  console: string[];
  page: string[];
  responses: string[];
};

function requireReleaseInput(value: unknown, name: string): asserts value {
  if (!value) throw new Error(`${name} is required; release browser checks must not be skipped`);
}

function observeRuntime(page: Page): RuntimeFailures {
  const failures: RuntimeFailures = { console: [], page: [], responses: [] };
  page.on("console", (message) => {
    if (message.type() === "error") failures.console.push(message.text());
  });
  page.on("pageerror", (error) => failures.page.push(error.message));
  page.on("response", (response) => {
    if (response.status() >= 400 && response.request().resourceType() !== "favicon") {
      failures.responses.push(`${response.status()} ${new URL(response.url()).pathname}`);
    }
  });
  return failures;
}

async function assertNoHorizontalOverflow(page: Page) {
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= document.documentElement.clientWidth,
      ),
    )
    .toBe(true);
}

async function assertPageBasics(page: Page, path: string) {
  const response = await page.goto(path, { waitUntil: "networkidle" });
  expect(response?.status()).toBe(200);
  await expect(page.locator("main")).toBeVisible();
  await expect(page.locator("h1")).toHaveCount(1);
  await assertNoHorizontalOverflow(page);
}

async function assertTwoViewports(page: Page, path: string) {
  for (const viewport of [
    { width: 1280, height: 720 },
    { width: 1440, height: 900 },
  ]) {
    await page.setViewportSize(viewport);
    await assertPageBasics(page, path);
  }
}

async function assertKeyboardFocus(page: Page) {
  await page.keyboard.press("Tab");
  await expect
    .poll(() =>
      page.evaluate(() => {
        const active = document.activeElement as HTMLElement | null;
        if (!active || active === document.body) return false;
        const style = getComputedStyle(active);
        return style.outlineStyle !== "none" || style.boxShadow !== "none";
      }),
    )
    .toBe(true);
}

test.describe("P00 anonymous entry", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("login and privacy pages are usable without an account", async ({ page }) => {
    requireReleaseInput(process.env.PHR_E2E_BASE_URL, "PHR_E2E_BASE_URL");
    const failures = observeRuntime(page);
    await page.setViewportSize({ width: 1280, height: 720 });
    const login = await page.goto("/login/", { waitUntil: "networkidle" });
    expect(login?.status()).toBe(200);
    await expect(page.locator("#login-form")).toBeVisible();
    await expect(page.locator("#id_phone")).toHaveAttribute("autocomplete", /tel/);
    await expect(page.locator("label[for=id_phone]")).toBeVisible();
    await expect(page.locator("label[for=id_code]")).toBeVisible();
    await assertKeyboardFocus(page);
    await assertNoHorizontalOverflow(page);
    const privacy = await page.goto("/privacy/", { waitUntil: "networkidle" });
    expect(privacy?.status()).toBe(200);
    await expect(page.locator("h1")).toHaveCount(1);
    expect(failures).toEqual({ console: [], page: [], responses: [] });
  });
});

test.describe("P02-P08 authenticated core flow", () => {
  test.beforeEach(() => {
    requireReleaseInput(process.env.PHR_E2E_BASE_URL, "PHR_E2E_BASE_URL");
    requireReleaseInput(process.env.PHR_E2E_STORAGE_STATE, "PHR_E2E_STORAGE_STATE");
  });

  test("home, archive and profile pass viewport, landmark and keyboard checks", async ({ page }) => {
    const failures = observeRuntime(page);
    for (const path of ["/", "/records/", "/me/"]) {
      await assertTwoViewports(page, path);
      await expect(page.locator("nav[aria-label]")).toBeVisible();
      await assertKeyboardFocus(page);
    }
    expect(failures).toEqual({ console: [], page: [], responses: [] });
  });

  test("home and archive become interactive within the PRD limit", async ({ page }, testInfo) => {
    const measurements: Record<string, number> = {};
    for (const path of ["/", "/records/"]) {
      await page.goto(path, { waitUntil: "networkidle" });
      const interactiveMilliseconds = await page.evaluate(() => {
        const navigation = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming;
        return navigation.domInteractive - navigation.startTime;
      });
      measurements[path] = interactiveMilliseconds;
      expect(interactiveMilliseconds).toBeLessThanOrEqual(3000);
    }
    await testInfo.attach("interactive-timing.json", {
      body: JSON.stringify(measurements, null, 2),
      contentType: "application/json",
    });
  });

  test("P03 accepts a synthetic fixture and preserves explicit task status", async ({ page }) => {
    requireReleaseInput(uploadFixture, "PHR_E2E_UPLOAD_FIXTURE");
    const failures = observeRuntime(page);
    await assertPageBasics(page, "/uploads/new/");
    await page.locator("#upload-file-input").setInputFiles(uploadFixture!);
    await expect(page.locator("[data-file-row]")).toHaveCount(1);
    await page.locator("[data-start-upload]").click();
    const status = page.locator("[data-file-status]");
    await expect(status).toHaveAttribute(
      "data-state",
      /PROCESSING|ORGANIZED|ORIGINAL_ONLY|PROCESSING_FAILED|EXACT_DUPLICATE/,
      { timeout: 60_000 },
    );
    await expect(status).not.toHaveText("");
    await expect(page.locator("[data-leave-notice]")).not.toHaveText("");
    expect(failures).toEqual({ console: [], page: [], responses: [] });
  });

  test("P04 search is keyboard operable and returns an explicit result or empty state", async ({ page }) => {
    const failures = observeRuntime(page);
    await assertPageBasics(page, "/records/");
    await page.locator("#records-query").fill(syntheticQuery);
    await page.locator("form[role=search] button[type=submit]").click();
    await page.waitForLoadState("networkidle");
    expect(new URL(page.url()).searchParams.get("q")).toBe(syntheticQuery);
    await expect(page.locator(".records-result-count, .records-empty")).toHaveCount(1);
    await assertNoHorizontalOverflow(page);
    expect(failures).toEqual({ console: [], page: [], responses: [] });
  });

  test("P05-P06 detail and original viewer load the first page", async ({ page }) => {
    requireReleaseInput(documentId, "PHR_E2E_DOCUMENT_ID");
    const failures = observeRuntime(page);
    await assertTwoViewports(page, `/records/${documentId}/`);
    const viewer = await page.goto(`/records/${documentId}/viewer/`, { waitUntil: "networkidle" });
    expect(viewer?.status()).toBe(200);
    await expect(page.locator("[data-viewer]")).toBeVisible();
    const imageResponse = await page.request.get(`/records/${documentId}/pages/1/image/`);
    expect(imageResponse.status()).toBe(200);
    expect(imageResponse.headers()["cache-control"]).toContain("no-store");
    expect(failures).toEqual({ console: [], page: [], responses: [] });
  });

  test("P07 trend page remains descriptive without relying on color", async ({ page }) => {
    requireReleaseInput(trendCode, "PHR_E2E_TREND_CODE");
    const failures = observeRuntime(page);
    await assertTwoViewports(page, `/trends/${encodeURIComponent(trendCode!)}/`);
    await expect(page.locator(".trend-chart svg")).toHaveCount(1);
    await expect(page.locator(".trend-points")).toHaveCount(1);
    expect(failures).toEqual({ console: [], page: [], responses: [] });
  });

  test("synthetic deletion requires its confirmation screen and removes access", async ({ page }) => {
    requireReleaseInput(deleteDocumentId, "PHR_E2E_DELETE_DOCUMENT_ID");
    const failures = observeRuntime(page);
    const confirmation = await page.goto(`/records/${deleteDocumentId}/delete/`, {
      waitUntil: "networkidle",
    });
    expect(confirmation?.status()).toBe(200);
    await expect(page.locator("form[method=post]")).toBeVisible();
    await page.locator("form[method=post] button[type=submit]").click();
    await page.waitForURL(/\/records\//);
    const removed = await page.request.get(`/records/${deleteDocumentId}/`);
    expect(removed.status()).toBe(404);
    expect(failures).toEqual({ console: [], page: [], responses: [] });
  });
});

test.describe("P01 consent gate", () => {
  test.use({ storageState: process.env.PHR_E2E_ONBOARDING_STORAGE_STATE });

  test("new synthetic account cannot enter without all three current consents", async ({ page }) => {
    requireReleaseInput(process.env.PHR_E2E_BASE_URL, "PHR_E2E_BASE_URL");
    requireReleaseInput(
      process.env.PHR_E2E_ONBOARDING_STORAGE_STATE,
      "PHR_E2E_ONBOARDING_STORAGE_STATE",
    );
    const failures = observeRuntime(page);
    await assertPageBasics(page, "/onboarding/");
    await expect(page.locator("#id_display_name")).toBeVisible();
    for (const field of ["privacy", "sensitive_data", "upload_authority"]) {
      await expect(page.locator(`#id_${field}`)).toBeVisible();
    }
    await page.locator("form button[type=submit]").click();
    await expect(page).toHaveURL(/\/onboarding\//);
    expect(failures).toEqual({ console: [], page: [], responses: [] });
  });
});
