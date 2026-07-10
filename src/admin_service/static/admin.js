"use strict";

const state = { documents: [], tags: [], jobs: [], current: null, fragment: null };
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

function toast(message, error = false) {
  const item = $("#toast");
  item.textContent = message;
  item.className = `toast show${error ? " error" : ""}`;
  window.setTimeout(() => { item.className = "toast"; }, 3200);
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  const type = response.headers.get("content-type") || "";
  const data = type.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = typeof data === "object" ? data.detail : data;
    throw new Error(detail || `HTTP ${response.status}`);
  }
  return data;
}

function showView(name) {
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  $$(".nav-link").forEach((link) => link.classList.toggle("active", link.dataset.view === name));
  $("#page-title").textContent = { overview: "Обзор", documents: "Документы", jobs: "Очередь обработки", settings: "Настройки парсинга" }[name];
  location.hash = name;
}

function tagsCell(tags) {
  const wrap = node("div");
  (tags || []).slice(0, 3).forEach((tag) => wrap.append(node("span", "tag", tag)));
  if ((tags || []).length > 3) wrap.append(node("span", "tag", `+${tags.length - 3}`));
  return wrap;
}

function renderDocuments() {
  const query = $("#doc-search").value.trim().toLowerCase();
  const tag = $("#tag-filter").value;
  const docs = state.documents.filter((doc) => {
    const haystack = `${doc.name} ${doc.version} ${(doc.tags || []).join(" ")}`.toLowerCase();
    return (!query || haystack.includes(query)) && (!tag || (doc.tags || []).includes(tag));
  });
  const body = $("#documents-body");
  body.replaceChildren();
  docs.forEach((doc) => {
    const row = node("tr");
    const name = node("td"); name.append(node("strong", "", doc.name)); name.append(node("small", "muted", doc.source || doc.doc_id));
    row.append(name, node("td", "", doc.version));
    const tags = node("td"); tags.append(tagsCell(doc.tags)); row.append(tags);
    row.append(node("td", "", String(doc.node_count || 0)), node("td", "", formatDate(doc.uploaded_at)));
    const actions = node("td", "row-actions");
    const open = node("button", "mini-button", "Открыть"); open.addEventListener("click", () => openDocument(doc));
    const update = node("button", "mini-button", "Новая версия"); update.addEventListener("click", () => openUpload("update", doc.name));
    actions.append(open, update); row.append(actions); body.append(row);
  });
  $("#documents-empty").classList.toggle("hidden", docs.length > 0);

  const recent = $("#recent-docs"); recent.replaceChildren();
  [...state.documents].sort((a, b) => String(b.uploaded_at).localeCompare(String(a.uploaded_at))).slice(0, 5).forEach((doc) => {
    const item = node("div", "compact-item"); item.append(node("strong", "", `${doc.name} · ${doc.version}`), node("span", "", formatDate(doc.uploaded_at))); recent.append(item);
  });
  if (!recent.children.length) recent.append(node("div", "empty", "Документов пока нет"));
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
}

function jobProgress(job) {
  if (job.progress_total) return Math.min(100, Math.round((job.progress || 0) / job.progress_total * 100));
  if (job.stage_total) return Math.min(96, Math.round(((job.stage_index || 1) - 1) / job.stage_total * 100));
  return job.status === "processing" ? 8 : 2;
}

function renderJob(job) {
  const item = node("div", "job");
  const head = node("div", "job-head");
  const left = node("div"); left.append(node("strong", "", job.name || job.filename || job.job_id), node("small", "", `${job.operation || "upload"} · ${job.stage || job.status}${job.phase ? ` · ${job.phase}` : ""}`));
  head.append(left, node("strong", "", `${jobProgress(job)}%`));
  const progress = node("progress", "native-progress"); progress.max = 100; progress.value = jobProgress(job);
  item.append(head, progress); return item;
}

function renderJobs() {
  const list = $("#jobs-list"); list.replaceChildren(); state.jobs.forEach((job) => list.append(renderJob(job)));
  $("#jobs-empty").classList.toggle("hidden", state.jobs.length > 0);
  const overview = $("#overview-jobs"); overview.replaceChildren();
  if (state.jobs.length) state.jobs.slice(0, 3).forEach((job) => overview.append(renderJob(job)));
  else overview.append(node("div", "empty", "Активных задач нет"));
  $("#jobs-badge").textContent = String(state.jobs.length);
  $("#stat-jobs").textContent = String(state.jobs.length);
}

async function loadDocuments() {
  const [documents, tags] = await Promise.all([request("/documents"), request("/tags")]);
  state.documents = documents.documents || []; state.tags = tags.tags || [];
  $("#stat-docs").textContent = String(documents.count || 0);
  $("#stat-nodes").textContent = String(state.documents.reduce((sum, doc) => sum + (doc.node_count || 0), 0));
  $("#stat-tags").textContent = String(tags.count || 0);
  const select = $("#tag-filter"); const current = select.value; select.replaceChildren(new Option("Все теги", "")); state.tags.forEach((tag) => select.add(new Option(tag, tag))); select.value = current;
  renderDocuments();
}

async function loadJobs() {
  try { const data = await request("/documents/jobs/active"); state.jobs = data.jobs || []; renderJobs(); }
  catch (error) { console.error(error); }
}

function openUpload(operation = "upload", name = "") {
  $("#upload-form").reset(); $("#upload-operation").value = operation; $("#upload-name").value = name;
  $("#upload-title").textContent = { upload: "Новый документ", update: "Новая версия", reload: "Полная замена" }[operation];
  $("#upload-dialog").showModal();
}

async function submitUpload(event) {
  event.preventDefault();
  const operation = $("#upload-operation").value; const name = $("#upload-name").value.trim(); const file = $("#upload-file").files[0];
  if (!file || (operation !== "upload" && !name)) { toast("Для обновления нужны файл и название документа", true); return; }
  const form = new FormData(); form.append("file", file);
  const fields = { name, version: $("#upload-version").value.trim(), title: $("#upload-doc-title").value.trim(), doc_type: $("#upload-doc-type").value.trim(), corpus: $("#upload-corpus").value.trim(), lang: $("#upload-lang").value.trim() };
  Object.entries(fields).forEach(([key, value]) => { if (value && !(key === "name" && operation !== "upload")) form.append(key, value); });
  const pathName = encodeURIComponent(name); const url = operation === "upload" ? "/documents" : `/documents/${pathName}`; const method = { upload: "POST", update: "PATCH", reload: "PUT" }[operation];
  try { await request(url, { method, body: form }); $("#upload-dialog").close(); toast("Документ поставлен в очередь"); await loadJobs(); showView("jobs"); }
  catch (error) { toast(error.message, true); }
}

function field(label, id, value = "", type = "text") {
  const wrap = node("label", "", label); const input = node(type === "textarea" ? "textarea" : "input"); input.id = id; input.value = value ?? ""; if (type !== "textarea") input.type = type; wrap.append(input); return wrap;
}

async function openDocument(doc) {
  try {
    const detail = await request(`/library/documents/${encodeURIComponent(doc.doc_id)}`); state.current = { row: doc, detail };
    $("#detail-name").textContent = detail.name; $("#detail-version").textContent = `Версия ${doc.version} · ${detail.fragments.length} фрагментов`;
    const form = $("#metadata-form"); form.replaceChildren(
      field("Заголовок", "meta-title", detail.title), field("Тип документа", "meta-doc-type", detail.doc_type), field("Корпус", "meta-corpus", detail.corpus), field("Язык", "meta-lang", detail.lang), field("Статус", "meta-status", detail.status), field("Дата вступления", "meta-effective-date", detail.effective_date), field("Теги, через запятую", "meta-tags", ((detail.tags || []).length ? detail.tags : (doc.tags || [])).join(", ")), field("External IDs, JSON", "meta-external", JSON.stringify(detail.external_ids || {}, null, 2), "textarea"), field("Метаданные, JSON", "meta-metadata", JSON.stringify(detail.metadata || {}, null, 2), "textarea")
    );
    renderFragments(detail.fragments); $("#document-dialog").showModal();
  } catch (error) { toast(error.message, true); }
}

function renderFragments(fragments) {
  const list = $("#fragments-list"); list.replaceChildren();
  fragments.forEach((fragment) => {
    const item = node("article", "fragment"); item.append(node("small", "", `#${fragment.order} ${fragment.numbering || ""}`));
    const body = node("div"); body.append(node("p", "", fragment.text), tagsCell(fragment.tags));
    const edit = node("button", "mini-button", "Изменить"); edit.addEventListener("click", () => openFragment(fragment)); item.append(body, edit); list.append(item);
  });
}

function parseJson(id) {
  const value = $(id).value.trim();
  try { return value ? JSON.parse(value) : {}; } catch (_) { throw new Error(`Некорректный JSON в поле ${id}`); }
}

async function saveMetadata(event) {
  event.preventDefault(); if (!state.current) return;
  try {
    const body = { title: $("#meta-title").value || null, doc_type: $("#meta-doc-type").value, corpus: $("#meta-corpus").value, lang: $("#meta-lang").value || null, status: $("#meta-status").value, effective_date: $("#meta-effective-date").value || null, tags: $("#meta-tags").value.split(",").map((v) => v.trim()).filter(Boolean), external_ids: parseJson("#meta-external"), metadata: parseJson("#meta-metadata") };
    await request(`/library/documents/${encodeURIComponent(state.current.detail.doc_id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); toast("Метаданные сохранены"); await loadDocuments();
  } catch (error) { toast(error.message, true); }
}

function openFragment(fragment) {
  state.fragment = fragment; $("#fragment-title").textContent = `${fragment.type || "Фрагмент"} ${fragment.numbering || ""}`; $("#fragment-text").value = fragment.text || ""; $("#fragment-tags").value = (fragment.tags || []).join(", "); $("#fragment-metadata").value = JSON.stringify(fragment.metadata || {}, null, 2); $("#fragment-dialog").showModal();
}

async function saveFragment(event) {
  event.preventDefault(); if (!state.current || !state.fragment) return;
  try {
    const body = { text: $("#fragment-text").value, tags: $("#fragment-tags").value.split(",").map((v) => v.trim()).filter(Boolean), metadata: parseJson("#fragment-metadata") };
    const updated = await request(`/library/documents/${encodeURIComponent(state.current.detail.doc_id)}/fragments/${encodeURIComponent(state.fragment.id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const index = state.current.detail.fragments.findIndex((item) => item.id === updated.id); state.current.detail.fragments[index] = updated; renderFragments(state.current.detail.fragments); $("#fragment-dialog").close(); toast("Фрагмент и embedding обновлены");
  } catch (error) { toast(error.message, true); }
}

async function deleteVersion(all = false) {
  if (!state.current) return; const { row } = state.current; const text = all ? `Удалить документ «${row.name}» со всеми версиями?` : `Удалить версию «${row.version}»?`;
  if (!confirm(text)) return;
  const suffix = all ? "" : `?version=${encodeURIComponent(row.version)}`;
  try { await request(`/documents/${encodeURIComponent(row.name)}${suffix}`, { method: "DELETE" }); $("#document-dialog").close(); toast("Удаление выполнено"); await loadDocuments(); }
  catch (error) { toast(error.message, true); }
}

const parserSettings = [
  ["partition_strategy", "Стратегия partition", "text"], ["languages", "Языки (через запятую)", "list"], ["window_chars", "Размер окна, символов", "number"], ["window_max_items", "Элементов в окне", "number"], ["overlap_blocks", "Перекрытие блоков", "number"], ["semantic_merge_max_passes", "Проходов объединения", "number"], ["split_sentences", "Разделять предложения", "boolean"], ["sent_min_len", "Минимальная длина предложения", "number"]
];

async function loadSettings() {
  try {
    const data = await request("/system/settings"); const values = Object.fromEntries(data.settings.map((item) => [item.field, item.value])); const form = $("#settings-form"); form.replaceChildren();
    parserSettings.forEach(([name, label, type]) => { const wrap = node("label", "", label); let input;
      if (type === "boolean") { input = node("input"); input.type = "checkbox"; input.checked = Boolean(values[name]); wrap.classList.add("checkbox-label"); }
      else { input = node("input"); input.type = type === "number" ? "number" : "text"; input.value = type === "list" ? (values[name] || []).join(", ") : values[name] ?? ""; }
      input.name = name; input.dataset.type = type; wrap.append(input); form.append(wrap); });
  } catch (error) { toast(error.message, true); }
}

async function saveSettings(event) {
  event.preventDefault(); const updates = {};
  $$("input", event.currentTarget).forEach((input) => { if (input.dataset.type === "boolean") updates[input.name] = input.checked; else if (input.dataset.type === "number") updates[input.name] = Number(input.value); else if (input.dataset.type === "list") updates[input.name] = input.value.split(",").map((v) => v.trim()).filter(Boolean); else updates[input.name] = input.value; });
  try { await request("/system/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ updates }) }); toast("Настройки парсинга сохранены"); }
  catch (error) { toast(error.message, true); }
}

function init() {
  const storedTheme = localStorage.getItem("dvd-admin-theme") || "dark"; document.documentElement.dataset.theme = storedTheme;
  $("#theme-toggle").addEventListener("click", () => { const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = theme; localStorage.setItem("dvd-admin-theme", theme); });
  $$(".nav-link").forEach((link) => link.addEventListener("click", () => showView(link.dataset.view))); $$(".goto").forEach((link) => link.addEventListener("click", () => showView(link.dataset.target)));
  $$(".close-dialog").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
  $$(".tab").forEach((tab) => tab.addEventListener("click", () => { $$(".tab").forEach((item) => item.classList.toggle("active", item === tab)); $$(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `tab-${tab.dataset.tab}`)); }));
  $("#open-upload").addEventListener("click", () => openUpload()); $("#upload-operation").addEventListener("change", (event) => { $("#upload-title").textContent = event.target.options[event.target.selectedIndex].text; }); $("#upload-form").addEventListener("submit", submitUpload);
  $("#doc-search").addEventListener("input", renderDocuments); $("#tag-filter").addEventListener("change", renderDocuments); $("#refresh-docs").addEventListener("click", loadDocuments);
  $("#metadata-form").addEventListener("submit", saveMetadata); $("#fragment-form").addEventListener("submit", saveFragment); $("#delete-document").addEventListener("click", () => deleteVersion(true)); $("#delete-version").addEventListener("click", () => deleteVersion(false)); $("#replace-document").addEventListener("click", () => { const name = state.current.row.name; $("#document-dialog").close(); openUpload("reload", name); }); $("#settings-form").addEventListener("submit", saveSettings);
  showView(location.hash.slice(1) || "overview"); Promise.all([loadDocuments(), loadJobs(), loadSettings()]).catch((error) => toast(error.message, true)); window.setInterval(loadJobs, 2500);
}

document.addEventListener("DOMContentLoaded", init);
