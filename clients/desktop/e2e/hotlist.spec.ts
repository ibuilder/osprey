import { expect, type APIRequestContext, test } from "@playwright/test";
import { API_PORT } from "../playwright.config";

const API = `http://127.0.0.1:${API_PORT}`;
const PASSWORD = "Sup3rSecret!pass";

// A forwarded email, exactly as the Forward-To path receives one.
const NOTICE = [
  "From: Site Super <super@gc.example>",
  "To: pm@owner.example",
  "Subject: NOTICE OF DELAY - differing site conditions at Tower B",
  "Message-ID: <e2e-notice-1@gc.example>",
  `Date: ${new Date().toUTCString()}`,
  "",
  "Pursuant to Section 8.3, this is formal notice of delay due to differing site conditions " +
    "discovered at the east foundation. A written response is required within 7 days or the " +
    "claim may be deemed waived. Estimated schedule and cost exposure $180,000.",
].join("\r\n");

/** Register an owner and put one forwarded notice into a project, through the API. */
async function ownerWithANotice(request: APIRequestContext) {
  const email = `e2e-${Date.now()}-${Math.round(Math.random() * 1e6)}@example.com`;
  const reg = await request.post(`${API}/auth/register`, {
    data: { email, password: PASSWORD, org_name: "E2E Builders" },
  });
  expect(reg.status(), await reg.text()).toBe(201);
  const headers = { Authorization: `Bearer ${(await reg.json()).access_token}` };

  const project = await request.post(`${API}/projects`, { headers, data: { name: "Tower B" } });
  expect(project.ok(), await project.text()).toBeTruthy();
  const connection = await request.post(`${API}/connections`, {
    headers,
    data: { project_id: (await project.json()).id, source_type: "filedrop", account_ref: "forward" },
  });
  expect(connection.ok(), await connection.text()).toBeTruthy();
  const forwarded = await request.post(`${API}/connections/${(await connection.json()).id}/forward`, {
    headers,
    data: { raw: NOTICE, kind: "email", source_kind: "email" },
  });
  expect(await forwarded.json()).toMatchObject({ created: 1 });
  return email;
}

async function signIn(page: import("@playwright/test").Page, email: string, password = PASSWORD) {
  await page.goto("/");
  // Outside the desktop shell there is no bundled backend, so the URL field shows.
  await page.getByPlaceholder("Backend URL").fill(API);
  await page.getByPlaceholder("Email").fill(email);
  await page.getByPlaceholder("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
}

test("a forwarded notice is ranked, explained, and the owner reaches the admin tools", async ({
  page,
  request,
}) => {
  const email = await ownerWithANotice(request);
  await signIn(page, email);

  // Refresh runs the pipeline, whichever state the first load found.
  await page.getByRole("button", { name: "Refresh" }).click();
  const card = page.locator(".card.item").filter({ hasText: "NOTICE OF DELAY" });
  await expect(card).toBeVisible();
  await expect(card).toHaveClass(/act_today/);
  await expect(card).toContainText("$180,000");

  // Real layout, which jsdom cannot measure: nothing forces a sideways scroll.
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);

  // Every item explains itself: factor breakdown and the cited source text.
  await card.click();
  await expect(page.getByText(/Why it ranked here/i)).toBeVisible();
  await expect(page.locator(".bar-row").first()).toBeVisible();
  const modal = page.locator(".modal");
  await expect(modal).toContainText("NOTICE DEADLINE");
  await expect(modal.locator(".cite")).toContainText("NOTICE OF DELAY");
  await page.getByRole("button", { name: "✕" }).click();
  await expect(page.locator(".modal")).toHaveCount(0);

  await page.getByRole("button", { name: "admin" }).click();
  const data = page.locator(".card").filter({ hasText: "Data and deletion" });
  await expect(data).toContainText("Type E2E Builders to confirm");
  await expect(data.getByRole("button", { name: "Delete organization" })).toBeDisabled();
});

test("a wrong password is refused with a message, not a blank screen", async ({ page, request }) => {
  const email = await ownerWithANotice(request);
  await signIn(page, email, "not-the-password");

  await expect(page.locator(".notice")).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
});
