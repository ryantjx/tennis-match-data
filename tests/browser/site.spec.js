const {expect, test} = require("@playwright/test");

test("search and SQL explorer work against published Parquet", async ({page}) => {
  await page.goto("/site/");
  await expect(page.locator("#search-status")).toContainText("Ready", {timeout: 90_000});

  await page.locator("#search-year").fill("2025");
  await page.locator("#search-level").selectOption("grand_slam");
  await page.locator("#search-button").click();
  await expect(page.locator("#search-status")).toContainText("ATP 2025", {timeout: 90_000});
  await expect(page.locator("#search-results tbody tr").first()).toBeVisible();
  await expect(page.locator("#search-results thead")).toContainText("Player/Team 1");
  await expect(page.locator("#search-pagination")).toBeVisible();
  if (await page.locator("#search-next").isEnabled()) {
    await page.locator("#search-next").click();
    await expect(page.locator("#search-page-info")).toContainText("Page 2");
  }

  await page.locator("#search-kind").selectOption("fixtures");
  await page.locator("#search-year").fill("2026");
  await page.locator("#search-level").selectOption("");
  await page.locator("#search-button").click();
  await expect(page.locator("#search-status")).toContainText("future", {timeout: 90_000});
  await expect(page.locator("#search-results tbody tr").first()).toBeVisible();
  await expect(page.locator("#search-results thead")).toContainText("Status");
  const source = page.locator("#search-results a", {hasText: "Source"}).first();
  if (await source.count()) await expect(source).toHaveAttribute("rel", "noopener noreferrer");

  await page.locator("#explorer-tab").click();
  await expect(page.locator("#explorer-panel")).toBeVisible();
  await page.locator("#explorer-year").fill("2025");
  await page.locator("#load-table").click();
  await expect(page.locator("#query-status")).toContainText("Loaded data/", {timeout: 90_000});
  await expect(page.locator("#schema-list .schema-row").first()).toBeVisible();

  await page.locator("#load-example").click();
  await page.locator("#run-query").click();
  await expect(page.locator("#query-status")).toContainText("rows", {timeout: 90_000});
  await expect(page.locator("#query-results tbody tr").first()).toBeVisible();

  await page.locator("#sql").fill("DELETE FROM matches");
  await page.locator("#run-query").click();
  await expect(page.locator("#query-status")).toContainText("read-only SELECT or WITH");

  await page.locator("#search-tab").click();
  await expect(page.locator("#search-panel")).toBeVisible();
});
