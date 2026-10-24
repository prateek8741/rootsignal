import { expect, test } from "@playwright/test";

// Needs: API + worker running with data (`./scripts/demo.sh --keep`, or `make seed-demo`), `npm run dev`,
// and DASHBOARD_LOGIN_URL set to a fresh one-time link (printed by both, or
// `rootsignal-server dashboard-link --org demo --project agents`).
const loginUrl = process.env.DASHBOARD_LOGIN_URL ?? "";

test("sign in with a link, inspect a failing trace, investigate its issue", async ({ page, context }) => {
  test.skip(!loginUrl, "set DASHBOARD_LOGIN_URL to a fresh one-time login link");

  await page.goto(loginUrl);
  await expect(page.getByRole("heading", { name: "Traces" })).toBeVisible();
  expect(page.url()).not.toContain("token"); // erased from the address bar and history

  // The session is an httpOnly cookie: present for the browser, invisible to page scripts.
  const cookies = await context.cookies();
  const session = cookies.find((c) => c.name === "rs_session");
  expect(session?.httpOnly).toBe(true);
  expect(session?.sameSite).toBe("Strict");
  const visibleToScripts = await page.evaluate(() => ({
    cookie: document.cookie, session: sessionStorage.length, local: localStorage.length,
  }));
  expect(visibleToScripts).toEqual({ cookie: "", session: 0, local: 0 });
  await page.screenshot({ path: "test-results/01-traces.png", fullPage: true });

  await page.getByLabel("Agent").fill("support-agent");
  await page.getByLabel("Version").fill("v1");
  const rows = page.locator("tbody tr");
  const withFindings = rows.filter({ has: page.locator("td:nth-child(4) span", { hasText: /^[1-9]/ }) });
  await withFindings.first().getByRole("link").first().click();
  await expect(page.getByRole("tree", { name: "Spans" })).toBeVisible();
  await page.screenshot({ path: "test-results/02-trace.png", fullPage: true });

  await page.getByRole("link", { name: "View issue →" }).first().click();
  await expect(page.getByRole("heading", { name: "History" })).toBeVisible();
  const investigate = page.getByRole("button", { name: /Run investigation|Investigate again/ });
  await investigate.click();
  // Wait for *this* run to finish: an earlier investigation's results may already be shown.
  await expect(page.getByRole("button", { name: "Investigating…" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Investigate again" })).toBeVisible({ timeout: 20_000 });
  const observed = page.getByRole("region", { name: "Observed" });
  await expect(observed).toBeVisible();
  await expect(page.getByRole("region", { name: "Hypotheses" })).toBeVisible();
  await page.screenshot({ path: "test-results/03-investigation.png", fullPage: true });

  // Evidence links lead to the exact span.
  const evidence = observed.getByRole("link", { name: /^span / }).first();
  const spanId = new URL(String(await evidence.getAttribute("href")), "http://x").searchParams.get("span");
  await evidence.click();
  await expect(page.getByRole("treeitem", { selected: true })).toBeVisible();
  await expect(page.getByText(String(spanId), { exact: true })).toBeVisible(); // details panel
  await page.screenshot({ path: "test-results/04-evidence-span.png", fullPage: true });

  // Turn the failure into a regression case.
  await page.goBack();
  await expect(page.getByRole("heading", { name: "History" })).toBeVisible();
  await page.getByRole("button", { name: "Create from worst trace" }).click();
  await expect(page.getByRole("heading", { name: /Assertions \(\d+ enabled\)/ })).toBeVisible();
  await expect(page.getByText("reproduces").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Download JSON" })).toBeVisible();
  await page.screenshot({ path: "test-results/05-regression-case.png", fullPage: true });

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("heading", { name: "Sign in to Rootsignal" })).toBeVisible();
  await page.goto("/traces");
  await expect(page.getByRole("heading", { name: "Sign in to Rootsignal" })).toBeVisible();
});
