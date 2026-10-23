import { expect, test } from "@playwright/test";

// Needs: API + worker running, demo data seeded (scripts/seed_demo.py), `npm run dev`,
// and DASHBOARD_KEY set to the read+triage key the seed script printed.
const key = process.env.DASHBOARD_KEY ?? "";

test("connect, inspect a failing trace, open its issue", async ({ page }) => {
  test.skip(!key, "set DASHBOARD_KEY to a read+triage key");

  await page.goto("/connect");
  await page.getByLabel("API key").fill(key);
  await page.getByRole("button", { name: "Connect" }).click();
  await expect(page.getByRole("heading", { name: "Traces" })).toBeVisible();
  const rows = page.locator("tbody tr");
  await expect(rows.first()).toBeVisible();
  await page.screenshot({ path: "test-results/01-traces.png", fullPage: true });

  // A support-agent v1 trace with findings: the retry loop.
  await page.getByLabel("Agent").fill("support-agent");
  await page.getByLabel("Version").fill("v1");
  const withFindings = rows.filter({ has: page.locator("td:nth-child(4) span", { hasText: /^[1-9]/ }) });
  await withFindings.first().getByRole("link").first().click();
  await expect(page.getByRole("heading", { name: /Findings/ })).toBeVisible();
  await expect(page.getByRole("tree", { name: "Spans" })).toBeVisible();
  await page.getByRole("button", { name: /retried|identical arguments|steps/ }).first().click();
  await expect(page.getByText("Evidence highlighted below")).toBeVisible();
  await page.getByRole("treeitem").filter({ hasText: "UpstreamTimeout" }).first().click();
  await expect(page.getByText("Side effect", { exact: true })).toBeVisible();
  await page.screenshot({ path: "test-results/02-trace.png", fullPage: true });

  await page.getByRole("link", { name: "View issue →" }).first().click();
  await expect(page.getByRole("heading", { name: "History" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Resolve…" })).toBeEnabled();
  await page.screenshot({ path: "test-results/03-issue.png", fullPage: true });

  await page.getByRole("link", { name: "Issues", exact: true }).click();
  // Assert on the page itself: the issue detail page also has table rows.
  await expect(page.getByRole("heading", { level: 1, name: "Issues", exact: true })).toBeVisible();
  await expect(page.getByRole("tab", { name: "open" })).toHaveAttribute("aria-selected", "true");
  await expect(page.locator("tbody tr").first()).toBeVisible();
  await page.screenshot({ path: "test-results/04-issues.png", fullPage: true });
});
