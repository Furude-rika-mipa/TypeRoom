"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const CATEGORY_NAMES = {serif: "衬线", sans: "无衬线", handwriting: "书写", mono: "等宽", other: "其他"};
const PRESETS = {
  chinese: "山河远阔，人间烟火。",
  japanese: "春の風に、桜が舞う。",
  english: "The art of being yourself.",
  numbers: "0123456789  !@#$%&*",
};
const DEFAULTS = {text: PRESETS.chinese, size: 36, spacing: 0, view: "grid", sort: "family"};
function readSaved(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* Private mode may disallow storage. */ }
}
const saved = readSaved("type-room-settings", {});
const savedFavorites = readSaved("type-room-favorites", []);
const state = {
  fonts: [], version: null, folder: "", exists: true, connected: false, initialized: false,
  text: typeof saved.text === "string" ? saved.text.slice(0, 300) : DEFAULTS.text,
  size: Number.isFinite(saved.size) ? Math.max(16, Math.min(80, saved.size)) : DEFAULTS.size,
  spacing: Number.isFinite(saved.spacing) ? Math.max(-1, Math.min(8, saved.spacing)) : DEFAULTS.spacing,
  view: saved.view === "list" ? "list" : "grid",
  sort: ["family", "name", "newest", "weight"].includes(saved.sort) ? saved.sort : "family",
  tab: "all", category: "all", search: "",
  favorites: new Set(Array.isArray(savedFavorites) ? savedFavorites.filter(id => typeof id === "string") : []),
  selected: new Set(), importing: false,
};
const records = new Map();
const loadQueue = [];
let activeLoads = 0;
let polling = false;
let toastTimer;
let detailFont = null;
let dragDepth = 0;
let tmpRefreshTimer;
let tmpActive = 0;
const tmpQueue = new Map();
const tmpJobs = new WeakMap();
const tmpWidths = new WeakMap();

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}
function element(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
}
function iconButton(name, label, action) {
  const button = element("button", "icon-button");
  button.type = "button";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.append(icon(name));
  button.addEventListener("click", action);
  return button;
}
function toast(message, duration = 3500) {
  clearTimeout(toastTimer);
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  toastTimer = setTimeout(() => { $("#toast").hidden = true; }, duration);
}
function persistSettings() {
  save("type-room-settings", {text: state.text, size: state.size, spacing: state.spacing, view: state.view, sort: state.sort});
}
function formatSize(bytes) {
  return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${(bytes / 1024).toFixed(1)} KB`;
}
function currentText() { return state.text.trim() ? state.text : PRESETS.chinese; }
function updateControls() {
  $("#preview-text").value = state.text;
  $("#font-size").value = state.size;
  $("#letter-spacing").value = state.spacing;
  $("#font-size-value").replaceChildren(document.createTextNode(String(state.size)), element("span", "", "px"));
  $("#letter-spacing-value").replaceChildren(document.createTextNode(String(state.spacing)), element("span", "", "px"));
  document.documentElement.style.setProperty("--sample-size", `${state.size}px`);
  document.documentElement.style.setProperty("--sample-spacing", `${state.spacing}px`);
  for (const input of [$("#font-size"), $("#letter-spacing")]) {
    const percentage = (Number(input.value) - Number(input.min)) / (Number(input.max) - Number(input.min)) * 100;
    input.style.setProperty("--range-progress", `${percentage}%`);
  }
  $$("[data-preset]").forEach(button => {
    const active = PRESETS[button.dataset.preset] === state.text;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  $$(".card-preview:not(.tmp-preview):not(.failed)").forEach(preview => { preview.textContent = currentText(); });
  if (detailFont && detailFont.kind !== "tmp") $("#detail-preview").textContent = currentText();
  scheduleTmpPreviews();
}

function prepareTmpPreview(preview) {
  preview.classList.add("tmp-preview");
  const canvas = element("canvas", "tmp-canvas");
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", "TMP 字形预览");
  const note = element("div", "tmp-missing");
  note.hidden = true;
  preview.replaceChildren(canvas, note);
  tmpResizeObserver.observe(preview);
}
function scheduleTmpPreviews() {
  clearTimeout(tmpRefreshTimer);
  tmpRefreshTimer = setTimeout(() => {
    $$(".tmp-preview[data-active='true']").forEach(preview => {
      const font = preview.id === "detail-preview" ? detailFont : state.fonts.find(item => item.id === preview.dataset.fontId);
      if (font && preview.isConnected) renderTmpPreview(preview, font);
    });
  }, 180);
}
function renderTmpPreview(preview, font) {
  preview.dataset.active = "true";
  const status = preview.closest(".font-card")?.querySelector(".card-status");
  if (font.tmp_status !== "ready") {
    preview.classList.add("ready");
    preview.textContent = font.error || "正在解析 TMP 资源包…";
    preview.classList.toggle("failed", Boolean(font.error));
    if (status) { status.textContent = font.error ? "解析失败" : "解析中"; status.classList.toggle("error", Boolean(font.error)); }
    return;
  }
  if (!$("canvas", preview)) prepareTmpPreview(preview);
  const previous = tmpJobs.get(preview);
  previous?.controller?.abort();
  const size = preview.id === "detail-preview" ? 45 : state.tab === "compare" ? state.size * 1.2 : state.view === "list" ? Math.min(state.size, 60) : state.size;
  const width = Math.max(80, Math.min(1600, Math.floor(preview.clientWidth)));
  const job = {preview, font, size, width, text: currentText(), spacing: state.spacing, dpr: Math.min(2, Math.max(1, window.devicePixelRatio || 1))};
  tmpWidths.set(preview, width);
  tmpJobs.set(preview, job);
  tmpQueue.set(preview, job);
  if (status) { status.textContent = "绘制中"; status.classList.remove("error", "loaded"); }
  pumpTmpPreviews();
}
function pumpTmpPreviews() {
  while (tmpActive < 2 && tmpQueue.size) {
    const [preview, job] = tmpQueue.entries().next().value;
    tmpQueue.delete(preview);
    if (!preview.isConnected || tmpJobs.get(preview) !== job) continue;
    tmpActive++;
    drawTmpPreview(job).finally(() => { tmpActive--; pumpTmpPreviews(); });
  }
}
async function drawTmpPreview(job) {
  const {preview, font} = job;
  const status = preview.closest(".font-card")?.querySelector(".card-status");
  job.controller = new AbortController();
  try {
    const query = new URLSearchParams({font: font.id, text: job.text, size: job.size, spacing: job.spacing, width: job.width, dpr: job.dpr, v: font.revision});
    const response = await fetch(`/api/tmp/render?${query}`, {signal: job.controller.signal});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "TMP 字形预览失败");
    const image = new Image();
    image.src = result.image;
    await image.decode();
    if (!preview.isConnected || tmpJobs.get(preview) !== job) return;
    const canvas = $("canvas", preview);
    canvas.width = result.pixel_width;
    canvas.height = result.pixel_height;
    canvas.style.height = `${result.height}px`;
    canvas.getContext("2d").drawImage(image, 0, 0);
    const messages = [];
    if (result.missing.length) messages.push(`缺少 ${result.missing.length} 个字符：${result.missing.join(" ")}`);
    if (result.truncated) messages.push("预览内容较长，已显示可绘制的部分");
    const note = $(".tmp-missing", preview);
    note.textContent = messages.join("；");
    note.hidden = !messages.length;
    note.title = result.missing.map(character => `${character} (U+${character.codePointAt(0).toString(16).toUpperCase()})`).join("、");
    canvas.setAttribute("aria-label", `${font.family}，${job.size}px：${job.text}${messages.length ? `；${messages.join("；")}` : ""}`);
    preview.classList.remove("failed");
    preview.classList.add("ready");
    preview.style.opacity = "1";
    if (status) {
      status.textContent = result.missing.length ? `缺字 ${result.missing.length}` : "已就绪";
      status.classList.toggle("error", Boolean(result.missing.length));
      status.classList.toggle("loaded", !result.missing.length);
    }
  } catch (error) {
    if (error.name === "AbortError" || !preview.isConnected || tmpJobs.get(preview) !== job) return;
    preview.classList.add("ready", "failed");
    const note = $(".tmp-missing", preview);
    if (note) { note.hidden = false; note.textContent = error.message; }
    if (status) { status.textContent = "预览失败"; status.classList.add("error"); }
  }
}
const tmpResizeObserver = new ResizeObserver(entries => {
  for (const entry of entries) {
    const preview = entry.target;
    const width = Math.floor(entry.contentRect.width);
    if (!preview.isConnected || preview.dataset.active !== "true" || width < 80 || tmpWidths.get(preview) === width) continue;
    const font = preview.id === "detail-preview" ? detailFont : state.fonts.find(item => item.id === preview.dataset.fontId);
    if (font?.tmp_status === "ready") renderTmpPreview(preview, font);
  }
});

function ensureFont(font) {
  const previous = records.get(font.id);
  if (previous && previous.revision === font.revision) return previous.promise;
  if (font.error) return Promise.reject(new Error(font.error));
  const record = {revision: font.revision, state: "queued", family: `tr_${font.id}_${font.revision.replace(/[^0-9a-z]/gi, "_")}`, face: null};
  record.promise = new Promise((resolve, reject) => { record.resolve = resolve; record.reject = reject; });
  records.set(font.id, record);
  loadQueue.push({font, record});
  pumpLoads();
  return record.promise;
}
function pumpLoads() {
  while (activeLoads < 3 && loadQueue.length) {
    const {font, record} = loadQueue.shift();
    if (records.get(font.id) !== record) { record.reject(new Error("字体已更新")); continue; }
    activeLoads++;
    record.state = "loading";
    const face = new FontFace(record.family, `url("${font.url}")`, {weight: String(font.weight), style: "normal"});
    record.face = face;
    face.load().then(loaded => {
      if (records.get(font.id) === record) document.fonts.add(loaded);
      record.state = "loaded";
      record.resolve(record);
    }).catch(error => {
      record.state = "error";
      record.reject(error);
    }).finally(() => { activeLoads--; pumpLoads(); });
  }
}
async function loadPreview(preview, font) {
  if (font.kind === "tmp") return renderTmpPreview(preview, font);
  if (preview.dataset.loading) return;
  preview.dataset.loading = "true";
  const status = $(".card-status", preview.closest(".font-card"));
  status.textContent = "载入中";
  try {
    const record = await ensureFont(font);
    if (!preview.isConnected) return;
    preview.style.fontFamily = `"${record.family}"`;
    preview.style.fontWeight = String(font.weight);
    preview.classList.add("ready");
    status.textContent = "已就绪";
    status.classList.add("loaded");
  } catch {
    if (!preview.isConnected) return;
    preview.classList.add("failed");
    preview.textContent = font.error || "字体载入失败，请检查文件是否完整。";
    status.textContent = "载入失败";
    status.classList.add("error");
  }
}
const previewObserver = new IntersectionObserver(entries => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    previewObserver.unobserve(entry.target);
    const font = state.fonts.find(item => item.id === entry.target.dataset.fontId);
    if (font) loadPreview(entry.target, font);
  }
}, {rootMargin: "120px 0px"});

function toggleFavorite(font) {
  if (state.favorites.has(font.id)) state.favorites.delete(font.id);
  else state.favorites.add(font.id);
  save("type-room-favorites", [...state.favorites]);
  if (state.tab === "favorites") renderLibrary();
  else updateCardActions();
}
function toggleCompare(font) {
  if (state.selected.has(font.id)) state.selected.delete(font.id);
  else {
    if (state.selected.size >= 4) return toast("最多同时对比 4 款字体，先移除一款再添加吧。");
    state.selected.add(font.id);
  }
  if (state.tab === "compare") renderLibrary();
  else updateCardActions();
  updateDock();
}
function updateDock() {
  const count = state.selected.size;
  $("#compare-count").textContent = count;
  $("#compare-count").hidden = !count;
  $("#dock-count").textContent = count;
  $("#compare-dock").hidden = !count || state.tab === "compare";
}
function updateCardActions() {
  $$(".font-card").forEach(card => {
    const favorite = state.favorites.has(card.dataset.fontId);
    const selected = state.selected.has(card.dataset.fontId);
    const favoriteButton = $(".favorite-button", card);
    const compareButton = $(".compare-button", card);
    favoriteButton.classList.toggle("favorited", favorite);
    favoriteButton.setAttribute("aria-pressed", String(favorite));
    favoriteButton.setAttribute("aria-label", favorite ? "取消收藏" : "收藏字体");
    favoriteButton.title = favorite ? "取消收藏" : "收藏字体";
    compareButton.classList.toggle("active", selected);
    compareButton.setAttribute("aria-pressed", String(selected));
    compareButton.setAttribute("aria-label", selected ? "移出对比" : "加入对比");
    compareButton.title = selected ? "移出对比" : "加入对比";
    compareButton.replaceChildren(icon(selected ? "check" : "compare"));
    card.classList.toggle("is-selected", selected);
  });
}
function createCard(font) {
  const card = element("article", "font-card");
  card.dataset.fontId = font.id;
  const top = element("div", "card-top");
  const names = element("div", "card-name-group");
  const title = element("h3", "card-title", font.family);
  title.title = font.name;
  const subtitle = font.kind === "tmp" ? `${font.filename}${font.engine_version ? ` · Unity ${font.engine_version}` : ""}` : `${font.style} · ${CATEGORY_NAMES[font.category] || "其他"}${font.variable ? " · 可变字体" : ""}`;
  names.append(title, element("div", "card-subtitle", subtitle));
  const badge = element("button", "font-badge", font.format);
  if (font.kind === "tmp") badge.classList.add("tmp");
  badge.title = "查看字体详情";
  badge.setAttribute("aria-label", `查看 ${font.name} 的详情`);
  badge.addEventListener("click", () => showDetails(font));
  top.append(names, badge);
  const preview = element("div", "card-preview", currentText());
  preview.dataset.fontId = font.id;
  if (font.kind === "tmp") prepareTmpPreview(preview);
  preview.setAttribute("aria-label", `${font.name} 文字预览`);
  const footer = element("div", "card-footer");
  const meta = element("div", "card-meta");
  const fileInfo = font.kind === "tmp" ? `${(font.character_count || 0).toLocaleString()} 字符 · ${formatSize(font.size)}` : `${font.weight} · ${formatSize(font.size)}`;
  meta.append(element("span", "card-status", "待预览"), element("span", "card-weight", fileInfo));
  const actions = element("div", "card-actions");
  const info = iconButton("info", "字体详情", () => showDetails(font));
  const compare = iconButton("compare", "加入对比", () => toggleCompare(font));
  compare.classList.add("compare-button");
  const favorite = iconButton("star", "收藏字体", () => toggleFavorite(font));
  favorite.classList.add("favorite-button");
  actions.append(info, compare, favorite);
  footer.append(meta, actions);
  card.append(top, preview, footer);
  previewObserver.observe(preview);
  return card;
}

function filteredFonts() {
  let fonts = state.fonts.filter(font => {
    if (state.tab === "compare") return state.selected.has(font.id);
    if (state.tab === "favorites" && !state.favorites.has(font.id)) return false;
    if (state.category === "tmp" && font.kind !== "tmp") return false;
    if (state.category !== "all" && state.category !== "tmp" && font.category !== state.category) return false;
    const query = state.search.trim().toLocaleLowerCase();
    return !query || `${font.name} ${font.family} ${font.filename} ${font.path} ${font.style} ${font.kind === "tmp" ? `TMP Unity ${font.engine_version || ""}` : ""}`.toLocaleLowerCase().includes(query);
  });
  if (state.tab === "compare") return fonts.sort((a, b) => [...state.selected].indexOf(a.id) - [...state.selected].indexOf(b.id));
  const compareNames = (a, b) => a.name.localeCompare(b.name, "zh-CN", {numeric: true}) || a.weight - b.weight;
  if (state.sort === "newest") return fonts.sort((a, b) => b.modified - a.modified || compareNames(a, b));
  if (state.sort === "weight") return fonts.sort((a, b) => a.weight - b.weight || compareNames(a, b));
  if (state.sort === "name") return fonts.sort(compareNames);
  // Rotate through families, so the first screen shows a variety of typefaces.
  const families = new Map();
  fonts.sort((a, b) => a.family.localeCompare(b.family, "zh-CN") || Math.abs(a.weight - 400) - Math.abs(b.weight - 400));
  for (const font of fonts) {
    if (!families.has(font.family)) families.set(font.family, []);
    families.get(font.family).push(font);
  }
  const result = [];
  let remaining = true;
  for (let index = 0; remaining; index++) {
    remaining = false;
    for (const family of families.values()) {
      if (family[index]) { result.push(family[index]); remaining = true; }
    }
  }
  return result;
}
function emptyMessage(title, description, action) {
  $("#empty-state").hidden = false;
  $("#empty-title").textContent = title;
  $("#empty-description").textContent = description;
  $("#empty-action").hidden = !action;
  if (action) {
    $("#empty-action").textContent = action.label;
    $("#empty-action").onclick = action.callback;
  }
}
function renderLibrary() {
  previewObserver.disconnect();
  tmpResizeObserver.disconnect();
  for (const preview of tmpQueue.keys()) if (preview.id !== "detail-preview") tmpQueue.delete(preview);
  const compare = state.tab === "compare";
  const fonts = filteredFonts();
  $("#library-title").firstChild.textContent = compare ? "放在一起，更有感觉" : state.tab === "favorites" ? "我的心选" : "发现好字";
  $("#result-count").textContent = fonts.length;
  const families = new Set(state.fonts.map(font => font.family)).size;
  $("#library-subtitle").textContent = compare ? "同样的文字，不同的表达。最多同时对比 4 款。" : state.tab === "favorites" ? "把喜欢的字体，留给下一次灵感。" : state.initialized ? `${state.fonts.length} 款字体，${families} 个字体家族。你的好字，都在这里。` : "你的字体，都在这里。";
  $("#font-grid").className = `font-grid${compare ? " compare" : state.view === "list" ? " list" : ""}`;
  $("#font-grid").setAttribute("aria-busy", String(!state.initialized));
  $("#font-grid").replaceChildren(...fonts.map(createCard));
  $("#empty-state").hidden = fonts.length > 0;
  $(".search-box").hidden = compare;
  $(".filter-toolbar").hidden = compare;
  if (!fonts.length) {
    if (!state.connected) emptyMessage("字体展厅暂时连不上了", "请保持启动窗口运行。\n如果尚未启动，请双击项目里的 start.cmd。", {label: "重新连接", callback: () => pollFonts(true)});
    else if (!state.exists) emptyMessage("还没有找到这个字体文件夹", "检查 settings.json 中的目录，或连接字体所在的磁盘。\n目录恢复后，这里会自动更新。");
    else if (compare) emptyMessage("好字，一起看才更清楚", "点击字体卡片上的对比图标，挑选你想一起看的字体。", {label: "去挑选字体", callback: () => changeTab("all")});
    else if (state.search || state.category !== "all") emptyMessage("还没有找到这款字", "试试其他关键词，或切换到全部字体。", {label: "清除筛选", callback: clearFilters});
    else if (state.tab === "favorites") emptyMessage("给喜欢的字体，留一个位置", "点击字体卡片右下角的星标，就能收藏到这里。", {label: "去发现好字", callback: () => changeTab("all")});
    else emptyMessage("你的第一款字体，即将登场", "把字体放进指定文件夹，或直接拖进这个页面。\n新加入的字体，会自动出现在这里。", {label: "添加字体", callback: () => $("#file-input").click()});
  }
  updateCardActions();
  updateDock();
  if (detailFont?.kind === "tmp" && $("#detail-dialog").open) tmpResizeObserver.observe($("#detail-preview"));
}
function clearFilters() {
  state.category = "all";
  state.search = "";
  $("#search").value = "";
  updateCategoryButtons();
  renderLibrary();
}
function updateCategoryButtons() {
  $$("[data-category]").forEach(button => {
    const active = button.dataset.category === state.category;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}
function changeTab(tab) {
  state.tab = tab;
  $$("[data-tab]").forEach(button => {
    const active = button.dataset.tab === tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  renderLibrary();
}

async function showDetails(font) {
  detailFont = font;
  $("#detail-title").textContent = font.name;
  $("#detail-family").textContent = font.family;
  const preview = $("#detail-preview");
  tmpJobs.get(preview)?.controller?.abort();
  tmpResizeObserver.unobserve(preview);
  preview.classList.remove("tmp-preview", "ready", "failed");
  delete preview.dataset.active;
  preview.style.fontFamily = '"SimSun", serif';
  preview.style.fontWeight = String(font.weight);
  preview.style.opacity = ".35";
  preview.textContent = currentText();
  const properties = [
    ["文件名", font.filename], ["相对路径", font.path], ["文件格式", `${font.format}${font.variable ? " · 可变字体" : ""}`],
    ["字重 / 风格", `${font.weight} / ${font.style}`], ["文件大小", formatSize(font.size)],
    ["修改时间", new Date(font.modified * 1000).toLocaleString("zh-CN")],
  ];
  if (font.kind === "tmp") properties.push(
    ["Unity 版本", font.engine_version || "正在解析"],
    ["收录字符", (font.character_count || 0).toLocaleString()],
    ["字体图集", `${font.atlas_count || 0} 张 · ${font.atlas_width || 0} × ${font.atlas_height || 0}`],
    ["预览方式", "资源包原始字形 · 缺字显示方框"],
  );
  $("#detail-properties").replaceChildren(...properties.flatMap(([key, value]) => [element("dt", "", key), element("dd", "", value)]));
  if (!$("#detail-dialog").open) $("#detail-dialog").showModal();
  if (font.kind === "tmp") { prepareTmpPreview(preview); renderTmpPreview(preview, font); return; }
  try {
    const record = await ensureFont(font);
    if (detailFont !== font) return;
    preview.style.fontFamily = `"${record.family}"`;
    preview.style.opacity = "1";
  } catch {
    if (detailFont === font) { preview.textContent = "字体载入失败"; preview.style.opacity = "1"; }
  }
}
function updateConnection(connected) {
  const wasConnected = state.connected;
  state.connected = connected;
  $("#sync-status").classList.toggle("offline", !connected);
  $("#sync-label").textContent = connected ? "自动同步已开启" : "连接已中断";
  $("#sync-status").title = connected ? "每 3 秒扫描一次字体文件夹，新增和移除均自动同步" : "请保持本地启动窗口运行";
  if (!connected && wasConnected) toast("与本地服务的连接中断，正在自动重试。", 4500);
}
async function pollFonts(force = false) {
  if (polling) return;
  polling = true;
  try {
    const response = await fetch("/api/fonts", {cache: "no-store", signal: AbortSignal.timeout(8000)});
    if (!response.ok) throw new Error("连接失败");
    const data = await response.json();
    if (!Array.isArray(data.fonts)) throw new Error("接口格式错误");
    const wasInitialized = state.initialized;
    const oldIds = new Set(state.fonts.map(font => font.id));
    updateConnection(true);
    state.initialized = true;
    if (data.version !== state.version || force) {
      for (const [id, record] of records) {
        const latest = data.fonts.find(font => font.id === id);
        if (!latest || latest.revision !== record.revision) {
          if (record.face) document.fonts.delete(record.face);
          records.delete(id);
        }
      }
      state.fonts = data.fonts;
      state.version = data.version;
      state.folder = data.folder;
      state.exists = data.exists;
      state.selected = new Set([...state.selected].filter(id => data.fonts.some(font => font.id === id)));
      $("#nav-count").textContent = data.count;
      $("#folder-label").textContent = data.folder;
      $("#folder-label").title = data.folder;
      const formats = "TTF / OTF / WOFF / WOFF2 / TMP";
      $("#directory-info").textContent = data.parsing ? `正在解析 ${data.parsing} 个 TMP 资源包…` : data.unsupported ? `${data.unsupported} 个字体集合暂不支持 · ${formats}` : `自动扫描子文件夹 · ${formats}`;
      $("#directory-info").title = data.ignored ? `已忽略 ${data.ignored} 个隐藏或附带文件` : "新增字体将在 3 秒左右自动出现";
      renderLibrary();
      if (detailFont && $("#detail-dialog").open) {
        const latest = data.fonts.find(font => font.id === detailFont.id);
        if (latest && latest.revision !== detailFont.revision) showDetails(latest);
      }
      if (wasInitialized && !state.importing) {
        const added = data.fonts.filter(font => !oldIds.has(font.id)).length;
        const removed = [...oldIds].filter(id => !data.fonts.some(font => font.id === id)).length;
        if (added) toast(`发现 ${added} 款新字体，已自动加入字体库。`);
        else if (removed) toast(`字体库已同步，移除了 ${removed} 款已不在目录中的字体。`);
      }
      if (detailFont && !data.fonts.some(font => font.id === detailFont.id)) $("#detail-dialog").close();
    }
  } catch {
    updateConnection(false);
    if (!state.initialized) {
      state.initialized = true;
      $("#folder-label").textContent = "本地服务未连接";
      renderLibrary();
    }
  } finally { polling = false; }
}
async function postAction(url, body) {
  const response = await fetch(url, {method: "POST", headers: {"X-Type-Room": "1", "Content-Type": "application/octet-stream"}, body});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "操作失败");
  return result;
}
async function importFiles(fileList) {
  if (state.importing) return toast("字体正在导入，稍等片刻。");
  const files = [...fileList].filter(file => !file.name.startsWith(".") && (/\.(ttf|otf|woff2?|bundle|unity3d|assetbundle|tmp)$/i.test(file.name) || !file.name.includes(".")));
  if (!files.length) return toast("请选择字体文件或 TMP 资源包（支持无后缀文件）。");
  state.importing = true;
  $("#import-button").disabled = true;
  let imported = 0;
  const errors = [];
  for (const [index, file] of files.entries()) {
    toast(`正在导入 ${index + 1} / ${files.length}：${file.name}`, 60000);
    try {
      if (file.size > 100 * 1024 * 1024) throw new Error("单个文件不能超过 100 MB");
      await postAction(`/api/fonts?name=${encodeURIComponent(file.name)}`, file);
      imported++;
    } catch (error) { errors.push(`${file.name}：${error.message}`); }
  }
  await pollFonts(true);
  state.importing = false;
  $("#import-button").disabled = false;
  $("#file-input").value = "";
  toast(errors.length ? `已导入 ${imported} 款字体。\n${errors.slice(0, 2).join("\n")}` : `已将 ${imported} 款字体添加到字体文件夹。`, errors.length ? 7000 : 3500);
}

$("#preview-text").addEventListener("input", event => { state.text = event.target.value; updateControls(); persistSettings(); });
$("#font-size").addEventListener("input", event => { state.size = Number(event.target.value); updateControls(); persistSettings(); });
$("#letter-spacing").addEventListener("input", event => { state.spacing = Number(event.target.value); updateControls(); persistSettings(); });
$$("[data-preset]").forEach(button => button.addEventListener("click", () => { state.text = PRESETS[button.dataset.preset]; updateControls(); persistSettings(); }));
$("#reset-button").addEventListener("click", () => { Object.assign(state, {text: DEFAULTS.text, size: DEFAULTS.size, spacing: DEFAULTS.spacing}); updateControls(); persistSettings(); toast("预览设置已重置。"); });
$("#search").addEventListener("input", event => { state.search = event.target.value; renderLibrary(); });
$$("[data-category]").forEach(button => button.addEventListener("click", () => { state.category = button.dataset.category; updateCategoryButtons(); renderLibrary(); }));
$$("[data-tab]").forEach(button => button.addEventListener("click", () => changeTab(button.dataset.tab)));
$$("[data-view]").forEach(button => button.addEventListener("click", () => {
  state.view = button.dataset.view;
  $$("[data-view]").forEach(item => { item.classList.toggle("active", item.dataset.view === state.view); item.setAttribute("aria-pressed", String(item.dataset.view === state.view)); });
  renderLibrary(); persistSettings();
}));
$("#sort").addEventListener("change", event => { state.sort = event.target.value; renderLibrary(); persistSettings(); });
$("#import-button").addEventListener("click", () => $("#file-input").click());
$("#file-input").addEventListener("change", event => importFiles(event.target.files));
$("#folder-button").addEventListener("click", async () => {
  try { await postAction("/api/open-folder"); } catch (error) { toast(error.message, 5000); }
});
$("#clear-compare").addEventListener("click", () => { state.selected.clear(); updateCardActions(); updateDock(); });
$("#show-compare").addEventListener("click", () => { changeTab("compare"); $("#font-library").scrollIntoView({behavior: "smooth", block: "start"}); });
$("#close-details").addEventListener("click", () => $("#detail-dialog").close());
$("#detail-dialog").addEventListener("close", () => {
  const preview = $("#detail-preview");
  tmpJobs.get(preview)?.controller?.abort();
  tmpResizeObserver.unobserve(preview);
  delete preview.dataset.active;
  detailFont = null;
});
$("#detail-dialog").addEventListener("click", event => {
  const bounds = event.currentTarget.getBoundingClientRect();
  if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) event.currentTarget.close();
});
$("#copy-filename").addEventListener("click", async () => {
  if (!detailFont) return;
  try { await navigator.clipboard.writeText(detailFont.filename); toast("文件名已复制。"); }
  catch { toast("复制失败，请在详情里选中文件名后手动复制。"); }
});
document.addEventListener("keydown", event => {
  if (event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey && !$("#detail-dialog").open && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
    event.preventDefault();
    if (state.tab === "compare") changeTab("all");
    $("#search").focus();
  }
});
document.addEventListener("dragenter", event => {
  if (![...event.dataTransfer.types].includes("Files")) return;
  event.preventDefault(); dragDepth++; $("#drop-overlay").hidden = false;
});
document.addEventListener("dragover", event => { if ([...event.dataTransfer.types].includes("Files")) { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; } });
document.addEventListener("dragleave", event => { if (!dragDepth) return; event.preventDefault(); dragDepth--; if (!dragDepth) $("#drop-overlay").hidden = true; });
document.addEventListener("drop", event => {
  event.preventDefault(); dragDepth = 0; $("#drop-overlay").hidden = true;
  if (event.dataTransfer.files.length) importFiles(event.dataTransfer.files);
});
window.addEventListener("blur", () => { dragDepth = 0; $("#drop-overlay").hidden = true; });
document.addEventListener("visibilitychange", () => { if (!document.hidden) pollFonts(); });
$("#sort").prepend(new Option("按家族浏览", "family"));
$("#sort").value = state.sort;
$("#sort option[value='newest']").textContent = "最近修改优先";
$$("[data-view]").forEach(button => { button.classList.toggle("active", button.dataset.view === state.view); button.setAttribute("aria-pressed", String(button.dataset.view === state.view)); });
updateControls();
pollFonts();
setInterval(() => { if (!document.hidden) pollFonts(); }, 3000);
