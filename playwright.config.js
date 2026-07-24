const {defineConfig} = require("@playwright/test");

module.exports = defineConfig({
  testDir: "tests/browser",
  timeout: 120_000,
  expect: {timeout: 30_000},
  retries: process.env.CI ? 1 : 0,
  use: {
    baseURL: "http://127.0.0.1:8000/site/",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "python3 -m http.server 8000 --directory .",
    url: "http://127.0.0.1:8000/site/",
    reuseExistingServer: !process.env.CI,
  },
});
