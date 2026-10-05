const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tag) { this.tag = tag; this.value = ""; this.children = []; this.listeners = {}; this.attributes = {}; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  addEventListener(event, callback) { this.listeners[event] = callback; }
  setAttribute(name, value) { this.attributes[name] = value; }
}

const texts = (item) => [item.textContent, ...item.children.flatMap((child) => texts(child))].filter(Boolean);
const find = (item, text) => item.textContent === text ? item : item.children.map((child) => find(child, text)).find(Boolean);

function app() {
  const elements = new Map(), requests = [];
  const get = (id) => { if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id); };
  const context = vm.createContext({
    document: { createElement: (tag) => new Element(tag), querySelector: get, querySelectorAll: () => [], addEventListener() {} },
    window: { setTimeout() { return 0; }, clearTimeout() {} },
    console: { error() {} },
    fetch: async (url, options) => {
      requests.push({ url, options });
      return { ok: true, headers: { get: () => "application/json" }, json: async () => ({ name: "ПЗЗ", amendments: [], amends: null, editions: {} }) };
    },
  });
  const run = (code) => vm.runInContext(code, context);
  run(readFileSync("src/admin_service/static/admin.js", "utf8"));
  run("loadJobs = async () => {}; state.current = { row: { name: 'ПЗЗ', version: '2019' } };");
  return { run, get, requests };
}

const overview = {
  name: "ПЗЗ",
  amends: null,
  editions: {
    "2019": { status: "superseded", consolidated: false, superseded_by: "2019 (ред. от 20.11.2023)" },
    "2019 (ред. от 20.11.2023)": { status: "active", consolidated: true, amended_by: ["Приказ № 170"], review_required: true },
  },
  amendments: [{
    name: "Приказ № 170", kind: "amends", effective_date: "2023-11-20", status: "partial", detected: true,
    results: [
      { item: "1.1", action: "insert", scope: ["Статья 17.1", "Ж-2.15"], status: "applied", reason: "" },
      { item: "2", action: "insert", scope: ["Приложение"], status: "failed", reason: "не найдено место: Приложение" },
    ],
  }],
};

test("the editions tab shows the current edition, the acts and every failed operation", () => {
  const a = app();
  a.run(`renderEditions(${JSON.stringify(overview)})`);
  const shown = texts(a.get("#editions-panel")).join("\n");
  assert.match(shown, /2019 \(ред\. от 20\.11\.2023\) · действующая/);
  assert.match(shown, /2019 · заменена/);
  assert.match(shown, /собрана из: Приказ № 170/);
  assert.match(shown, /требует проверки/);
  assert.match(shown, /Изменение: Приказ № 170/);
  assert.match(shown, /от 2023-11-20 · применено частично · связано по заголовку/);
  assert.match(shown, /п\. 2: insert · Приложение — не найдено место: Приложение/);
});

test("linking and rebuilding send the right requests", async () => {
  const a = app();
  a.run(`renderEditions(${JSON.stringify(overview)})`);
  const panel = a.get("#editions-panel");
  const controls = panel.children.flatMap((child) => child.children || []).flatMap((child) => child.children || []);
  controls.find((item) => item.id === "link-target").value = "Правила";
  controls.find((item) => item.id === "link-kind").value = "explains";
  await find(panel, "Связать").listeners.click();
  const put = a.requests.find((r) => r.options?.method === "PUT");
  assert.equal(put.url, "/documents/%D0%9F%D0%97%D0%97/amends");
  assert.deepEqual(JSON.parse(put.options.body), { target: "Правила", kind: "explains" });
  await find(panel, "Перечитать изменения").listeners.click();
  assert.ok(a.requests.some((r) => r.options?.method === "POST" && r.url.endsWith("/consolidate?reextract=true")));
});

test("an act shows what it changes and can be unlinked", async () => {
  const a = app();
  a.run(`renderEditions(${JSON.stringify({ name: "ПЗЗ", amendments: [], editions: {}, amends: { name: "ПЗЗ", target: "Правила", kind: "amends", status: "applied", results: [] } })})`);
  const panel = a.get("#editions-panel");
  assert.match(texts(panel).join("\n"), /Изменение документа «Правила»/);
  await find(panel, "Отвязать").listeners.click();
  assert.ok(a.requests.some((r) => r.options?.method === "DELETE" && r.url.endsWith("/amends")));
});
