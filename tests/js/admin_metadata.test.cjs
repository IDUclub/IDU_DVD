const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

class Element {
  constructor() { this.value = ""; this.children = []; this.listeners = {}; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  addEventListener(event, callback) { this.listeners[event] = callback; }
}

const district = { territory_id: 54, name: "Выборгский район", parent_name: "Ленинградская область" };
const label = (item) => item.parent_name ? `${item.name} — ${item.parent_name}` : item.name;

function app(search = async () => [district]) {
  const elements = new Map(), timers = new Map(), requests = [];
  let nextTimer = 0;
  const get = (id) => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const context = vm.createContext({
    document: { createElement: () => new Element(), querySelector: get, querySelectorAll: () => [], addEventListener() {} },
    window: { setTimeout(fn) { timers.set(++nextTimer, fn); return nextTimer; }, clearTimeout(id) { timers.delete(id); } },
    console: { error() {} },
    fetch: async (url, options) => {
      requests.push({ url, options });
      const data = url.includes("/territories?")
        ? { territories: await search(new URL(url, "http://test").searchParams.get("query")) }
        : { fields_updated: ["title"] };
      return { ok: true, headers: { get: () => "application/json" }, json: async () => data };
    },
  });
  const run = (code) => vm.runInContext(code, context);
  run(readFileSync("src/admin_service/static/admin.js", "utf8"));
  run("loadDocuments = async () => {}; state.current = { detail: { doc_id: 'doc', title: 'Old', territory_id: 1, territory_name: 'Old territory' } };");
  for (const [key, value] of Object.entries({ title: "New title", "doc-type": "document", corpus: "default", status: "active", territory: "Old territory" })) get(`#meta-${key}`).value = value;
  run("bindTerritoryLookup('#meta-territory', '#meta-territory-options'); bindTerritoryLookup('#upload-territory', '#territory-options');");
  return {
    run, get, requests,
    input(value, id = "#meta-territory") { const input = get(id); input.value = value; input.listeners.input({ target: input }); },
    async tick() { const pending = [...timers.values()]; timers.clear(); await Promise.all(pending.map((fn) => fn())); },
    save: () => run("saveMetadata({ preventDefault() {} })"),
    patches: () => requests.filter((r) => r.options?.method === "PATCH").map((r) => JSON.parse(r.options.body)),
  };
}

test("a selected territory survives the debounce and a later title edit", async () => {
  const a = app(async (query) => query.includes(" — ") ? [] : [district]);
  a.input("Выборг"); await a.tick();
  a.input(label(district)); await a.tick();
  assert.equal(a.run("selectedTerritoryId('#meta-territory')"), 54);
  await a.save();
  assert.equal(a.patches()[0].territory_id, 54);
  a.get("#meta-title").value = "Second title";
  await a.save();
  assert.equal(a.patches()[1].title, "Second title");
  assert.equal(Object.hasOwn(a.patches()[1], "territory_id"), false);
  assert.equal(a.requests.filter((r) => r.url.includes("/territories?")).length, 1);
});

test("title-only edits never send territory_id or name", async () => {
  const a = app(); await a.save();
  assert.equal(a.patches()[0].title, "New title");
  assert.equal(Object.hasOwn(a.patches()[0], "territory_id"), false);
  assert.equal(Object.hasOwn(a.patches()[0], "name"), false);
  assert.equal(a.requests.filter((r) => r.url.includes("/territories?")).length, 0);
});

test("clearing a territory is sent once; unselected text blocks saving", async () => {
  const a = app(); a.input(""); await a.tick(); await a.save();
  assert.equal(a.patches()[0].territory_id, null);
  await a.save();
  assert.equal(Object.hasOwn(a.patches()[1], "territory_id"), false);
  a.input("Несуществующая территория"); await a.tick(); await a.save();
  assert.equal(a.patches().length, 2);
  assert.match(a.get("#toast").textContent, /Выберите территорию/);
});

test("a late result cannot replace a newer selected territory", async () => {
  let resolveOld;
  const a = app((query) => query === "старый" ? new Promise((resolve) => { resolveOld = resolve; }) : [district]);
  a.input("старый"); const oldLookup = a.tick();
  a.input("Выборг"); await a.tick(); a.input(label(district));
  resolveOld([]); await oldLookup;
  assert.equal(a.run("selectedTerritoryId('#meta-territory')"), 54);
  await a.save(); assert.equal(a.patches()[0].territory_id, 54);
});

test("upload and document territory controls keep independent selections", async () => {
  const other = { territory_id: 2, name: "Other", parent_name: "Region" };
  const a = app(async (query) => query === "Other" ? [other] : [district]);
  a.input("Выборг"); await a.tick(); a.input(label(district));
  a.input("Other", "#upload-territory"); await a.tick();
  a.input(label(other), "#upload-territory"); await a.tick();
  assert.equal(a.run("selectedTerritoryId('#meta-territory')"), 54);
  assert.equal(a.run("selectedTerritoryId('#upload-territory')"), 2);
});

test("territory search failures are visible to the user", async () => {
  const a = app(async () => { throw new Error("Urban API unavailable"); });
  a.input("Выборг"); await a.tick();
  assert.match(a.get("#toast").textContent, /Urban API unavailable/);
});
