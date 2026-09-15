const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

// Minimal DOM boundary; all rendering, fetching and click handling use the real admin.js.
class Element {
  constructor() {
    this.children = []; this.textContent = ""; this.listeners = {};
    this.classList = { add() {}, remove() {}, toggle() {} };
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(event, callback) { this.listeners[event] = callback; }
  get text() { return this.textContent + this.children.map((child) => child.text).join(" "); }
}

function app(fetcher) {
  const elements = new Map();
  const get = (id) => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const context = vm.createContext({
    document: { createElement: () => new Element(), querySelector: get, querySelectorAll: () => [], addEventListener() {} },
    window: { setTimeout() {} }, location: { hash: "" }, confirm: () => true,
    fetch: async (url, options) => ({ ok: true, headers: { get: () => "application/json" }, json: async () => fetcher(url, options) }),
    console: { error() {} },
  });
  vm.runInContext(readFileSync("src/admin_service/static/admin.js", "utf8"), context);
  vm.runInContext("var documentReloads = 0; loadDocuments = async () => { documentReloads++; };", context);
  return { context, get, run: (code) => vm.runInContext(code, context) };
}

const job = (i, status = "queued") => ({
  job_id: `j${i}`, name: `Document ${i}`, operation: "reparse", status,
  created_at: new Date(Date.UTC(2026, 0, 1, 0, 0, i)).toISOString(),
  queue_position: i, version_total: 2, version_index: status === "queued" ? 0 : 1,
  stage: status === "queued" ? "queued" : "embeddings", overall_progress: 50,
});

test("bulk button shows every queued document, running first, and opens the queue", async () => {
  const active = [job(0, "processing"), ...Array.from({ length: 24 }, (_, i) => job(i + 1))];
  const a = app((url, options) => {
    if (url === "/documents/reparse") {
      assert.equal(options.method, "POST");
      return { queued_documents: 25, queued_versions: 50, skipped: [{ name: "Missing", version: "v1", reason: "Нет исходника" }] };
    }
    return { jobs: url.includes("recent") ? [...active].reverse().slice(0, 20) : [...active].reverse() };
  });
  await a.run("reparseAllDocuments()");
  assert.equal(a.get("#jobs-list").children.length, 25);
  assert.match(a.get("#jobs-list").children[0].text, /Document 0 /);
  assert.match(a.get("#jobs-list").children[1].text, /Document 1 /);
  assert.match(a.get("#overview-jobs").children[0].text, /Document 0 /);
  assert.equal(a.get("#jobs-badge").textContent, "25");
  assert.match(a.get("#reparse-result").text, /документов: 25, версий: 50/);
  assert.match(a.get("#reparse-result").text, /Missing.*Нет исходника/);
  a.get("#reparse-result").children.at(-1).listeners.click();
  assert.equal(a.context.location.hash, "jobs");
  assert.equal(a.get("#reparse-all").disabled, false);
  assert.doesNotMatch(a.get("#jobs-list").children[1].text, /версия 0/);
});

test("completion outside the 20 recent jobs refreshes the library and retains the result", async () => {
  let finished = false;
  const a = app((url) => {
    if (url === "/documents/j0") return { ...job(0, "done"), version_index: 2 };
    if (url.includes("recent")) return { jobs: Array.from({ length: 20 }, (_, i) => job(i + 1)) };
    return { jobs: finished ? [] : [job(0, "processing")] };
  });
  await a.run("loadJobs()"); finished = true;
  await a.run("loadJobs()");
  assert.equal(a.run("documentReloads"), 1);
  assert.equal(a.run("state.jobs.find(j => j.job_id === 'j0').status"), "done");
  assert.equal(a.run("overallProgress(state.jobs.find(j => j.job_id === 'j0'))"), 100);
});

test("an older poll cannot overwrite a newer completed status", async () => {
  const resolvers = [];
  const a = app(() => new Promise((resolve) => resolvers.push(resolve)));
  const oldPoll = a.run("loadJobs()");
  const newPoll = a.run("loadJobs()");
  await new Promise(setImmediate);
  resolvers[2]({ jobs: [job(0, "done")] }); resolvers[3]({ jobs: [] });
  await newPoll;
  resolvers[0]({ jobs: [job(0)] }); resolvers[1]({ jobs: [job(0)] });
  await oldPoll;
  assert.equal(a.run("state.jobs[0].status"), "done");
  assert.equal(a.get("#jobs-badge").textContent, "0");
});

test("edition progress and errors remain visible", () => {
  const a = app(() => ({}));
  assert.equal(a.run("overallProgress({operation:'reparse', status:'queued', version_total:2, version_index:0})"), 0);
  assert.equal(a.run("overallProgress({operation:'reparse', status:'processing', version_total:2, version_index:2, overall_progress:50})"), 75);
  assert.match(a.run("renderJob({operation:'reparse', status:'error', error:'Ошибка парсинга', version_total:2, version_index:2}).text"), /Ошибка парсинга/);
});

test("slow polls still update while the next poll is pending", async () => {
  const resolvers = [];
  const a = app(() => new Promise((resolve) => resolvers.push(resolve)));
  const first = a.run("loadJobs()");
  const next = a.run("loadJobs()");
  await new Promise(setImmediate);
  resolvers[0]({ jobs: [job(0, "processing")] }); resolvers[1]({ jobs: [job(0, "processing")] });
  await first;
  assert.equal(a.get("#jobs-badge").textContent, "1");
  resolvers[2]({ jobs: [job(0, "done")] }); resolvers[3]({ jobs: [] });
  await next;
  assert.equal(a.get("#jobs-badge").textContent, "0");
});

test("poll failure is visible and preserves the last received queue", async () => {
  let broken = false;
  const a = app(() => { if (broken) throw new Error("Connection lost"); return { jobs: [job(0)] }; });
  await a.run("loadJobs()"); broken = true;
  await a.run("loadJobs()");
  assert.equal(a.get("#jobs-list").children.length, 1);
  assert.match(a.get("#jobs-error").text, /Не удалось обновить очередь.*Connection lost/);
});
