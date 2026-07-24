const {expect, test} = require("@playwright/test");

async function waitForExplorer(page) {
  await page.goto("/site/");
  await expect(page.locator("#results-count")).toContainText("matches", {timeout: 90_000});
  await expect(page.locator("#match-results")).toHaveAttribute("aria-busy", "false");
}

test("release summary and data-derived discovery controls load", async ({page}) => {
  await waitForExplorer(page);

  await expect(page.locator("#release-status")).toContainText("Preview release");
  await expect(page.locator("#summary-matches")).not.toHaveText("—");
  await expect(page.locator("#summary-players")).not.toHaveText("—");
  await expect(page.locator("#summary-tournaments")).not.toHaveText("—");
  await expect(page.locator("#results-table tr").first()).toBeVisible();

  const player = page.locator("#player-filter");
  await player.focus();
  const playerOptions = page.locator("#player-options .combo-option");
  await expect(playerOptions.first()).toBeVisible();
  expect(await playerOptions.count()).toBeGreaterThan(100);
  await player.press("ArrowDown");
  await player.press("Enter");
  await expect(player).not.toHaveValue("");
  await expect(page.locator("#filter-note")).toContainText("active filter");

  await page.locator("#player-combobox .combo-clear").click();
  await expect(page.locator("#filter-note")).toContainText("Explore all");
  const tournament = page.locator("#tournament-filter");
  await tournament.focus();
  const tournamentOptions = page.locator("#tournament-options .combo-option");
  await expect(tournamentOptions.first()).toBeVisible();
  expect(await tournamentOptions.count()).toBeGreaterThan(20);
  await tournament.press("ArrowDown");
  await tournament.press("Enter");
  await expect(tournament).not.toHaveValue("");
});

test("facets, sorting, pagination, fixtures, and clear-all work together", async ({page}) => {
  await waitForExplorer(page);

  await page.locator('#tour-filter [data-value="atp"]').click();
  await page.locator("#year-filter").selectOption("2025");
  await expect(page.locator("#filter-note")).toContainText("2 active filters");
  await expect(page.locator("#results-table tr").first()).toBeVisible();

  await page.locator("#sort-filter").selectOption("oldest");
  await expect(page.locator("#results-context")).toContainText("page 1");

  await page.locator("#clear-filters").click();
  await expect(page.locator("#filter-note")).toContainText("Explore all");
  await expect(page.locator("#next-page")).toBeEnabled();
  await page.locator("#next-page").click();
  await expect(page.locator("#page-info")).toContainText("Page 2");

  await page.locator("#lifecycle-filter").selectOption("fixture");
  await expect(page.locator("#results-count")).toContainText("matches");
  await expect(page.locator("#results-table")).toContainText("Fixture");
  await expect(page.locator("#results-table")).toContainText("TBD");
});

test("mobile results use readable match cards", async ({page}) => {
  await page.setViewportSize({width: 390, height: 844});
  await waitForExplorer(page);

  await expect(page.locator(".matches-table")).toBeHidden();
  await expect(page.locator("#match-cards")).toBeVisible();
  await expect(page.locator("#match-cards .match-card").first()).toBeVisible();
  await expect(page.locator("#match-cards .match-card-players").first()).toBeVisible();
});

test("manifest failures produce a retryable error state", async ({page}) => {
  await page.route("**/site/data/manifest.json", route => route.fulfill({
    status: 503,
    contentType: "application/json",
    body: "{}",
  }));
  await page.goto("/site/");
  await expect(page.locator("#error-state")).toBeVisible();
  await expect(page.locator("#error-message")).toContainText("HTTP 503");
  await expect(page.locator("#retry-button")).toBeVisible();
});
