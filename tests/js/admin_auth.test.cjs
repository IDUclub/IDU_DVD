const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

const reply = (status, data = {}) => ({ status, ok: status < 400, headers: { get: () => "application/json" }, json: async () => data });
const tick = () => new Promise(setImmediate);

function app(handler, uploadStatuses = []) {
  const calls = [], uploads = [], timers = new Map(), elements = new Map();
  let timerId = 0;
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, {
      open: false, listeners: {}, textContent: "",
      showModal() { this.open = true; }, close() { this.open = false; },
      addEventListener(name, listener) { this.listeners[name] = listener; },
      querySelector: get,
    });
    return elements.get(id);
  };
  class XHR {
    constructor() { this.listeners = {}; this.upload = { addEventListener() {} }; }
    open(method, url) { this.method = method; this.url = url; }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    send(form) {
      uploads.push({ method: this.method, url: this.url, form });
      this.status = uploadStatuses.shift(); this.response = this.status === 200 ? { job_id: "job" } : { detail: "denied" };
      this.listeners.load();
    }
  }
  const context = vm.createContext({
    URLSearchParams, XMLHttpRequest: XHR,
    document: { querySelector: get, addEventListener() {} },
    window: {
      setTimeout(fn, delay) { timers.set(++timerId, { fn, delay }); return timerId; },
      clearTimeout(id) { timers.delete(id); },
    },
    fetch: async (url, options) => { calls.push({ url, options }); return handler(url, options); },
  });
  const run = (code) => vm.runInContext(code, context);
  run(readFileSync("src/admin_service/static/admin.js", "utf8"));
  return { run, calls, uploads, timers, get,
    login: () => run("adminSession.login({ username: 'admin', password: 'secret' })"),
    request: () => run("request('/library/documents/doc', { method: 'PATCH', body: 'same-body' })"),
  };
}

test("login schedules renewal before expiry, including short-lived tokens", async () => {
  for (const seconds of [300, 20]) {
    const a = app(() => reply(200, { expires_in: seconds }));
    await a.login();
    const timer = [...a.timers.values()][0];
    assert.equal(timer.delay, seconds === 300 ? 270000 : 10000);
    timer.fn(); await tick();
    assert.equal(a.calls.length, 2);
    assert.equal(a.calls[1].url, "/admin/ui/session");
    assert.equal(a.calls[1].options.body.get("password"), "secret");
    assert.equal(a.timers.size, 1);
  }
});

test("an expired token after browser sleep is renewed before sending a mutation", async () => {
  const a = app((url) => reply(200, url.includes("session") ? { expires_in: 300 } : { saved: true }));
  await a.login(); a.run("adminSession.expiresAt = 1");
  assert.equal((await a.request()).saved, true);
  assert.deepEqual(a.calls.map((c) => c.url), ["/admin/ui/session", "/admin/ui/session", "/library/documents/doc"]);
});

test("parallel 401 responses share a renewal and each mutation is retried once", async () => {
  let loginCount = 0, finishRefresh;
  const a = app((url) => {
    if (url.includes("session")) {
      if (++loginCount === 1) return reply(200, { expires_in: 300 });
      return new Promise((resolve) => { finishRefresh = () => resolve(reply(200, { expires_in: 300 })); });
    }
    return reply(finishRefresh ? 200 : 401, { saved: true });
  });
  await a.login();
  const first = a.request(), second = a.request();
  await tick(); finishRefresh(); await Promise.all([first, second]);
  assert.equal(loginCount, 2);
  const mutations = a.calls.filter((c) => !c.url.includes("session"));
  assert.equal(mutations.length, 4);
  assert.ok(mutations.every((c) => c.options.method === "PATCH" && c.options.body === "same-body"));
});

test("a late 401 from the old token reuses the completed renewal", async () => {
  let late, mutationCount = 0;
  const a = app((url) => {
    if (url.includes("session")) return reply(200, { expires_in: 300 });
    if (++mutationCount === 1) return new Promise((resolve) => { late = resolve; });
    return reply(mutationCount === 2 ? 401 : 200, { saved: true });
  });
  await a.login(); const old = a.request(); await a.request();
  late(reply(401)); await old;
  assert.equal(a.calls.filter((c) => c.url.includes("session")).length, 2);
});

test("401 after a successful renewal stops retries and requests login", async () => {
  const a = app((url) => url.includes("session") ? reply(200, { expires_in: 300 }) : reply(401, { detail: "denied" }));
  await a.login(); await assert.rejects(a.request(), /denied/);
  assert.equal(a.calls.length, 4);
  assert.equal(a.run("adminSession.credentials"), null);
  assert.equal(a.get("#session-login").open, true);
});

test("a page reopened without in-memory credentials requests login on 401", async () => {
  const a = app(() => reply(401));
  await assert.rejects(a.request(), /Войдите снова/);
  assert.equal(a.calls.length, 1);
  assert.equal(a.get("#session-login").open, true);
});

test("403 for an operation does not renew or replay it", async () => {
  const a = app((url) => url.includes("session") ? reply(200, { expires_in: 300 }) : reply(403, { detail: "forbidden" }));
  await a.login(); await assert.rejects(a.request(), /forbidden/);
  assert.equal(a.calls.length, 2);
});

test("a rejected helper login clears credentials; a helper outage permits retry", async () => {
  for (const status of [401, 403, 502, 503]) {
    let loggedIn = false;
    const a = app(() => {
      if (!loggedIn) { loggedIn = true; return reply(200, { expires_in: 300 }); }
      return reply(status, { detail: "unavailable" });
    });
    await a.login(); [...a.timers.values()][0].fn(); await tick();
    assert.equal(a.run("Boolean(adminSession.credentials)"), status >= 500);
    assert.equal(a.get("#session-login").open, status < 500);
    if (status >= 500) assert.ok([...a.timers.values()].some((timer) => timer.delay === 15000));
  }
});

test("uploads replay the same form and method once after a 401", async () => {
  const a = app(() => reply(200, { expires_in: 300 }), [401, 200]);
  await a.login();
  assert.equal((await a.run("uploadRequest('/documents/doc', 'PUT', { file: 'docx' }, () => {})")).job_id, "job");
  assert.equal(a.uploads.length, 2);
  assert.equal(a.uploads[0].form, a.uploads[1].form);
  assert.ok(a.uploads.every((u) => u.method === "PUT" && u.url === "/documents/doc"));
  assert.equal(a.calls.length, 2);
});

test("uploads stop after a second 401 and never replay other errors", async () => {
  for (const statuses of [[401, 401], [403], [500]]) {
    const expected = statuses.length;
    const a = app(() => reply(200, { expires_in: 300 }), [...statuses]);
    await a.login();
    await assert.rejects(a.run("uploadRequest('/documents', 'POST', {}, () => {})"), /denied/);
    assert.equal(a.uploads.length, expected);
  }
});

test("manual reauthentication closes the dialog, clears fields and resumes renewal", async () => {
  const a = app(() => reply(200, { expires_in: 300 }));
  const form = a.get("#login-form");
  form.elements = { username: { value: "admin" }, password: { value: "secret" } };
  form.reset = () => { form.elements.password.value = ""; };
  a.get("#session-login").open = true;
  a.run("bindLogin()");
  await form.listeners.submit({ preventDefault() {}, currentTarget: form });
  assert.equal(a.get("#session-login").open, false);
  assert.equal(form.elements.password.value, "");
  assert.equal(a.run("adminSession.credentials.password"), "secret");
});
