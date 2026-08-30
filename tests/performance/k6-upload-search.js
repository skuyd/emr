import { check, fail, sleep } from "k6";
import http from "k6/http";
import { Rate, Trend } from "k6/metrics";

const sessionPath = __ENV.PHR_K6_SESSION_FILE || "";
const fixtureManifestPath = __ENV.PHR_K6_FIXTURE_MANIFEST || "";
const sessionData = sessionPath ? JSON.parse(open(sessionPath)) : { sessions: [] };
const manifest = fixtureManifestPath
  ? JSON.parse(open(fixtureManifestPath))
  : { synthetic_only: false, batches: [] };
const fixtureBatches = (manifest.batches || []).map((batch) => ({
  declaredPages: Number(batch.total_pages),
  files: (batch.files || []).map((file) => ({
    name: String(file.name || file.path.split(/[\\/]/).pop()),
    pages: Number(file.pages),
    data: open(file.path, "b"),
  })),
}));

const requestFailures = new Rate("request_failures");
const firstResultFailures = new Rate("first_result_failures");
const homeDuration = new Trend("home_duration", true);
const recordsDuration = new Trend("records_duration", true);
const searchDuration = new Trend("search_duration", true);
const viewerFirstPageDuration = new Trend("viewer_first_page_duration", true);
const uploadOriginalDuration = new Trend("upload_original_duration", true);
const firstResultDuration = new Trend("first_result_duration", true);

export const options = {
  discardResponseBodies: false,
  scenarios: {
    online_accounts: {
      executor: "constant-vus",
      exec: "browseArchive",
      vus: 100,
      duration: __ENV.PHR_K6_BROWSE_DURATION || "3m",
      gracefulStop: "15s",
    },
    concurrent_upload_batches: {
      executor: "per-vu-iterations",
      exec: "uploadBatch",
      vus: 10,
      iterations: 1,
      startTime: "10s",
      maxDuration: "7m",
    },
  },
  thresholds: {
    request_failures: ["rate==0"],
    first_result_failures: ["rate==0"],
    home_duration: ["p(95)<3000"],
    records_duration: ["p(95)<3000"],
    search_duration: ["p(95)<2000"],
    viewer_first_page_duration: ["p(95)<3000"],
    first_result_duration: ["p(95)<=300000", "max<=300000"],
  },
  summaryTrendStats: ["min", "med", "p(95)", "p(99)", "max"],
};

function requireCondition(value, message) {
  if (!value) fail(message);
}

function sessionForVu() {
  return sessionData.sessions[(__VU - 1) % sessionData.sessions.length];
}

function installSession(baseUrl, session) {
  http.cookieJar().set(baseUrl, "sessionid", session.sessionid, {
    path: "/",
    secure: true,
    http_only: true,
  });
}

function timedGet(url, metric, tags) {
  const response = http.get(url, { redirects: 0, tags });
  metric.add(response.timings.duration, tags);
  const ok = check(response, { [`${tags.operation} returned 200`]: (item) => item.status === 200 });
  requestFailures.add(!ok, tags);
  return response;
}

function csrfFrom(response) {
  return response.html().find("input[name=csrfmiddlewaretoken]").first().attr("value");
}

function recordFailedFirstResult(elapsed, tags) {
  firstResultDuration.add(Math.max(300001, elapsed), tags);
  firstResultFailures.add(1, tags);
}

export function setup() {
  requireCondition(/^https:\/\/[^/]+/.test(sessionData.base_url || ""), "base_url must use HTTPS");
  requireCondition(sessionData.synthetic_only === true, "session data must be explicitly synthetic");
  requireCondition(sessionData.sessions.length >= 100, "at least 100 independent sessions are required");
  requireCondition(
    sessionData.sessions.every((session) => session.sessionid && session.document_count >= 300),
    "every session must represent an account with at least 300 documents",
  );
  requireCondition(manifest.synthetic_only === true, "fixture manifest must be explicitly synthetic");
  requireCondition(fixtureBatches.length >= 10, "at least 10 independent upload batches are required");
  requireCondition(
    fixtureBatches.slice(0, 10).every((batch) =>
      batch.files.length >= 1 &&
      batch.files.length <= 20 &&
      batch.declaredPages === batch.files.reduce((sum, file) => sum + file.pages, 0) &&
      batch.declaredPages <= 60 &&
      batch.files.every((file) => file.data.byteLength > 0 && file.pages >= 1),
    ),
    "each batch must contain 1-20 non-empty files and no more than 60 declared pages",
  );
  const firstTen = fixtureBatches.slice(0, 10);
  const totalFiles = firstTen.reduce((sum, batch) => sum + batch.files.length, 0);
  const totalPages = firstTen.reduce((sum, batch) => sum + batch.declaredPages, 0);
  requireCondition(totalPages / totalFiles === 3, "the upload fixture set must average exactly 3 pages per document");
  requireCondition(firstTen.some((batch) => batch.declaredPages === 60), "one upload batch must exercise 60 pages");
  return { baseUrl: sessionData.base_url.replace(/\/$/, "") };
}

export function browseArchive(data) {
  const session = sessionForVu();
  installSession(data.baseUrl, session);
  timedGet(`${data.baseUrl}/`, homeDuration, { operation: "home" });
  timedGet(`${data.baseUrl}/records/`, recordsDuration, { operation: "records" });
  timedGet(
    `${data.baseUrl}/records/?q=${encodeURIComponent(session.query)}`,
    searchDuration,
    { operation: "search" },
  );
  timedGet(
    `${data.baseUrl}/records/${encodeURIComponent(session.viewer_document_id)}/pages/1/image/`,
    viewerFirstPageDuration,
    { operation: "viewer_first_page" },
  );
  sleep(1);
}

export function uploadBatch(data) {
  const session = sessionData.sessions[(__VU - 1) % 10];
  const fixtureBatch = fixtureBatches[(__VU - 1) % 10];
  const tags = { operation: "upload_batch", batch_slot: String((__VU - 1) % 10) };
  installSession(data.baseUrl, session);

  const uploadPage = timedGet(`${data.baseUrl}/uploads/new/`, homeDuration, { operation: "upload_page" });
  const csrf = csrfFrom(uploadPage);
  if (!csrf) {
    requestFailures.add(1, tags);
    recordFailedFirstResult(0, tags);
    return;
  }

  const candidateBody = JSON.stringify({
    files: fixtureBatch.files.map((file) => ({ name: file.name, byte_size: file.data.byteLength })),
  });
  const create = http.post(`${data.baseUrl}/api/upload-batches/`, candidateBody, {
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrf, Accept: "application/json" },
    redirects: 0,
    tags,
  });
  const createOk = check(create, { "batch created": (response) => response.status === 201 });
  requestFailures.add(!createOk, tags);
  if (!createOk) {
    recordFailedFirstResult(0, tags);
    return;
  }

  const created = create.json();
  const acceptedItems = (created.items || []).filter((item) => item.accepted === true);
  if (acceptedItems.length !== fixtureBatch.files.length) {
    requestFailures.add(1, tags);
    recordFailedFirstResult(0, tags);
    return;
  }

  for (let index = 0; index < acceptedItems.length; index += 1) {
    const item = acceptedItems[index];
    const fixture = fixtureBatch.files[index];
    const response = http.post(
      `${data.baseUrl}/api/upload-batches/${created.batch_id}/items/${item.item_id}/content/`,
      { file: http.file(fixture.data, fixture.name) },
      { headers: { "X-CSRFToken": csrf, Accept: "application/json" }, redirects: 0, tags },
    );
    uploadOriginalDuration.add(response.timings.duration, tags);
    const uploaded = check(response, {
      "original persisted": (result) => result.status === 200 && result.json("saved") === true,
    });
    requestFailures.add(!uploaded, tags);
    if (!uploaded) {
      recordFailedFirstResult(0, tags);
      return;
    }
  }

  const resultStartedAt = Date.now();
  while (Date.now() - resultStartedAt <= 300000) {
    const status = http.get(`${data.baseUrl}/api/upload-batches/${created.batch_id}/status/`, {
      headers: { Accept: "application/json" },
      redirects: 0,
      tags,
    });
    const statusOk = check(status, { "batch status available": (response) => response.status === 200 });
    requestFailures.add(!statusOk, tags);
    if (!statusOk) {
      recordFailedFirstResult(Date.now() - resultStartedAt, tags);
      return;
    }
    const states = status.json("items.#.status") || [];
    if (states.some((value) => ["ORGANIZED", "ORIGINAL_ONLY"].includes(value))) {
      firstResultDuration.add(Date.now() - resultStartedAt, tags);
      firstResultFailures.add(0, tags);
      return;
    }
    sleep(2);
  }
  recordFailedFirstResult(Date.now() - resultStartedAt, tags);
}
