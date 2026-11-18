/* Run against ui-preview.js with Playwright available on NODE_PATH. No provider calls. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const base = process.env.ALYSIS_UI_URL || "http://127.0.0.1:4317";
const output = path.resolve(__dirname, "../../../../qa_reports/vscode_ui_finish");
const scenarios = ["welcome", "setup", "chat", "running", "streaming", "thinking-start", "stopping", "error", "approved", "denied", "expired", "models", "settings", "history", "history-empty", "browser"];
const sizes = [{ width: 260, height: 620 }, { width: 360, height: 800 }, { width: 900, height: 700 }, { width: 360, height: 420 }];

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const errors = [];
  const checks = [];
  page.on("pageerror", (error) => errors.push(error.message));
  async function open(scenario, theme = "dark") {
    await page.goto(`${base}/?scenario=${scenario}&theme=${theme}`);
    const surface = scenario === "history-empty" ? "history"
      : ["models", "settings", "history", "browser"].includes(scenario) ? scenario : "task";
    await page.locator(`#${surface}Surface`).waitFor({ state: "visible" });
    if (surface === "task") await page.locator(["welcome", "setup"].includes(scenario) ? "#welcome" : "#conversation").waitFor({ state: "visible" });
  }
  async function geometry(label) {
    const issues = await page.evaluate(() => {
      const issues = [];
      for (const element of document.querySelectorAll("button,input,textarea,select,summary,h1,h2")) {
        if (!element.checkVisibility() || element.closest(".sr-only")) continue;
        const rect = element.getBoundingClientRect();
        if (rect.left < -1 || rect.right > innerWidth + 1) issues.push(`${element.id || element.className}: horizontal overflow`);
      }
      if (document.documentElement.scrollWidth > innerWidth) issues.push("Root horizontal overflow");
      const input = document.querySelector("#taskInput");
      if (input.checkVisibility() && input.getBoundingClientRect().bottom > innerHeight) issues.push("Composer clipped");
      return issues;
    });
    checks.push({ label, issues });
    assert.deepEqual(issues, [], label);
  }
  try {
    for (const theme of ["dark", "light", "contrast"]) {
      for (const size of sizes) {
        await page.setViewportSize(size);
        for (const scenario of scenarios) {
          await open(scenario, theme);
          await geometry(`${theme}/${size.width}x${size.height}/${scenario}`);
          if (size.width === 360 && size.height === 800) await page.screenshot({ path: path.join(output, `${theme}-${scenario}.png`) });
        }
      }
    }
    await page.setViewportSize({ width: 260, height: 420 });
    const draft = "A detailed task\nwith many lines\nMore context\nAnother detail\nOne more\nKeep everything visible\nAnd this line";
    for (const [trigger, palette] of [["#permissionStrip", "#permissionPalette"], ["#modelButton", "#modelPalette"], ["#personaButton", "#personaPalette"]]) {
      await open("welcome", "light");
      await page.locator("#taskInput").fill(draft);
      await page.locator(trigger).click();
      const bounds = await page.locator(palette).boundingBox();
      assert.ok(bounds.y >= 0 && bounds.y + bounds.height <= 420, `${palette} stays inside the short viewport`);
      await page.locator(`${palette} button`).last().scrollIntoViewIfNeeded();
      await geometry(`long draft/${palette}`);
      assert.equal(await page.locator("#taskInput").inputValue(), draft);
      await page.screenshot({ path: path.join(output, `short-${palette.slice(1)}.png`) });
    }
    await page.setViewportSize({ width: 360, height: 800 });
    await open("settings");
    await page.locator("#settingsSearch").fill("safety");
    assert.equal(await page.locator(".settings-group:not(.settings-filtered)").count(), 1);
    await page.locator("#settingsSearch").fill("no-matching-setting");
    assert.equal(await page.locator("#settingsEmpty").isVisible(), true);
    await page.locator("#settingsClear").click();
    assert.equal(await page.locator("#settingsSearch").inputValue(), "");
    await open("models");
    await page.locator(".provider-head").first().click();
    assert.equal(await page.locator(".provider-head").first().evaluate((node) => node === document.activeElement), true);
    await page.locator(".connection-more").focus();
    await page.keyboard.press("ArrowDown");
    assert.equal(await page.evaluate(() => document.activeElement.textContent), "Replace key");
    await page.keyboard.press("ArrowDown");
    assert.equal(await page.evaluate(() => document.activeElement.textContent), "Remove key");
    await page.keyboard.press("Escape");
    assert.equal(await page.locator(".connection-more").evaluate((node) => node === document.activeElement), true);
    for (const scenario of ["welcome", "chat", "models", "settings", "browser"]) {
      await open(scenario);
      await page.evaluate(() => document.documentElement.style.setProperty("--vscode-font-size", "20px"));
      await geometry(`larger font/${scenario}`);
    }
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, "acceptance.json"), JSON.stringify({ passed: true, checks, errors }, null, 2));
    console.log(`UI acceptance passed: ${checks.length} layout checks, short-window menus, settings and provider keyboard journeys.`);
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
