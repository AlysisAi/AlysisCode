/* Capture the production renderer with disposable fixture data for the listing.
 * Start ui-preview.js first. These images are UI illustrations, not provider,
 * native VS Code, sandbox, or signed-install acceptance evidence. */
"use strict";
const { chromium } = require("playwright");
const fs = require("node:fs");
const path = require("node:path");
const base = process.env.ALYSIS_UI_URL || "http://127.0.0.1:4317";
const output = path.resolve(__dirname, "../../resources/screenshots");

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 520, height: 840 }, deviceScaleFactor: 1 });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/fixture.js", async (route) => {
      const response = await route.fetch();
      const body = (await response.text())
        .replace("additions: 17083, deletions: 1453, files: 24, untracked: 2", "additions: 3, deletions: 2, files: 1, untracked: 0")
        .replaceAll("gpt-5.6-sol", "deepseek-chat").replaceAll("gpt-5.4-mini", "deepseek-reasoner")
        .replaceAll("OpenAI", "DeepSeek").replaceAll("openai", "deepseek");
      await route.fulfill({ response, body });
    });
    for (const [name, scenario, surface] of [
      ["conversation", "approved", "task"], ["setup", "setup", "task"],
      ["review", "chat", "task"],
      ["settings", "settings", "settings"]
    ]) {
      await page.goto(`${base}/?scenario=${scenario}&theme=dark`);
      await page.locator(`#${surface}Surface`).waitFor({ state: "visible" });
      await page.evaluate(() => document.fonts.ready);
      await page.screenshot({ path: path.join(output, `${name}.png`), animations: "disabled" });
    }
    if (errors.length) throw new Error(errors.join("\n"));
    console.log("Captured four listing illustrations from the production renderer.");
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
