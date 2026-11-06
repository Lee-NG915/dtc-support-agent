#!/usr/bin/env node
/**
 * Isolated software E2E driver for the after-sales acceptance groups.
 *
 * This file is a fixed scenario script, not a test framework or page-object
 * system. It launches a fresh Playwright Chromium (never a user profile),
 * talks only to the two owned loopback origins, drives the real consumer and
 * staff pages, and writes a private bounded report plus screenshots.
 *
 * Usage:
 *   node frontend/scripts/after-sales-browser.mjs <config.json> <report.json>
 */

import { createHash } from "node:crypto";
import {
  chmodSync,
  mkdirSync,
  readFileSync,
  statSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";

import { chromium } from "playwright";

const configPath = process.argv[2];
const reportPath = process.argv[3];
if (!configPath || !reportPath) {
  throw new Error("usage: after-sales-browser.mjs <config.json> <report.json>");
}
const config = JSON.parse(readFileSync(configPath, "utf8"));
if (typeof config.browserLifecyclePath !== "string" || !config.browserLifecyclePath) {
  throw new Error("the browser lifecycle sidecar path is required");
}
const expected = config.expected;
const runId = config.runId;

const secretValues = [
  ...Object.values(config.staff ?? {}),
  config.epigServiceToken,
].filter((value) => typeof value === "string" && value.length > 0);

function redact(value) {
  let text = String(value ?? "");
  for (const secret of secretValues) text = text.split(secret).join("[redacted]");
  return text
    .replace(/postgres(?:ql)?(?:\+asyncpg)?:\/\/[^\s"']+/gi, "[redacted-database-url]")
    .replace(/Bearer\s+[A-Za-z0-9._~-]+/gi, "Bearer [redacted]")
    .replace(/dtc_staff_[A-Za-z0-9._~-]+/g, "[redacted-staff-token]");
}

function sha256(value) {
  return createHash("sha256").update(String(value ?? "")).digest("hex");
}

function nowIso() {
  return new Date().toISOString();
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function asJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function pathOf(url) {
  try {
    return new URL(url).pathname;
  } catch {
    return String(url);
  }
}

const report = {
  schemaVersion: 1,
  status: "running",
  startedAt: nowIso(),
  runId,
  browser: null,
  groups: [
    { id: "group1", title: "consumer request -> staff materials/draft -> approver -> worker -> unique reply", status: "not_run", actions: [], assertions: [], codes: {}, ids: {}, counts: {}, failures: [], screenshots: [] },
    { id: "group2", title: "consumer isolation + committed-response-loss recovery", status: "not_run", actions: [], assertions: [], codes: {}, ids: {}, counts: {}, failures: [], screenshots: [] },
    { id: "group3", title: "staff 409, logout/role isolation, AI-unavailable fallback", status: "not_run", actions: [], assertions: [], codes: {}, ids: {}, counts: {}, failures: [], screenshots: [] },
    { id: "group4", title: "desktop/320px render, keyboard, error/empty/loading states", status: "not_run", actions: [], assertions: [], codes: {}, ids: {}, counts: {}, failures: [], screenshots: [] },
  ],
  ids: {},
  counts: {},
  errors: [],
  routeErrors: [],
  blockedExternalRequests: [],
  pageErrors: [],
  consoleErrors: [],
  screenshots: [],
  limits: {
    a11y: "keyboard Tab reachability only; not a full WCAG audit",
    mobile: "320px render and horizontal-overflow check; not a device-lab result",
    visual: "screenshots preserved for Codex manual review; DOM assertions are not a visual pass",
    desktop_plugin: "not verified; independent software E2E surface only",
  },
};

function group(id) {
  const found = report.groups.find((item) => item.id === id);
  if (!found) throw new Error(`unknown group ${id}`);
  return found;
}

function action(groupId, text) {
  group(groupId).actions.push({ at: nowIso(), text: redact(text) });
}

function assertion(groupId, ok, message) {
  const entry = { ok: Boolean(ok), message: redact(message) };
  group(groupId).assertions.push(entry);
  if (!ok) throw new Error(redact(message));
  return true;
}

function softFailure(groupId, message) {
  group(groupId).failures.push(redact(message));
}

function recordCode(groupId, name, value) {
  group(groupId).codes[name] = value;
}

function recordId(groupId, name, value) {
  group(groupId).ids[name] = value;
  report.ids[name] = value;
}

function recordCount(groupId, name, value) {
  group(groupId).counts[name] = value;
  report.counts[name] = value;
}

function setGroupStatus(groupId, status) {
  group(groupId).status = status;
}

const ownedOrigins = new Set([
  new URL(config.dtcBaseUrl).origin,
  new URL(config.epigBrowserOrigin).origin,
]);

const browserLifecycle = {
  closeAttempted: false,
  closeOk: false,
  connectedAfterClose: null,
  error: null,
  contextsCreated: 0,
};

let suiteClosing = false;

function routeErrorIsCleanupNoise(route, contextClosed) {
  if (suiteClosing || contextClosed) return true;
  try {
    const page = route.request().frame()?.page();
    return page ? page.isClosed() : false;
  } catch {
    return false;
  }
}

function recordRouteError(route, url, action, error, contextClosed, label) {
  if (routeErrorIsCleanupNoise(route, contextClosed)) return;
  report.routeErrors.push({
    page: label,
    origin: url.origin,
    pathname: url.pathname,
    action,
    errorType: error?.name || "Error",
  });
}

async function continueRoute(route, url, contextClosed, label) {
  try {
    await route.continue();
  } catch (error) {
    recordRouteError(route, url, "continue", error, contextClosed, label);
  }
}

async function abortRoute(route, url, contextClosed, label) {
  try {
    await route.abort();
  } catch (error) {
    recordRouteError(route, url, "abort", error, contextClosed, label);
  }
}

function attachPage(page, label) {
  page.on("pageerror", (error) => {
    report.pageErrors.push({ page: label, message: redact(error?.message ?? error) });
  });
  page.on("console", (message) => {
    if (message.type() === "error") {
      report.consoleErrors.push({ page: label, message: redact(message.text()) });
    }
  });
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (!ownedOrigins.has(url.origin)) return;
    const kind = requestKind(request);
    if (!kind) return;
    const headers = request.headers();
    const body = request.postData();
    const bodyJson = asJson(body);
    request.__dtcCapture = {
      kind,
      method: request.method(),
      path: url.pathname,
      idempotencyKeyHash: headers["idempotency-key"]
        ? sha256(headers["idempotency-key"])
        : null,
      bodyHash: body ? sha256(body) : null,
      orderId: bodyJson && typeof bodyJson.order_id === "string" ? bodyJson.order_id : null,
      description:
        bodyJson && typeof bodyJson.description === "string" ? bodyJson.description : null,
      body: body ?? null,
      at: nowIso(),
    };
    page.__dtcRequests ??= [];
    page.__dtcRequests.push(request.__dtcCapture);
  });
  page.on("response", (response) => {
    const request = response.request();
    if (!ownedOrigins.has(new URL(request.url()).origin)) return;
    const kind = requestKind(request);
    if (!kind) return;
    const capture = request.__dtcCapture;
    if (capture) {
      capture.status = response.status();
      capture.responseAt = nowIso();
      if (
        ["create_request", "save_draft", "submit_draft", "approve", "add_material", "suggest"].includes(
          kind,
        )
      ) {
        void response
          .json()
          .then((body) => {
            if (body && typeof body === "object") {
              if (typeof body.error_code === "string") capture.errorCode = body.error_code;
              if (typeof body.message === "string") capture.errorMessage = redact(body.message);
            }
          })
          .catch(() => undefined);
      }
    }
    page.__dtcResponses ??= [];
    page.__dtcResponses.push({
      kind,
      method: request.method(),
      path: pathOf(request.url()),
      status: response.status(),
      at: nowIso(),
    });
  });
}

function requestKind(request) {
  const pathname = pathOf(request.url());
  if (!pathname.includes("/after-sales/")) return null;
  if (pathname.endsWith("/requests")) return "create_request";
  if (pathname.endsWith("/attachments")) return "upload_attachment";
  if (pathname.endsWith("/suggestion")) return "suggest";
  if (pathname.endsWith("/submit")) return "submit_draft";
  if (pathname.endsWith("/approve")) return "approve";
  if (pathname.endsWith("/materials")) return "add_material";
  if (pathname.endsWith("/drafts")) return "save_draft";
  return null;
}

function captures(page, kind) {
  return (page.__dtcRequests ?? []).filter((item) => item.kind === kind);
}

async function createContext(browser, viewport, label, { delayCaseList = false } = {}) {
  browserLifecycle.contextsCreated += 1;
  const context = await browser.newContext({
    viewport,
    locale: "en-AU",
    colorScheme: "light",
    serviceWorkers: "block",
  });
  let contextClosed = false;
  context.on("close", () => {
    contextClosed = true;
  });
  await context.route("**/*", async (route) => {
    const request = route.request();
    const raw = request.url();
    if (raw.startsWith("data:") || raw.startsWith("blob:") || raw.startsWith("about:")) {
      await continueRoute(
        route,
        { origin: raw.split(":", 1)[0], pathname: "" },
        contextClosed,
        label,
      );
      return;
    }
    let url;
    try {
      url = new URL(raw);
    } catch {
      await abortRoute(route, { origin: "invalid", pathname: "" }, contextClosed, label);
      return;
    }
    if (ownedOrigins.has(url.origin)) {
      if (
        delayCaseList &&
        url.pathname.startsWith("/api/v1/after-sales/au/cases")
      ) {
        await sleep(1600);
      }
      await continueRoute(route, url, contextClosed, label);
      return;
    }
    report.blockedExternalRequests.push({
      page: label,
      origin: url.origin,
      path: url.pathname,
      resourceType: request.resourceType(),
      at: nowIso(),
    });
    await abortRoute(route, url, contextClosed, label);
  });
  return context;
}

const openPages = [];

async function newPage(context, label) {
  const page = await context.newPage();
  page.__dtcLabel = label;
  page.setDefaultTimeout(45000);
  page.setDefaultNavigationTimeout(120000);
  attachPage(page, label);
  openPages.push(page);
  return page;
}

async function captureFailureDiagnostics(activeGroup) {
  report.failureDiagnostics = [];
  for (const page of openPages) {
    if (page.isClosed()) continue;
    const label = page.__dtcLabel ?? "unknown";
    try {
      const file = path.join(
        config.screenshotsDirectory,
        `failure-${activeGroup ?? "unknown"}-${label.replace(/[^A-Za-z0-9_.-]/g, "_")}.png`,
      );
      await page.screenshot({ path: file, fullPage: true });
      chmodSync(file, 0o600);
      report.screenshots.push({ group: activeGroup, name: "failure", path: file });
      const bodyText = await page.locator("body").innerText().catch(() => "");
      report.failureDiagnostics.push({
        group: activeGroup,
        page: label,
        url: page.url(),
        path: file,
        bodyText: redact(bodyText.slice(0, 3000)),
      });
    } catch (error) {
      report.failureDiagnostics.push({
        group: activeGroup,
        page: label,
        error: redact(error?.message ?? error),
      });
    }
  }
}

async function screenshot(page, groupId, name, viewport) {
  const targetDir = config.screenshotsDirectory;
  mkdirSync(targetDir, { recursive: true, mode: 0o700 });
  const previous = page.viewportSize();
  await page.setViewportSize(viewport);
  await page.waitForTimeout(250);
  const file = path.join(targetDir, `${groupId}-${name}-${viewport.width}.png`);
  await page.screenshot({ path: file, fullPage: true });
  chmodSync(file, 0o600);
  const entry = { group: groupId, name, viewport: viewport.width, path: file };
  report.screenshots.push(entry);
  group(groupId).screenshots.push(entry);
  if (previous) await page.setViewportSize(previous);
  await page.waitForTimeout(100);
  return file;
}

async function overflowMetrics(page) {
  return page.evaluate(() => {
    const root = document.documentElement;
    const body = document.body;
    const inner = [...document.querySelectorAll(".staff-table-wrap, .staff-approval-tabs")]
      .map((element) => ({
        className: element.className,
        clientWidth: element.clientWidth,
        scrollWidth: element.scrollWidth,
        innerScroll: element.scrollWidth > element.clientWidth,
      }));
    const offenders = [...document.querySelectorAll("body *")]
      .map((element) => {
        const rect = element.getBoundingClientRect();
        return {
          tag: element.tagName,
          className: String(element.className || "").slice(0, 120),
          width: Math.round(rect.width),
          right: Math.round(rect.right),
          scrollWidth: element.scrollWidth,
        };
      })
      .filter((item) => item.right > root.clientWidth + 1 && item.width > 0)
      .sort((left, right) => right.right - left.right)
      .slice(0, 8);
    return {
      viewport: window.innerWidth,
      clientWidth: root.clientWidth,
      rootScrollWidth: root.scrollWidth,
      bodyScrollWidth: body ? body.scrollWidth : 0,
      inner,
      offenders,
    };
  });
}

async function assertNoRootOverflow(page, groupId, label, viewport = null) {
  const previous = page.viewportSize();
  if (viewport) {
    await page.setViewportSize(viewport);
    await page.waitForTimeout(250);
  }
  const metrics = await overflowMetrics(page);
  if (viewport && previous) await page.setViewportSize(previous);
  const over =
    metrics.rootScrollWidth > metrics.clientWidth + 1 ||
    metrics.bodyScrollWidth > metrics.viewport + 1;
  group(groupId).counts[`${label}_overflow`] = over;
  group(groupId).counts[`${label}_metrics`] = metrics;
  if (over) {
    softFailure(
      groupId,
      `${label}: root horizontal overflow (root=${metrics.rootScrollWidth} client=${metrics.clientWidth} body=${metrics.bodyScrollWidth} viewport=${metrics.viewport})`,
    );
  }
  return metrics;
}

async function waitForQueueSettled(page) {
  await page.getByRole("heading", { name: "诉求队列" }).waitFor();
  await page
    .getByText(/正在读取当前市场的诉求队列|当前市场暂无诉求|已读取 \d+ \/ \d+ 条/)
    .first()
    .waitFor({ timeout: 90000 });
}

async function loginStaff(page, token, { keyboard = false } = {}) {
  await page.goto(`${config.dtcBaseUrl}/staff`, { waitUntil: "domcontentloaded" });
  const input = page.getByLabel(/员工凭证/);
  await input.waitFor();
  if (keyboard) {
    let reached = false;
    for (let index = 0; index < 12; index += 1) {
      await page.keyboard.press("Tab");
      const active = await page.evaluate(() => ({
        tag: document.activeElement?.tagName ?? "",
        name: document.activeElement?.getAttribute("name") ?? "",
        type: document.activeElement?.getAttribute("type") ?? "",
      }));
      if (active.name === "token" && active.type === "password") {
        reached = true;
        break;
      }
    }
    if (!reached) throw new Error("keyboard Tab did not reach the staff token field");
    await page.keyboard.type(token);
    await page.keyboard.press("Tab");
    const submitFocus = await page.evaluate(() => document.activeElement?.textContent ?? "");
    if (!submitFocus.includes("登录工作台")) {
      await page.getByLabel(/员工凭证/).focus();
      await page.getByRole("button", { name: /登录工作台/ }).focus();
    }
    await page.keyboard.press("Enter");
  } else {
    await input.fill(token);
    await page.getByRole("button", { name: /登录工作台/ }).click();
  }
  await page.locator(".staff-topbar").waitFor({ timeout: 60000 });
  const queueHeading = page.getByRole("heading", { name: "诉求队列" });
  const operationsHeading = page.getByRole("heading", { name: "执行与对账" });
  await Promise.race([
    queueHeading.waitFor({ timeout: 60000 }),
    operationsHeading.waitFor({ timeout: 60000 }),
  ]);
  if (await queueHeading.count()) await waitForQueueSettled(page);
}

async function refreshQueue(page) {
  const refresh = page.getByRole("button", { name: /^刷新$/ });
  if (await refresh.count()) {
    await refresh.first().click();
    await page.waitForTimeout(350);
  }
}

async function waitForCaseRow(page, timeout = 150000) {
  const deadline = Date.now() + timeout;
  let attempts = 0;
  while (Date.now() < deadline) {
    attempts += 1;
    await refreshQueue(page);
    const count = await page.locator(".staff-case-row").count();
    if (count > 0) return count;
    await page.waitForTimeout(1000);
  }
  throw new Error("the Worker did not make a case visible in the staff queue");
}

async function openCaseByDescription(page, description) {
  await waitForCaseRow(page);
  const rows = page.locator(".staff-case-row");
  const count = await rows.count();
  for (let index = 0; index < count; index += 1) {
    await rows.nth(index).click();
    await page.locator(".staff-description").waitFor({ timeout: 60000 });
    await page.waitForTimeout(500);
    const value = await page.locator(".staff-description").innerText();
    if (value.includes(description)) return;
  }
  throw new Error("could not find the target case by its consumer description");
}

function waitForPost(page, suffix, timeout = 60000) {
  return page.waitForResponse(
    (response) =>
      response.request().method() === "POST" && pathOf(response.url()).endsWith(suffix),
    { timeout },
  );
}

async function firstVisible(page, locator) {
  const count = await locator.count();
  for (let index = 0; index < count; index += 1) {
    const item = locator.nth(index);
    if (await item.isVisible().catch(() => false)) return item;
  }
  return null;
}

async function selectFirstEvidence(page, groupId = "group1") {
  const boxes = page.locator(".staff-evidence-picker input[type=checkbox]");
  const count = await boxes.count();
  assertion(groupId, count > 0, "the staff detail exposes at least one usable policy evidence chunk");
  const first = boxes.first();
  if (!(await first.isChecked())) await first.check();
  return count;
}

async function selectEvidenceMatching(page, matcher) {
  const rows = page.locator(".staff-check-row");
  const count = await rows.count();
  for (let index = 0; index < count; index += 1) {
    const row = rows.nth(index);
    const text = await row.innerText();
    if (!matcher.test(text)) continue;
    const box = row.locator("input[type=checkbox]");
    if (!(await box.isChecked())) await box.check();
    return text.trim();
  }
  return null;
}

async function maybeAdoptLatestRevision(page, groupId) {
  const adopt = page.getByRole("button", { name: /确认以 r\d+ 为保存基准/ });
  if (await adopt.count()) {
    await adopt.first().click();
    await page.getByText(/已确认以服务端数据版本 r\d+ 为保存基准/).waitFor();
    action(groupId, "explicitly adopted the latest server revision after a material change");
    return true;
  }
  return false;
}

async function tabSequence(page, max = 80) {
  const sequence = [];
  for (let index = 0; index < max; index += 1) {
    await page.keyboard.press("Tab");
    const active = await page.evaluate(() => {
      const element = document.activeElement;
      if (!element) return null;
      return {
        tag: element.tagName,
        role: element.getAttribute("role") ?? "",
        name:
          element.getAttribute("aria-label") ||
          element.getAttribute("name") ||
          element.textContent?.trim().slice(0, 80) ||
          "",
        type: element.getAttribute("type") ?? "",
      };
    });
    if (active) sequence.push(active);
  }
  return sequence;
}

async function storageSnapshot(page) {
  return page.evaluate(() => {
    const local = {};
    const session = {};
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index);
      local[key] = localStorage.getItem(key);
    }
    for (let index = 0; index < sessionStorage.length; index += 1) {
      const key = sessionStorage.key(index);
      session[key] = sessionStorage.getItem(key);
    }
    return {
      local,
      session,
      cookie: document.cookie,
    };
  });
}

function containsSecret(storage, secrets) {
  const joined = JSON.stringify(storage);
  return secrets.some((secret) => secret && joined.includes(secret));
}

async function group1(browser) {
  setGroupStatus("group1", "running");
  const context = await createContext(browser, { width: 1440, height: 1000 }, "group1-consumer");
  const page = await newPage(context, "group1-consumer");
  action("group1", "consumerOne opened the dedicated bootstrap URL");
  await page.goto(config.consumers.consumerOne.bootstrapUrl, { waitUntil: "domcontentloaded" });
  await page.getByRole("heading", { name: "After-sales support" }).waitFor();
  await page.getByLabel("What went wrong?").waitFor();
  assertion("group1", await page.getByText(/No saved after-sales request/).isVisible(), "consumerOne starts with a browser-local empty state");

  const tabbed = await tabSequence(page, 90);
  group("group4").counts.consumerTabStops = tabbed.length;
  assertion(
    "group4",
    tabbed.some((item) => item.name.includes("Send request")),
    "keyboard Tab reaches the consumer Send request action",
  );
  await page.getByLabel("What went wrong?").focus();

  await page.getByLabel("What went wrong?").selectOption("damaged");
  await page.getByLabel("Description", { exact: true }).fill(expected.consumerOneDescription);
  const uploadPromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      pathOf(response.url()).endsWith("/after-sales/attachments"),
    { timeout: 60000 },
  );
  await page.getByLabel(/Photos/).setInputFiles(config.uploadPngPath);
  const uploadResponse = await uploadPromise;
  const uploadBody = await uploadResponse.json();
  recordCode("group1", "attachmentUpload", uploadResponse.status());
  recordId("group1", "consumerOneAttachmentId", uploadBody.attachment_id);
  assertion("group1", uploadResponse.status() === 201, "real attachment upload returned 201");
  await page.getByText(path.basename(config.uploadPngPath)).waitFor();

  const createPromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      pathOf(response.url()).endsWith("/after-sales/requests"),
    { timeout: 60000 },
  );
  await page.getByRole("button", { name: "Send request" }).click();
  const createResponse = await createPromise;
  const createBody = await createResponse.json();
  recordCode("group1", "requestCreate", createResponse.status());
  recordId("group1", "consumerOneRequestId", createBody.request_id);
  assertion("group1", [200, 201].includes(createResponse.status()), "real consumer request creation succeeded");
  await page.getByText("Received — awaiting reply").waitFor();
  const uiRequestId = (await page.locator("h3.break-all").first().innerText()).trim();
  assertion(
    "group1",
    uiRequestId === createBody.request_id,
    `the page shows the exact pending request id (ui=${uiRequestId} api=${createBody.request_id})`,
  );
  recordCount("group1", "consumerOneAttachmentCount", createBody.attachments?.length ?? 0);
  await screenshot(page, "group1", "consumer-pending", { width: 1440, height: 1000 });
  await screenshot(page, "group1", "consumer-pending", { width: 320, height: 900 });
  await assertNoRootOverflow(page, "group4", "consumer-pending-320", { width: 320, height: 900 });

  const supportContext = await createContext(browser, { width: 1440, height: 1000 }, "group1-support");
  const supportPage = await newPage(supportContext, "group1-support");
  action("group1", "support logged in through the real staff form using keyboard focus");
  await loginStaff(supportPage, config.staff.support, { keyboard: true });
  await openCaseByDescription(supportPage, expected.consumerOneDescription);
  assertion("group1", (await supportPage.locator(".staff-description").innerText()).includes(expected.consumerOneDescription), "staff sees the exact consumer description");
  const sourceOrderPanel = supportPage
    .locator(".staff-panel")
    .filter({ hasText: "订单与物流" })
    .first();
  const sourceOrderText = await sourceOrderPanel.innerText();
  assertion(
    "group1",
    sourceOrderText.includes("Synthetic Mug") && sourceOrderText.includes("SYNTH-MUG-001"),
    "the displayed source order is the actual Synthetic Mug / SYNTH-MUG-001 fixture",
  );
  const preview = await firstVisible(supportPage, supportPage.getByRole("button", { name: "预览" }));
  assertion("group1", preview !== null, "staff can request an authorized attachment preview");
  await preview.click();
  await supportPage.locator('img[alt^="附件 "]').first().waitFor();
  action("group1", "staff previewed the protected consumer attachment through the UI");
  const evidenceCount = Number(
    (await supportPage.getByText(/可用证据 \d+ 条/).first().innerText()).match(/(\d+)/)?.[1] ?? 0,
  );
  assertion("group1", evidenceCount > 0, "staff receives real market/date-scoped policy evidence");
  recordCount("group1", "evidenceChunks", evidenceCount);

  action("group1", "support added required manual verification and delivery proof through the UI");
  await supportPage.getByLabel("材料类型").selectOption("manual_verification");
  await supportPage.getByLabel("来源（必填）").fill(expected.materialManualSource);
  await supportPage.getByLabel("材料内容").fill(expected.materialManualContent);
  const manualMaterial = waitForPost(supportPage, "/materials");
  await supportPage.getByRole("button", { name: "保存内部材料" }).click();
  const manualResponse = await manualMaterial;
  recordCode("group1", "manualMaterialSave", manualResponse.status());
  assertion("group1", manualResponse.status() === 200, "manual verification material saved through the real UI");
  await supportPage.getByText(expected.materialManualContent).waitFor();

  await supportPage.getByLabel("材料类型").selectOption("delivery_verification");
  await supportPage.getByLabel("来源（必填）").fill(expected.materialDeliverySource);
  await supportPage.getByLabel("材料内容").fill(expected.materialDeliveryContent);
  await supportPage.getByLabel(/订单 SKU/).selectOption("SYNTH-MUG-001");
  await supportPage.getByLabel(/签收核验时间/).fill("2026-11-05T10:00");
  const deliveryMaterial = waitForPost(supportPage, "/materials");
  await supportPage.getByRole("button", { name: "保存内部材料" }).click();
  const deliveryResponse = await deliveryMaterial;
  recordCode("group1", "deliveryMaterialSave", deliveryResponse.status());
  assertion("group1", deliveryResponse.status() === 200, "delivery proof material saved through the real UI");
  await supportPage.getByText(expected.materialDeliveryContent).waitFor();
  recordCount("group1", "materialsSaved", 2);
  await maybeAdoptLatestRevision(supportPage, "group1");

  const reviewEvidence = await selectEvidenceMatching(
    supportPage,
    /交付损坏|退货期限|退款时限|取消与改单|配送服务|有限保修/,
  );
  assertion(
    "group1",
    reviewEvidence !== null,
    "the UI exposes review-policy evidence for the human follow-up action",
  );
  recordId("group1", "ticketAction", "other");
  action("group1", "selected action=other (human follow-up) and matching review evidence");
  await supportPage.getByLabel("处理动作").selectOption("other");
  await supportPage.getByLabel("内部摘要").fill(expected.summaryV1);
  await supportPage.getByLabel("消费者最终回复").fill(expected.replyV1);
  const draftV1 = waitForPost(supportPage, "/drafts");
  await supportPage.getByRole("button", { name: "保存为新的编辑草稿" }).click();
  const draftV1Response = await draftV1;
  recordCode("group1", "draftV1Save", draftV1Response.status());
  assertion("group1", draftV1Response.status() === 200, "draft v1 saved through the real UI");
  await supportPage.getByText(/草稿 v1 已保存为编辑中/).waitFor();
  action("group1", "support saved draft v1 through the real form");

  await supportPage.getByLabel("内部摘要").fill(expected.summaryV2);
  await supportPage.getByLabel("消费者最终回复").fill(expected.replyV2);
  const draftV2 = waitForPost(supportPage, "/drafts");
  await supportPage.getByRole("button", { name: "保存为新的编辑草稿" }).click();
  const draftV2Response = await draftV2;
  recordCode("group1", "draftV2Save", draftV2Response.status());
  assertion("group1", draftV2Response.status() === 200, "draft v2 saved through the real UI");
  await supportPage.getByText(/草稿 v2 已保存为编辑中/).waitFor();
  action("group1", "support edited and saved draft v2 through the real form");

  const submitResponsePromise = waitForPost(supportPage, "/submit");
  await supportPage.getByRole("button", { name: "提交审批" }).click();
  await supportPage.getByRole("button", { name: "确认送审" }).click();
  const submitResponse = await submitResponsePromise;
  const submitBody = await submitResponse.json().catch(() => null);
  recordCode("group1", "draftSubmit", submitResponse.status());
  recordCode("group1", "draftSubmitCode", submitBody?.error_code ?? null);
  assertion(
    "group1",
    submitResponse.status() === 200,
    `draft v2 was submitted through the real UI (status=${submitResponse.status()} code=${
      submitBody?.error_code ?? "none"
    })`,
  );
  await supportPage.getByText(/草稿 v2 已送审/).waitFor();
  recordCount("group1", "draftVersionsSubmitted", 2);
  await screenshot(supportPage, "group1", "staff-detail", { width: 1440, height: 1000 });
  await screenshot(supportPage, "group1", "staff-detail", { width: 320, height: 900 });
  await assertNoRootOverflow(supportPage, "group4", "staff-detail-320", { width: 320, height: 900 });

  const approverContext = await createContext(browser, { width: 1440, height: 1000 }, "group1-approver");
  const approverPage = await newPage(approverContext, "group1-approver");
  action("group1", "a distinct approver logged in through a fresh browser context");
  await loginStaff(approverPage, config.staff.approver);
  const pendingFilter = approverPage.getByRole("button", { name: "只看待审批" });
  if (await pendingFilter.count()) await pendingFilter.click();
  await approverPage.waitForTimeout(400);
  await openCaseByDescription(approverPage, expected.consumerOneDescription);
  await approverPage.getByText("冻结草稿 v2").waitFor();
  const snapshotPanel = approverPage
    .locator(".staff-panel")
    .filter({ hasText: "审批与冻结依据" })
    .first();
  const snapshotReply = await snapshotPanel.locator("blockquote").first().innerText();
  assertion("group1", snapshotReply.includes(expected.replyV2), "approver sees the frozen latest v2 reply");
  assertion("group1", !snapshotReply.includes(expected.replyV1), "approver does not see the superseded v1 reply");
  await screenshot(approverPage, "group1", "approver-approval", { width: 1440, height: 1000 });
  await screenshot(approverPage, "group1", "approver-approval", { width: 320, height: 900 });
  await assertNoRootOverflow(approverPage, "group4", "approver-approval-320", { width: 320, height: 900 });
  const approveResponsePromise = waitForPost(approverPage, "/approve");
  await approverPage.getByRole("button", { name: "批准并准备执行" }).click();
  const approveResponse = await approveResponsePromise;
  recordCode("group1", "approval", approveResponse.status());
  assertion("group1", approveResponse.status() === 200, "the approver decision was accepted by the real server");
  await approverPage.getByText("已批准并准备执行。系统会等待 Worker 与真实回执；现在不等于工单已完成。").waitFor();
  action("group1", "the distinct approver approved the frozen v2 snapshot");

  const ticketDeadline = Date.now() + 180000;
  let ticketSectionReady = false;
  while (Date.now() < ticketDeadline) {
    const refresh = await firstVisible(supportPage, supportPage.getByRole("button", { name: "刷新详情" }));
    if (refresh) await refresh.click();
    await supportPage.waitForTimeout(800);
    if (await supportPage.getByText("已有远端工单").first().isVisible().catch(() => false)) {
      ticketSectionReady = true;
      break;
    }
  }
  assertion("group1", ticketSectionReady, "the real Worker created and linked a remote ticket");
  await supportPage.getByText(/工单 tic_/).first().waitFor({ timeout: 60000 });
  const ticketHeading = await supportPage.getByText(/工单 tic_/).first().innerText();
  const ticketId = ticketHeading.match(/tic_[0-9a-f-]{36}/)?.[0] ?? "";
  recordId("group1", "consumerOneTicketId", ticketId);
  assertion("group1", Boolean(ticketId), "the staff ticket read exposes the real remote ticket id");
  const staffTicketReply = await supportPage.locator(".staff-ticket blockquote").first().innerText();
  assertion("group1", staffTicketReply === expected.replyV2, "the real ticket reply equals the approved v2 reply");
  await screenshot(supportPage, "group1", "staff-ticket", { width: 1440, height: 1000 });

  action("group1", "consumerOne refreshed the same saved request");
  const refreshRequest = await firstVisible(page, page.getByRole("button", { name: "Refresh request" }));
  assertion("group1", refreshRequest !== null, "consumer can refresh the saved request");
  await refreshRequest.click();
  await page.getByText("Reply received").waitFor({ timeout: 60000 });
  await page.getByRole("heading", { name: "Customer support reply" }).waitFor();
  const consumerReply = await page
    .locator("p.whitespace-pre-wrap")
    .filter({ hasText: expected.replyV2 })
    .first()
    .innerText();
  assertion("group1", consumerReply.trim() === expected.replyV2, "the consumer sees the exact edited v2 reply");
  assertion(
    "group1",
    (await page.getByText(expected.replyV1, { exact: true }).count()) === 0,
    "the consumer does not see the superseded v1 reply",
  );
  recordCount("group1", "uniqueConsumerReplies", 1);
  await screenshot(page, "group1", "consumer-reply", { width: 1440, height: 1000 });
  await screenshot(page, "group1", "consumer-reply", { width: 320, height: 900 });
  await assertNoRootOverflow(page, "group4", "consumer-reply-320", { width: 320, height: 900 });

  setGroupStatus("group1", "passed");
  return { consumerContext: context, consumerPage: page, supportContext, supportPage, approverContext, approverPage };
}

async function group2(browser, group1State) {
  setGroupStatus("group2", "running");
  const context = await createContext(browser, { width: 1440, height: 1000 }, "group2-consumer-two");
  const page = await newPage(context, "group2-consumer-two");
  action("group2", "consumerTwo opened its own bootstrap URL in a fresh browser context");
  await page.goto(config.consumers.consumerTwo.bootstrapUrl, { waitUntil: "domcontentloaded" });
  await page.getByRole("heading", { name: "After-sales support" }).waitFor();
  await page.getByLabel("What went wrong?").waitFor();

  const attachmentsBefore = captures(page, "upload_attachment").length;
  await page.getByLabel(/Photos/).setInputFiles({
    name: "notes.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("synthetic non-image input"),
  });
  await page.getByText(/notes.txt: use a JPEG, PNG, or WebP image/).waitFor();
  await page.getByLabel(/Photos/).setInputFiles({
    name: "huge.png",
    mimeType: "image/png",
    buffer: Buffer.alloc(5 * 1024 * 1024 + 1),
  });
  await page.getByText(/huge.png: each photo must be 5 MiB or smaller/).waitFor();
  recordCount("group2", "invalidFileUploadRequests", captures(page, "upload_attachment").length - attachmentsBefore);
  assertion("group2", captures(page, "upload_attachment").length === attachmentsBefore, "invalid file type/size cases are blocked before any upload call");
  action("group2", "client rejected an unsupported type and an oversized file without a request");

  const oneRequestId = group1State.requestId;
  const oneTicketId = group1State.ticketId;
  const oneAttachmentId = group1State.attachmentId;
  const isolation = await page.evaluate(
    async ({ requestId, ticketId, attachmentId }) => {
      const read = async (suffix) => {
        const response = await fetch(suffix, {
          credentials: "same-origin",
          cache: "no-store",
          headers: { "X-After-Sales-Contract-Version": "1" },
        });
        return response.status;
      };
      return {
        request: await read(`/api/au/after-sales/requests/${encodeURIComponent(requestId)}`),
        ticket: await read(`/api/au/after-sales/tickets/${encodeURIComponent(ticketId)}`),
        attachment: await read(
          `/api/au/after-sales/attachments/${encodeURIComponent(attachmentId)}/content`,
        ),
      };
    },
    { requestId: oneRequestId, ticketId: oneTicketId, attachmentId: oneAttachmentId },
  );
  recordCode("group2", "crossConsumerRequestRead", isolation.request);
  recordCode("group2", "crossConsumerTicketRead", isolation.ticket);
  recordCode("group2", "crossConsumerAttachmentRead", isolation.attachment);
  assertion("group2", [403, 404].includes(isolation.request), "consumerTwo cannot read consumerOne's request");
  assertion("group2", [403, 404].includes(isolation.ticket), "consumerTwo cannot read consumerOne's ticket");
  assertion("group2", [403, 404].includes(isolation.attachment), "consumerTwo cannot read consumerOne's attachment bytes");

  await page.getByLabel("Have a request ID from customer support?").fill(oneRequestId);
  await page.getByRole("button", { name: "Find request" }).click();
  await page.getByRole("alert").first().waitFor();
  assertion(
    "group2",
    (await page.getByText(expected.consumerOneDescription, { exact: true }).count()) === 0,
    "consumerTwo's recovery UI does not reveal consumerOne's description",
  );
  await page.getByLabel("Have a request ID from customer support?").fill("");

  action("group2", "consumerTwo submitted its own order with the controlled committed-response-loss marker");
  await page.getByLabel("What went wrong?").selectOption("other");
  await page.getByLabel("Description", { exact: true }).fill(expected.consumerTwoDescription);
  const firstCreate = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      pathOf(response.url()).endsWith("/after-sales/requests"),
    { timeout: 60000 },
  );
  await page.getByRole("button", { name: "Send request" }).click();
  const firstResponse = await firstCreate;
  const firstBody = await firstResponse.json();
  recordCode("group2", "committedResponseLoss", firstResponse.status());
  assertion("group2", firstResponse.status() === 503, "the controlled boundary returned a real 503 after commit");
  assertion(
    "group2",
    Boolean(firstBody?.error?.request_id),
    "the controlled 503 carries the committed request id for audit",
  );
  recordId("group2", "consumerTwoCommittedRequestId", firstBody.error.request_id);
  await page.getByText("An unconfirmed application is saved in this tab.").waitFor();
  assertion(
    "group2",
    (await page.getByRole("button", { name: "Send request" }).count()) === 0,
    "the unresolved application hides the new-request form and cannot mint a new intent",
  );
  const firstCapture = captures(page, "create_request").at(-1);
  recordCount("group2", "createRequestsBeforeReload", captures(page, "create_request").length);
  assertion("group2", captures(page, "create_request").length === 1, "only one create request was sent before recovery");

  const storageBefore = await storageSnapshot(page);
  const localRaw = JSON.stringify(storageBefore.local);
  const sessionRaw = JSON.stringify(storageBefore.session);
  assertion("group2", !localRaw.includes(config.lostResponseMarker), "localStorage stores no request body/marker");
  assertion("group2", !localRaw.includes(expected.consumerTwoDescription), "localStorage stores no consumer description");
  assertion("group2", sessionRaw.includes(expected.consumerTwoDescription), "sessionStorage keeps the private pending body for this owned context");
  recordCount("group2", "createRequestsAfterFirstSubmit", captures(page, "create_request").length);
  recordCount("group2", "pendingSessionKeys", Object.keys(storageBefore.session).length);
  recordCount("group2", "localMarkerKeys", Object.keys(storageBefore.local).length);

  action("group2", "consumerTwo refreshed the tab; the saved key/body auto-replayed");
  const replayPromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      pathOf(response.url()).endsWith("/after-sales/requests"),
    { timeout: 90000 },
  );
  await page.reload({ waitUntil: "domcontentloaded" });
  const replayResponse = await replayPromise;
  const replayBody = await replayResponse.json();
  recordCode("group2", "replayedCreate", replayResponse.status());
  assertion("group2", replayResponse.status() === 200, "the original key/body replayed to an idempotent 200");
  assertion(
    "group2",
    replayBody.request_id === firstBody.error.request_id,
    "the replay returned the same committed request id",
  );
  await page.getByText("Received — awaiting reply").waitFor();
  const replayedUiId = (await page.locator("h3.break-all").first().innerText()).trim();
  recordId("group2", "consumerTwoRequestId", replayedUiId);
  assertion("group2", replayedUiId === firstBody.error.request_id, "the page shows the one committed request id");

  const secondCapture = captures(page, "create_request").at(-1);
  assertion(
    "group2",
    firstCapture && secondCapture && firstCapture.idempotencyKeyHash === secondCapture.idempotencyKeyHash,
    "the retry reused the exact original idempotency key",
  );
  assertion(
    "group2",
    firstCapture && secondCapture && firstCapture.bodyHash === secondCapture.bodyHash,
    "the retry reused the exact original request body",
  );
  recordCount("group2", "totalCreateRequests", captures(page, "create_request").length);
  assertion("group2", captures(page, "create_request").length === 2, "the source saw exactly one submit plus one idempotent replay");
  let storageAfter = await storageSnapshot(page);
  let afterSessionRaw = JSON.stringify(storageAfter.session);
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (!afterSessionRaw.includes(expected.consumerTwoDescription)) break;
    await page.waitForTimeout(100);
    storageAfter = await storageSnapshot(page);
    afterSessionRaw = JSON.stringify(storageAfter.session);
  }
  const afterLocalRaw = JSON.stringify(storageAfter.local);
  assertion("group2", !afterLocalRaw.includes(expected.consumerTwoDescription), "localStorage still stores no description after recovery");
  assertion(
    "group2",
    !afterSessionRaw.includes(expected.consumerTwoDescription) &&
      !afterSessionRaw.includes(config.lostResponseMarker),
    "the pending session body was cleared after the idempotent success",
  );
  assertion(
    "group2",
    afterLocalRaw.includes(replayedUiId),
    "localStorage keeps only the request id/time index after recovery",
  );
  await screenshot(page, "group2", "consumer-recovered", { width: 1440, height: 1000 });
  await screenshot(page, "group2", "consumer-recovered", { width: 320, height: 900 });
  await assertNoRootOverflow(page, "group4", "group2-recovered-320", { width: 320, height: 900 });

  setGroupStatus("group2", "passed");
  return { context, page };
}

async function group3(browser, group1State, group2State) {
  setGroupStatus("group3", "running");
  const contextA = await createContext(browser, { width: 1440, height: 1000 }, "group3-support-a");
  const pageA = await newPage(contextA, "group3-support-a");
  await loginStaff(pageA, config.staff.support);
  await openCaseByDescription(pageA, expected.consumerTwoDescription);
  action("group3", "support A opened the second order case and requested an AI suggestion");
  const aiResponsePromise = pageA.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      pathOf(response.url()).endsWith("/suggestion"),
    { timeout: 60000 },
  );
  await pageA.getByRole("button", { name: "AI 生成建议草稿" }).click();
  const aiResponse = await aiResponsePromise;
  const aiBody = await aiResponse.json();
  recordCode("group3", "aiUnavailable", aiResponse.status());
  recordCode("group3", "aiUnavailableCode", aiBody?.error_code ?? null);
  assertion("group3", aiResponse.status() === 503, "the unconfigured model returns a real 503, not a fake suggestion");
  await pageA.getByText("AI 建议不可用，可继续人工撰写草稿").waitFor();
  action("group3", "the UI kept the manual draft path available after the AI 503");

  await pageA.getByLabel("内部摘要").fill(expected.supportASummary);
  await pageA.getByLabel("消费者最终回复").fill(expected.supportAReply);
  await selectFirstEvidence(pageA, "group3");

  const contextB = await createContext(browser, { width: 1440, height: 1000 }, "group3-support-b");
  const pageB = await newPage(contextB, "group3-support-b");
  await loginStaff(pageB, config.staff.support);
  await openCaseByDescription(pageB, expected.consumerTwoDescription);
  await pageB.getByLabel("内部摘要").fill(expected.supportBSummary);
  await pageB.getByLabel("消费者最终回复").fill(expected.supportBReply);
  await selectFirstEvidence(pageB, "group3");
  const bSavePromise = waitForPost(pageB, "/drafts");
  await pageB.getByRole("button", { name: "保存为新的编辑草稿" }).click();
  const bSaveResponse = await bSavePromise;
  recordCode("group3", "supportBSave", bSaveResponse.status());
  assertion("group3", bSaveResponse.status() === 200, "support B's real save advanced the server revision");
  await pageB.getByText(/草稿 v1 已保存为编辑中/).waitFor();
  action("group3", "support B advanced the actual server revision by saving draft v1");

  const saveCountBefore = captures(pageA, "save_draft").length;
  const conflictPromise = pageA.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      pathOf(response.url()).endsWith("/drafts"),
    { timeout: 60000 },
  );
  await pageA.getByRole("button", { name: "保存为新的编辑草稿" }).click();
  const conflictResponse = await conflictPromise;
  const conflictBody = await conflictResponse.json();
  recordCode("group3", "staleSave", conflictResponse.status());
  recordCode("group3", "staleSaveCode", conflictBody?.error_code ?? null);
  assertion("group3", conflictResponse.status() === 409, "the stale save received a real server 409");
  await pageA.getByText("版本冲突，未提交的输入已保留").waitFor();
  assertion("group3", (await pageA.getByLabel("内部摘要").inputValue()) === expected.supportASummary, "409 preserved the unsaved summary");
  assertion("group3", (await pageA.getByLabel("消费者最终回复").inputValue()) === expected.supportAReply, "409 preserved the unsaved reply");
  await pageA.waitForTimeout(1200);
  recordCount("group3", "staleSaveRequests", captures(pageA, "save_draft").length - saveCountBefore);
  assertion("group3", captures(pageA, "save_draft").length - saveCountBefore === 1, "409 did not trigger an automatic retry write");

  const readLatest = await firstVisible(pageA, pageA.getByRole("button", { name: "读取最新版本（保留输入）" }));
  assertion("group3", readLatest !== null, "the conflict notice offers an explicit latest read");
  await readLatest.click();
  await pageA.waitForTimeout(900);
  assertion("group3", (await pageA.getByLabel("内部摘要").inputValue()) === expected.supportASummary, "explicit latest read preserved the unsaved summary");
  assertion("group3", (await pageA.getByLabel("消费者最终回复").inputValue()) === expected.supportAReply, "explicit latest read preserved the unsaved reply");
  await pageA.getByText("服务端数据已变化，你的输入仍保留").waitFor();
  await pageA.getByRole("button", { name: /确认以 r\d+ 为保存基准/ }).click();
  await pageA.getByText(/已确认以服务端数据版本 r\d+ 为保存基准/).waitFor();
  assertion("group3", (await pageA.getByLabel("消费者最终回复").inputValue()) === expected.supportAReply, "explicit revision confirmation preserved the input");

  const postConfirmCount = captures(pageA, "save_draft").length;
  const explicitSavePromise = waitForPost(pageA, "/drafts");
  await pageA.getByRole("button", { name: "保存为新的编辑草稿" }).click();
  const explicitSaveResponse = await explicitSavePromise;
  recordCode("group3", "explicitSaveAfterConfirmation", explicitSaveResponse.status());
  assertion("group3", explicitSaveResponse.status() === 200, "the explicit post-confirmation save succeeded");
  await pageA.getByText(/草稿 v2 已保存为编辑中/).waitFor();
  recordCount("group3", "explicitSaveAfterConfirmation", captures(pageA, "save_draft").length - postConfirmCount);
  assertion("group3", captures(pageA, "save_draft").length - postConfirmCount === 1, "only the explicit post-confirmation save was written");

  const storageBeforeLogout = await storageSnapshot(pageA);
  assertion("group3", !containsSecret(storageBeforeLogout, secretValues), "no staff token is present in persistent browser storage");
  action("group3", "support A logged out with business data and draft state open");
  await pageA.getByRole("button", { name: /退出/ }).click();
  await pageA.getByLabel(/员工凭证/).waitFor();
  const loggedOutText = await pageA.locator("body").innerText();
  assertion("group3", !loggedOutText.includes(expected.consumerTwoDescription), "logout cleared the consumer description");
  assertion("group3", !loggedOutText.includes(expected.supportAReply), "logout cleared the draft reply");
  const storageAfterLogout = await storageSnapshot(pageA);
  assertion("group3", !containsSecret(storageAfterLogout, secretValues), "logout left no staff token in owned storage/cookies");

  action("group3", "auditor logged in and reviewed with a read-only surface");
  const auditorContext = await createContext(browser, { width: 1440, height: 1000 }, "group3-auditor");
  const auditorPage = await newPage(auditorContext, "group3-auditor");
  await loginStaff(auditorPage, config.staff.auditor);
  await openCaseByDescription(auditorPage, expected.consumerOneDescription);
  await auditorPage.getByText("只读视图").waitFor();
  await auditorPage.getByText("当前角色不能批准或驳回，只能查看审批依据。").waitFor();
  assertion("group3", (await auditorPage.getByRole("button", { name: "保存为新的编辑草稿" }).count()) === 0, "auditor has no draft save action");
  assertion("group3", (await auditorPage.getByRole("button", { name: "提交审批" }).count()) === 0, "auditor has no submit action");
  assertion("group3", (await auditorPage.getByRole("button", { name: "批准并准备执行" }).count()) === 0, "auditor has no approval action");
  await screenshot(auditorPage, "group3", "auditor-readonly", { width: 1440, height: 1000 });

  action("group3", "operator logged in to the safe operations-only surface");
  const operatorContext = await createContext(browser, { width: 1440, height: 1000 }, "group3-operator");
  const operatorPage = await newPage(operatorContext, "group3-operator");
  await loginStaff(operatorPage, config.staff.operator);
  await operatorPage.getByRole("heading", { name: "执行与对账" }).waitFor();
  await operatorPage.getByText("操作列表", { exact: true }).waitFor();
  const operatorText = await operatorPage.locator("body").innerText();
  assertion("group3", !operatorText.includes(expected.consumerOneDescription), "operator cannot see consumer descriptions");
  assertion("group3", !operatorText.includes(expected.replyV2), "operator cannot see consumer reply material");
  assertion("group3", !operatorText.includes(path.basename(config.uploadPngPath)), "operator cannot see attachment material");
  assertion("group3", operatorText.includes("仅显示脱敏运行状态"), "operator sees the safe metadata boundary note");
  await screenshot(operatorPage, "group3", "operator", { width: 1440, height: 1000 });
  await screenshot(operatorPage, "group3", "operator", { width: 320, height: 900 });
  await assertNoRootOverflow(operatorPage, "group4", "operator-320", { width: 320, height: 900 });

  action("group3", "empty queue state checked with a real server-backed filter");
  const statusFilter = pageB.getByLabel("处理状态");
  await statusFilter.selectOption("failed");
  await pageB.getByText("当前筛选下没有诉求").waitFor();
  await screenshot(pageB, "group4", "staff-empty-queue", { width: 1440, height: 1000 });
  group("group4").assertions.push({ ok: true, message: "empty queue state is visible" });
  await pageB.getByLabel("队列筛选").getByRole("button", { name: "清除筛选" }).click();
  await waitForQueueSettled(pageB);

  action("group3", "invalid credential login produced a visible real error");
  const errorContext = await createContext(browser, { width: 1440, height: 1000 }, "group3-login-error");
  const errorPage = await newPage(errorContext, "group3-login-error");
  await errorPage.goto(`${config.dtcBaseUrl}/staff`, { waitUntil: "domcontentloaded" });
  await errorPage.getByLabel(/员工凭证/).fill("dtc_staff_invalid_synthetic_credential");
  const invalidLoginPromise = errorPage.waitForResponse(
    (response) => pathOf(response.url()).endsWith("/after-sales/me"),
    { timeout: 60000 },
  );
  await errorPage.getByRole("button", { name: /登录工作台/ }).click();
  const invalidLoginResponse = await invalidLoginPromise;
  recordCode("group3", "invalidLogin", invalidLoginResponse.status());
  assertion("group3", invalidLoginResponse.status() === 401, "invalid credential login is rejected by the real server");
  await errorPage.getByText("登录已失效，请重新登录。").waitFor();
  group("group4").assertions.push({ ok: true, message: "invalid credential error is visible" });
  await screenshot(errorPage, "group4", "staff-login-error", { width: 320, height: 900 });

  action("group3", "first queue load rendered a visible loading state");
  const loadingContext = await createContext(
    browser,
    { width: 320, height: 900 },
    "group3-loading",
    { delayCaseList: true },
  );
  const loadingPage = await newPage(loadingContext, "group3-loading");
  await loadingPage.goto(`${config.dtcBaseUrl}/staff`, { waitUntil: "domcontentloaded" });
  await loadingPage.getByLabel(/员工凭证/).fill(config.staff.support);
  await loadingPage.getByRole("button", { name: /登录工作台/ }).click();
  await loadingPage.getByText("正在读取当前市场的诉求队列…").waitFor();
  await screenshot(loadingPage, "group4", "staff-loading", { width: 320, height: 900 });
  group("group4").assertions.push({ ok: true, message: "visible loading state captured" });
  await loadingContext.unrouteAll({ behavior: "wait" });
  await loadingContext.close();

  setGroupStatus("group3", "passed");
  return { contextA, pageA, contextB, pageB, auditorContext, auditorPage, operatorContext, operatorPage };
}

async function group4(browser, state) {
  setGroupStatus("group4", "running");
  action("group4", "collected real 320px and desktop renders for consumer, queue, detail, approval and operations surfaces");
  await assertNoRootOverflow(state.consumerPage, "group4", "consumer-final-320", { width: 320, height: 900 });
  await assertNoRootOverflow(state.supportPage, "group4", "staff-support-final-320", { width: 320, height: 900 });
  await screenshot(state.supportPage, "group4", "staff-queue", { width: 320, height: 900 });
  await screenshot(state.supportPage, "group4", "staff-queue", { width: 1440, height: 1000 });

  const supportTab = await tabSequence(state.supportPage, 60);
  group("group4").counts.staffTabStops = supportTab.length;
  group("group4").assertions.push({
    ok: supportTab.length > 0,
    message: "staff Tab sequence captured",
  });
  if (supportTab.length === 0) softFailure("group4", "staff Tab sequence was empty");

  const overflowKeys = Object.entries(group("group4").counts)
    .filter(([key]) => key.endsWith("_overflow"))
    .map(([key, value]) => `${key}=${value}`);
  action("group4", `overflow observations: ${overflowKeys.join(", ")}`);
  setGroupStatus("group4", group("group4").failures.length === 0 ? "passed" : "failed");
}

async function main() {
  const browserServer = await chromium.launchServer({
    headless: true,
    host: "127.0.0.1",
    port: 0,
    handleSIGINT: false,
    handleSIGHUP: false,
    handleSIGTERM: false,
    args: ["--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check"],
  });
  const serverProcess = browserServer.process();
  const browserPid = serverProcess?.pid;
  if (!Number.isInteger(browserPid) || browserPid <= 1) {
    throw new Error("the browser server did not expose its actual process id");
  }
  const actualExecutable =
    (typeof serverProcess?.spawnfile === "string" && serverProcess.spawnfile) ||
    chromium.executablePath();
  const sidecar = {
    version: 1,
    pid: browserPid,
    pgid: process.platform === "win32" ? null : browserPid,
    platform: process.platform,
    executablePath: actualExecutable,
    startedAt: nowIso(),
  };
  writeFileSync(config.browserLifecyclePath, `${JSON.stringify(sidecar)}\n`, {
    mode: 0o600,
  });
  chmodSync(config.browserLifecyclePath, 0o600);
  const endpoint = browserServer.wsEndpoint();
  const browser = await chromium.connect(endpoint);
  browserLifecycle.serverProcessPid = browserPid;
  browserLifecycle.serverProcessExecutable = actualExecutable;
  report.browser = {
    version: browser.version(),
    engine: "Playwright Chromium",
    executablePath: actualExecutable,
    headless: true,
    pid: browserPid,
    pgid: sidecar.pgid,
    server: "launchServer+connect",
  };
  let state = null;
  try {
    state = await group1(browser);
    const group1State = {
      requestId: report.ids.consumerOneRequestId,
      ticketId: report.ids.consumerOneTicketId,
      attachmentId: report.ids.consumerOneAttachmentId,
    };
    const group2State = await group2(browser, group1State);
    const group3State = await group3(browser, group1State, group2State);
    await group4(browser, {
      ...state,
      ...group3State,
    });
    for (const item of report.groups) {
      if (item.status === "running") item.status = "not_run";
    }
  } catch (error) {
    const active = report.groups.find((item) => item.status === "running");
    if (active) {
      active.status = "failed";
      active.failures.push(redact(error?.message ?? error));
    }
    report.errors = report.errors ?? [];
    report.errors.push(redact(error?.message ?? error));
    for (const item of report.groups) {
      if (item.status === "not_run") continue;
      if (item.status === "running") item.status = "failed";
    }
    await captureFailureDiagnostics(active?.id ?? null);
  } finally {
    suiteClosing = true;
    browserLifecycle.closeAttempted = true;
    try {
      await browserServer.close();
      browserLifecycle.closeOk = true;
    } catch (error) {
      browserLifecycle.closeError = redact(error?.message ?? error);
    }
    try {
      browserLifecycle.connectedAfterClose = browser.isConnected();
    } catch (error) {
      browserLifecycle.connectedAfterClose = null;
      browserLifecycle.connectionStateError = redact(error?.message ?? error);
    }
  }
  report.browserLifecycle = browserLifecycle;
  if (report.browser) {
    report.browser.closed =
      browserLifecycle.closeOk && browserLifecycle.connectedAfterClose === false;
  }

  if (report.pageErrors.length > 0) {
    report.status = "failed";
    report.errors = report.errors ?? [];
    report.errors.push(`unexpected page errors: ${report.pageErrors.length}`);
  }
  if (report.routeErrors.length > 0) {
    report.status = "failed";
    report.errors = report.errors ?? [];
    report.errors.push(`unexpected route errors: ${report.routeErrors.length}`);
  }
  if (report.groups.some((item) => item.status === "failed")) report.status = "failed";
  if (report.groups.some((item) => item.status === "not_run")) report.status = "failed";
  if (!report.status || report.status === "running") report.status = "passed";
  report.finishedAt = nowIso();
  const payload = `${JSON.stringify(report, null, 2)}\n`;
  writeFileSync(reportPath, payload, { mode: 0o600 });
  chmodSync(reportPath, 0o600);
  if (statSync(reportPath).size > 5_000_000) {
    throw new Error("browser report exceeded the bounded size limit");
  }

  const summary = [
    `after-sales browser: ${report.status.toUpperCase()}`,
    `report=${reportPath}`,
    `screenshots=${config.screenshotsDirectory}`,
    `groups=${report.groups.map((item) => `${item.id}:${item.status}`).join(" ")}`,
    `page_errors=${report.pageErrors.length} blocked_external=${report.blockedExternalRequests.length}`,
  ];
  console.log(summary.join("\n"));
  process.exitCode = report.status === "passed" ? 0 : 1;
}

await main();
