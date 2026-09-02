import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.PHR_E2E_BASE_URL || "https://phr.invalid";
const authenticatedState = process.env.PHR_E2E_STORAGE_STATE;

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: [["line"], ["json", { outputFile: "test-results/results.json" }]],
  outputDir: "test-results/artifacts",
  snapshotPathTemplate: "{testDir}/{testFileDir}/{testFileName}-snapshots/{projectName}/{arg}{ext}",
  use: {
    baseURL,
    locale: "zh-CN",
    storageState: authenticatedState,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    ignoreHTTPSErrors: false,
  },
  projects: [
    {
      name: "chrome-current",
      use: { ...devices["Desktop Chrome"], channel: "chrome" },
    },
    {
      name: "edge-current",
      use: { ...devices["Desktop Edge"], channel: "msedge" },
    },
    {
      // This catches WebKit-engine regressions, but is never accepted as real
      // Safari evidence by the release gate.
      name: "webkit-reference",
      use: { ...devices["Desktop Safari"] },
    },
  ],
});
