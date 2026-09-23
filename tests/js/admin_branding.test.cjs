const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {test} = require("node:test");
const vm = require("node:vm");

function setup(handler) {
  const elements = new Map();
  const get = (key) => {
    if (!elements.has(key)) elements.set(key, {listeners: {}, files: [], disabled: false,
      addEventListener(name, fn) {this.listeners[name] = fn;}});
    return elements.get(key);
  };
  const context = vm.createContext({FormData, Date,
    document: {querySelector: get, querySelectorAll: () => [get("brand")], addEventListener() {}},
    window: {setTimeout() {}},
  });
  vm.runInContext(readFileSync("src/admin_service/static/admin.js", "utf8"), context);
  context.handler = handler;
  vm.runInContext("request = handler; bindBranding();", context);
  return {get, select(file) {get("#logo-file").files = file ? [file] : []; return get("#logo-file").listeners.change();},
    save() {return get("#logo-form").listeners.submit({preventDefault() {}});}};
}

test("only the latest successfully prepared selection can be saved", async () => {
  const pending = [];
  const ui = setup(() => new Promise(resolve => pending.push(resolve)));
  const a = ui.select(new Blob(["old"]));
  const b = ui.select(new Blob(["new"]));
  pending[1]({preview: "new-preview"}); await b;
  pending[0]({preview: "old-preview"}); await a;
  assert.equal(ui.get("#logo-preview").src, "new-preview");
  assert.equal(ui.get("#logo-save").disabled, false);
  await ui.select(null);
  assert.equal(ui.get("#logo-save").disabled, true);
});

test("successful save refreshes sidebar and favicon; failed save keeps selection", async () => {
  let fail = true, calls = 0;
  const ui = setup(async (url) => {
    if (url.includes("preview")) return {preview: "prepared"};
    calls++;
    if (fail) throw new Error("Storage unavailable");
    return {url: "/admin/ui/logo.png"};
  });
  await ui.select(new Blob(["logo"]));
  await ui.save();
  assert.equal(ui.get("#logo-status").textContent, "Storage unavailable");
  assert.equal(ui.get("#logo-save").disabled, false);
  assert.equal(ui.get("#logo-file").disabled, false);
  assert.equal(ui.get("brand").src, undefined);
  fail = false; await ui.save();
  assert.match(ui.get("brand").src, /^\/admin\/ui\/logo.png\?v=/);
  assert.match(ui.get("#site-favicon").href, /^\/admin\/ui\/favicon.png\?v=/);
  assert.equal(ui.get("#logo-save").disabled, true);
  assert.equal(calls, 2);
});

test("oversized files never leave the browser and disable saving", async () => {
  const ui = setup(() => {throw new Error("unexpected request");});
  await ui.select({size: 5 * 1024 * 1024 + 1});
  assert.equal(ui.get("#logo-save").disabled, true);
  assert.match(ui.get("#logo-status").textContent, /5 МБ/);
});
