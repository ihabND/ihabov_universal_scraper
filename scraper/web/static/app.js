/* Front end for every page: copy buttons on the docs page, the Sites page, and the workbench on
   the home page, which calls GET /scrape directly and renders the result. */
(function () {
  "use strict";

  // ---- Helpers ---------------------------------------------------------------------------------
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const esc = (value) =>
    String(value).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch (e) { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* storage blocked */ } },
    remove(key) { try { localStorage.removeItem(key); } catch (e) { /* storage blocked */ } },
  };

  function copyText(text, button) {
    const done = () => {
      const label = button.textContent;
      button.textContent = "Copied";
      setTimeout(() => (button.textContent = label), 1200);
    };
    const fallback = () => {
      const area = document.createElement("textarea");
      area.value = text;
      area.style.cssText = "position:fixed;opacity:0";
      document.body.appendChild(area);
      area.select();
      try { document.execCommand("copy"); done(); } catch (e) { /* nothing more to try */ }
      area.remove();
    };
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, fallback);
    else fallback();
  }

  function download(text, filename, type) {
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([text], { type }));
    link.download = filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  }

  // ---- Copy buttons on the docs page -------------------------------------------------------------
  $$("[data-copy-target]").forEach((button) =>
    button.addEventListener("click", () => {
      const target = document.getElementById(button.dataset.copyTarget);
      if (target) copyText(target.textContent, button);
    })
  );

  // ---- Sites page: every site's strategy, read with the API key and edited with the admin key ----
  if ($("#strategies")) return sitesPage();

  function sitesPage() {
    const keyInput = $("#key");
    const adminInput = $("#admin-key");
    const list = $("#sites");
    const status = $("#sites-status");
    const filter = $("#filter");
    let groups = [];
    keyInput.value = store.get("scraper-key") || "";
    adminInput.value = store.get("scraper-admin-key") || "";

    const say = (text, bad = false) => {
      status.textContent = text;
      status.classList.toggle("bad", bad);
    };
    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    const show = (value) => {
      if (Array.isArray(value)) return value.length ? value.join(", ") : "none";
      if (value && typeof value === "object") {
        const pairs = Object.entries(value);
        return pairs.length ? pairs.map(([k, v]) => `${k}: ${v}`).join(", ") : "none";
      }
      return String(value);
    };
    const source = (row) =>
      ({ pinned: `pinned ${row.at || ""}`, learned: `learned ${row.at}`, parser: "parser default" })[row.source] ||
      "default";

    async function call(method, path, body, admin = false) {
      const headers = admin ? { "X-Admin-Key": adminInput.value.trim() } : { "X-API-Key": keyInput.value.trim() };
      if (body !== undefined) headers["Content-Type"] = "application/json";
      const response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Error ${response.status}`);
      return data;
    }

    function settingRow(row) {
      const learned = row.learned;
      let why = "";
      if (learned && row.source === "learned") why = learned.evidence;
      else if (learned && row.source === "pinned" && !same(learned.value, row.value))
        why = `the scraper found ${show(learned.value)} (${learned.at}): ${learned.evidence}`;
      const action =
        row.source === "pinned" ? `<button class="linkbtn" type="button" data-unpin="${esc(row.name)}">unpin</button>`
        : row.source === "learned" ? `<button class="linkbtn" type="button" data-pin="${esc(row.name)}">pin</button>`
        : "";
      return `<tr class="${same(row.value, row.default) ? "" : "changed"}">
        <td><code>${esc(row.name)}</code></td>
        <td>${esc(show(row.value))}</td>
        <td><span class="src ${esc(row.source)}">${esc(source(row))}</span>${why ? `<span class="why">${esc(why)}</span>` : ""}</td>
        <td>${action}</td></tr>`;
    }

    function siteCard(site) {
      const start = site.settings.find((row) => row.name === "start");
      const changed = site.settings.filter((row) => row.name !== "start" && !same(row.value, row.default)).length;
      const flags = Object.entries(site.flags);
      const last = site.last.at
        ? `${site.last.status} ${site.last.route === "-" ? "failed" : "via " + site.last.route}, ${site.last.seconds} s, ${site.last.at}`
        : "not scraped yet";
      return `<details class="strategy" data-site="${esc(site.site)}">
        <summary>
          <b>${esc(site.site)}</b>
          <span class="tag">start: ${esc(start.value)} · ${esc(source(start))}</span>
          ${changed ? `<span class="tag changed">${changed} changed</span>` : ""}
          ${flags.length ? `<span class="tag flag">⚑ ${flags.length}</span>` : ""}
          <span class="meta">last: ${esc(last)} · ${site.requests} requests, ${site.ok} ok</span>
        </summary>
        <div class="strategy-body">
          ${flags.length ? `<ul class="flags">${flags
            .map(([name, flag]) => `<li>⚑ ${esc(flag.at)}: <b>${esc(name)}</b>: ${esc(flag.seen)}. You pinned ${esc(show(site.pins[name]))}.</li>`)
            .join("")}</ul>` : ""}
          <div class="tablewrap"><table>
            <tr><th>Setting</th><th>Value</th><th>Source</th><th></th></tr>
            ${site.settings.map(settingRow).join("")}
          </table></div>
          <label class="field"><span>Pins as JSON: add a setting to pin it, remove it to unpin</span>
            <textarea class="input pins" rows="4" spellcheck="false">${esc(JSON.stringify(site.pins, null, 2))}</textarea>
          </label>
          <div class="strategy-actions">
            <button class="tool" type="button" data-save>Save pins</button>
            ${flags.length ? `<button class="tool" type="button" data-clear>Clear flags</button>` : ""}
          </div>
        </div>
      </details>`;
    }

    function render() {
      const needle = filter.value.trim().toLowerCase();
      const open = new Set($$("details.strategy[open]", list).map((card) => card.dataset.site));
      const shown = groups
        .map((group) => ({
          ...group,
          sites: group.sites.filter((site) => !needle || site.site.includes(needle) || group.domain.includes(needle)),
        }))
        .filter((group) => group.sites.length);
      list.innerHTML = shown.length
        ? shown.map((group) => `<section class="domain"><h2>${esc(group.domain)} <span class="count">${group.sites.length}</span></h2>${group.sites.map(siteCard).join("")}</section>`).join("")
        : groups.length ? `<p class="hint">No site matches the filter.</p>` : "";
      $$("details.strategy", list).forEach((card) => (card.open = open.has(card.dataset.site)));
      const total = groups.reduce((n, group) => n + group.sites.length, 0);
      $("#site-count").textContent = total ? `· ${total}` : "";
    }

    async function load() {
      for (const [input, name] of [[keyInput, "scraper-key"], [adminInput, "scraper-admin-key"]]) {
        const value = input.value.trim();
        value ? store.set(name, value) : store.remove(name);
      }
      say("Loading…");
      try {
        groups = await call("GET", "/api/sites");
        render();
        say(groups.length ? "Click a site for every setting. Pinned values win over learned ones, which win over the defaults." : "No sites yet: scrape something on the workbench first.");
      } catch (e) {
        say(e.message, true);
      }
    }

    async function change(method, path, body, done) {
      try {
        await call(method, path, body, true);
        await load();
        say(done);
      } catch (e) {
        say(e.message, true);
      }
    }

    list.addEventListener("click", (event) => {
      const button = event.target.closest("button");
      const card = button && button.closest("details.strategy");
      if (!card) return;
      const name = card.dataset.site;
      const site = groups.flatMap((group) => group.sites).find((s) => s.site === name);
      const path = `/api/sites/${encodeURIComponent(name)}`;
      if (button.dataset.clear !== undefined) return change("DELETE", `${path}/flags`, undefined, `Cleared the flags of ${name}.`);
      let pins = { ...site.pins };
      if (button.dataset.unpin) delete pins[button.dataset.unpin];
      else if (button.dataset.pin) pins[button.dataset.pin] = site.settings.find((row) => row.name === button.dataset.pin).value;
      else if (button.dataset.save !== undefined) {
        try {
          pins = JSON.parse($("textarea", card).value.trim() || "{}");
        } catch (e) {
          return say("The pins are not valid JSON.", true);
        }
      } else return;
      change("PUT", `${path}/pins`, { pins }, `Saved the pins of ${name}.`);
    });

    $("#keys").addEventListener("submit", (event) => {
      event.preventDefault();
      load();
    });
    filter.addEventListener("input", render);
    $("#export").addEventListener("click", async () => {
      try {
        const pins = await call("GET", "/api/sites/export", undefined, true);
        download(JSON.stringify(pins, null, 2), "site-pins.json", "application/json");
        say(`Exported the pins of ${Object.keys(pins).length} sites.`);
      } catch (e) {
        say(e.message, true);
      }
    });
    $("#import").addEventListener("change", async (event) => {
      const file = event.target.files[0];
      event.target.value = "";
      if (!file) return;
      let data;
      try {
        data = JSON.parse(await file.text());
      } catch (e) {
        return say("That file is not valid JSON.", true);
      }
      change("POST", "/api/sites/import", data, `Imported the pins of ${Object.keys(data).length} sites.`);
    });
    if (keyInput.value) load();
  }

  const form = $("#scrape-form");
  if (!form) return; // everything below is the workbench on the home page

  // ---- Workbench state ---------------------------------------------------------------------------
  const urlInput = $("#url");
  const keyInput = $("#key");
  const runButton = $("#run");
  const statusBar = $("#status");
  const tabBar = $("#tabs");
  const view = $("#view");
  const mode = () => $("input[name=mode]:checked").value;
  const browser = () => $("input[name=browser]:checked").value;
  const hostOf = (url) => { try { return new URL(url).hostname.replace(/^www\./, ""); } catch (e) { return ""; } };
  const kilobytes = (text) => `${(new Blob([text]).size / 1024).toFixed(1)} KB`;
  const seconds = (ms) => `${(ms / 1000).toFixed(1)} s`;
  let snippetLanguage = "curl";
  let inFlight = null;

  function setOption(name, value) {
    const input = $(`input[name=${name}][value="${value}"]`);
    if (input) input.checked = true;
    optionsChanged();
  }

  // ---- API key, kept in this browser -------------------------------------------------------------
  keyInput.value = store.get("scraper-key") || keyInput.value;
  function paintKey() { $("#keyrow").classList.toggle("set", Boolean(keyInput.value.trim())); }
  keyInput.addEventListener("input", () => {
    const key = keyInput.value.trim();
    key ? store.set("scraper-key", key) : store.remove("scraper-key");
    paintKey();
    renderSnippet();
  });
  paintKey();

  // ---- Options and the hint under them -----------------------------------------------------------
  function optionsChanged() {
    $("#hint").textContent =
      browser() === "always" ? "Always goes straight to the headless browser. Slower: a few seconds per page."
      : browser() === "never" ? "Never uses the browser, so sites with bot protection may come back blocked."
      : "Auto tries a plain request first and opens a browser only if the site blocks it.";
    renderSnippet();
  }
  $$("input[name=mode], input[name=browser]").forEach((input) => input.addEventListener("change", optionsChanged));

  // ---- Recent scrapes ----------------------------------------------------------------------------
  function recentList() {
    try { return JSON.parse(store.get("scraper-recent") || "[]"); } catch (e) { return []; }
  }
  function remember(entry) {
    const list = recentList().filter((r) => !(r.url === entry.url && r.mode === entry.mode && r.browser === entry.browser));
    list.unshift(entry);
    store.set("scraper-recent", JSON.stringify(list.slice(0, 8)));
    renderRecent();
  }
  function renderRecent() {
    const list = recentList();
    const box = $("#recent");
    if (!list.length) {
      box.innerHTML = '<p class="hint">Your scrapes show up here, kept in this browser only.</p>';
      return;
    }
    box.innerHTML = list.map((r, i) => {
      const when = new Date(r.time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      return `<button type="button" data-i="${i}"><span class="pill ${pillClass(r.status)}">${r.status}</span>`
        + `<span class="u" title="${esc(r.url)}">${esc(r.url.replace(/^https?:\/\/(www\.)?/, ""))}</span><span class="t">${when}</span></button>`;
    }).join("");
    $$("button", box).forEach((button) => button.addEventListener("click", () => {
      const r = list[button.dataset.i];
      urlInput.value = r.url;
      setOption("mode", r.mode);
      setOption("browser", r.browser);
      run();
    }));
  }
  $("#clear-recent").addEventListener("click", () => { store.remove("scraper-recent"); renderRecent(); });

  // ---- Shortcuts into a scrape: site cards, example buttons, the keyboard -----------------------
  $$(".site").forEach((site) => site.addEventListener("click", (event) => {
    event.preventDefault();
    urlInput.value = site.dataset.example;
    setOption("mode", "json");
    run();
  }));
  view.addEventListener("click", (event) => {
    const example = event.target.closest("[data-try]");
    if (!example) return;
    urlInput.value = example.dataset.try;
    setOption("mode", example.dataset.mode);
    run();
  });
  form.addEventListener("submit", (event) => { event.preventDefault(); run(); });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); run(); }
  });
  urlInput.addEventListener("input", renderSnippet);

  // ---- Status bar, tabs and messages ------------------------------------------------------------
  function pillClass(code) { return code < 300 ? "good" : code < 500 ? "warn" : "bad"; }

  function showStatus(code, via, url, size, ms) {
    statusBar.innerHTML = `<span class="pill ${pillClass(code)}">${code}</span>`
      + `<span class="meta">via <b>${esc(via)}</b></span><span class="url" title="${esc(url)}">${esc(url)}</span>`
      + `<span class="meta">${size}</span><span class="meta">${seconds(ms)}</span>`;
  }

  // tabs: [[id, label, render], ...]; tools: extra buttons on the right, as HTML
  function showTabs(tabs, tools) {
    tabBar.hidden = false;
    tabBar.innerHTML = tabs.map(([id, label], i) =>
      `<button type="button" class="tab" data-tab="${id}" aria-selected="${i === 0}">${label}</button>`).join("")
      + `<span class="spacer"></span>${tools}`;
    $$(".tab", tabBar).forEach((button) => button.addEventListener("click", () => {
      $$(".tab", tabBar).forEach((t) => t.setAttribute("aria-selected", String(t === button)));
      tabs.find(([id]) => id === button.dataset.tab)[2]();
    }));
    tabs[0][2]();
  }

  function showMessage(title, text, action, isError = true) {
    tabBar.hidden = true;
    view.innerHTML = `<div class="message${isError ? " error" : ""}"><h3>${esc(title)}</h3><p>${esc(text)}</p>`
      + (action ? `<div class="next"><button class="tool" type="button" id="message-action">${esc(action.label)}</button></div>` : "")
      + "</div>";
    if (action) $("#message-action").addEventListener("click", action.run);
  }

  function showError(code, title, text, action) {
    statusBar.innerHTML = `<span class="pill ${code ? pillClass(code) : "bad"}">${code || "error"}</span>`
      + `<span class="url">${esc(urlInput.value.trim())}</span>`;
    showMessage(title, text, action);
  }

  // ---- Running a scrape ----------------------------------------------------------------------------
  function run() {
    const url = urlInput.value.trim();
    const key = keyInput.value.trim();
    if (!/^https?:\/\//i.test(url)) {
      return showError(400, "That is not a web address", "The URL has to start with http:// or https://.");
    }
    if (!key) {
      keyInput.focus();
      return showError(401, "Add your API key first", "Paste it into the API key field. It is kept in this browser only.");
    }
    if (inFlight) inFlight.abort();
    const controller = new AbortController();
    inFlight = controller;
    const request = { url, mode: mode(), browser: browser() };
    const started = performance.now();

    runButton.disabled = true;
    tabBar.hidden = true;
    view.innerHTML = `<div class="skeleton">${[90, 70, 82, 40, 75, 60, 88, 35].map((w) => `<i style="width:${w}%"></i>`).join("")}</div>`;
    statusBar.innerHTML = `<span class="meta">Scraping</span><span class="url">${esc(url)}</span>`
      + '<span class="meta" id="elapsed">0.0 s</span><button class="tool" type="button" id="cancel">Cancel</button>';
    $("#cancel").addEventListener("click", () => controller.abort());
    const timer = setInterval(() => {
      const elapsed = $("#elapsed");
      if (elapsed) elapsed.textContent = seconds(performance.now() - started);
    }, 100);

    fetch("/scrape?" + new URLSearchParams(request), { headers: { "X-API-Key": key }, signal: controller.signal })
      .then(async (response) => {
        if (inFlight !== controller) return; // replaced by a newer scrape
        let body = null;
        try { body = await response.json(); } catch (e) { /* not JSON */ }
        const ms = performance.now() - started;
        if (inFlight !== controller) return;
        if (!response.ok || !body) return apiError(response.status, body, request);
        remember({ ...request, status: body.status_code, time: Date.now() });
        const text = request.mode === "json" ? JSON.stringify(body.parsed) : body.content || "";
        showStatus(body.status_code, body.fetched_with, body.url, kilobytes(text), ms);
        if (request.mode === "json") renderJSON(body.parsed, body.url);
        else renderHTML(body.content || "");
      })
      .catch((error) => {
        if (inFlight !== controller) return; // replaced by a newer scrape, which shows its own result
        if (error.name === "AbortError") {
          showError(0, "Cancelled", "The request keeps running on the server until it finishes, so trying again may come back from the cache.",
            { label: "Try again", run });
        } else {
          showError(0, "Could not reach the scraper", "The request never got an answer. Check your connection and try again.",
            { label: "Try again", run });
        }
      })
      .finally(() => {
        clearInterval(timer);
        if (inFlight !== controller) return;
        inFlight = null;
        runButton.disabled = false;
        renderSnippet();
      });
  }

  function apiError(status, body, request) {
    // A string for every error, except 422, where FastAPI lists the invalid parameters.
    const detail = body && (Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join(" ") : body.detail);
    const blocked = /bot protection|blocked/i.test(detail || "");
    const switchTo = (name, value, label) => ({ label, run: () => { setOption(name, value); run(); } });
    const known = {
      401: ["The API key was rejected", { label: "Check the key", run: () => keyInput.focus() }],
      400: ["The scraper rejected the request", null],
      403: ["That address is not allowed", null],
      413: ["The page is too large", null],
      415: ["That is not an HTML page", null],
      422: ["A setting is not valid", null],
      429: ["Rate limit reached", { label: "Try again", run }],
      502: [blocked ? "The site blocked the scraper" : "The page could not be fetched",
        blocked && request.browser !== "always" ? switchTo("browser", "always", "Try with the browser") : { label: "Try again", run }],
      503: ["Every headless browser is busy", { label: "Try again", run }],
    }[status] || [`Something went wrong (${status})`, null];
    if (status >= 400) remember({ ...request, status, time: Date.now() });
    showError(status, known[0], detail || "No details were returned.", known[1]);
  }

  // ---- JSON results: a sortable table for lists, a tree, and the raw JSON -------------------------
  function renderJSON(data, url) {
    const raw = JSON.stringify(data, null, 2);
    const rows = Array.isArray(data) && data.length && data.every((r) => r && typeof r === "object" && !Array.isArray(r)) ? data : null;
    const tabs = [];
    if (rows) tabs.push(["table", `Table<span class="count">${rows.length} rows</span>`, () => renderTable(rows)]);
    tabs.push(["tree", "Parsed", () => { view.innerHTML = ""; const tree = document.createElement("div"); tree.className = "tree"; tree.appendChild(treeNode(null, data)); view.appendChild(tree); }]);
    tabs.push(["raw", "Raw JSON", () => { view.innerHTML = numbered(raw.split("\n").map(esc)); }]);
    showTabs(tabs, (rows ? '<button class="tool" type="button" id="download-csv">Download CSV</button>' : "")
      + '<button class="tool" type="button" id="copy-result">Copy JSON</button>');
    $("#copy-result").addEventListener("click", (event) => copyText(raw, event.target));
    if (rows) $("#download-csv").addEventListener("click", () => download(toCSV(rows), `${hostOf(url) || "result"}.csv`, "text/csv"));
  }

  function treeNode(key, value) {
    const label = key === null ? "" : `<span class="k">${esc(key)}</span>: `;
    if (value !== null && typeof value === "object") {
      const isList = Array.isArray(value);
      const node = document.createElement("details");
      node.open = true;
      const size = isList ? `${value.length} items` : `${Object.keys(value).length} keys`;
      node.innerHTML = `<summary>${label}${isList ? "[" : "{"}<span class="count">${size}</span></summary>`;
      (isList ? value.map((v, i) => [i, v]) : Object.entries(value)).forEach(([k, v]) => node.appendChild(treeNode(String(k), v)));
      return node;
    }
    const leaf = document.createElement("div");
    leaf.className = "leaf";
    const kind = value === null ? "z" : typeof value === "number" ? "n" : typeof value === "boolean" ? "b" : "s";
    const shown = value === null ? "null" : typeof value === "string" ? `"${esc(value)}"` : esc(String(value));
    leaf.innerHTML = `${label}<span class="${kind}">${shown}</span>`;
    return leaf;
  }

  const columnsOf = (rows) => [...new Set(rows.flatMap((r) => Object.keys(r)))];
  const cellText = (v) => (Array.isArray(v) ? v.join(", ") : v === null || v === undefined ? "" : typeof v === "object" ? JSON.stringify(v) : String(v));

  function renderTable(rows, sortBy = null, ascending = true) {
    const columns = columnsOf(rows);
    const numeric = new Set(columns.filter((c) => rows.every((r) => typeof r[c] === "number" || r[c] == null)));
    const sorted = sortBy === null ? rows : [...rows].sort((a, b) => {
      const x = a[sortBy], y = b[sortBy];
      const order = numeric.has(sortBy) ? (x ?? -Infinity) - (y ?? -Infinity) : cellText(x).localeCompare(cellText(y));
      return ascending ? order : -order;
    });
    view.innerHTML = '<table class="grid"><thead><tr><th class="idx">#</th>'
      + columns.map((c) => `<th data-col="${esc(c)}" class="${numeric.has(c) ? "num" : ""}">${esc(c)}${c === sortBy ? (ascending ? " ↑" : " ↓") : ""}</th>`).join("")
      + "</tr></thead><tbody>"
      + sorted.map((r, i) => `<tr><td class="idx">${i + 1}</td>`
        + columns.map((c) => tableCell(r[c], numeric.has(c))).join("") + "</tr>").join("")
      + "</tbody></table>";
    $$("th[data-col]", view).forEach((th) => th.addEventListener("click", () =>
      renderTable(rows, th.dataset.col, th.dataset.col === sortBy ? !ascending : true)));
  }

  function tableCell(value, isNumber) {
    const text = cellText(value);
    const content = /^https?:\/\//.test(text)
      ? `<a href="${esc(text)}" target="_blank" rel="noopener noreferrer">${esc(text)}</a>`
      : esc(text);
    return `<td class="${isNumber ? "num" : ""}" title="${esc(text)}">${content}</td>`;
  }

  function toCSV(rows) {
    const columns = columnsOf(rows);
    const quote = (v) => { const s = cellText(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
    return [columns.map(quote).join(","), ...rows.map((r) => columns.map((c) => quote(r[c])).join(","))].join("\n");
  }

  // ---- HTML results: the formatted source with line numbers, or just the text ---------------------
  const numbered = (lines) => `<pre class="src">${lines.map((line) => `<span class="ln"></span>${line}`).join("\n")}</pre>`;

  function renderHTML(html) {
    const highlighted = html.split("\n").map((line) => esc(line)
      .replace(/(&lt;\/?)([a-zA-Z][a-zA-Z0-9-]*)/g, '$1<span class="tg">$2</span>')
      .replace(/\s([a-zA-Z-]+)=(&quot;)/g, ' <span class="at">$1</span>=$2'));
    showTabs([
      ["source", "HTML", () => { view.innerHTML = numbered(highlighted); }],
      ["text", "Text only", () => {
        const doc = new DOMParser().parseFromString(html, "text/html");
        $$("script, style, noscript, template", doc).forEach((node) => node.remove());
        const text = (doc.body ? doc.body.textContent : "").split("\n").map((s) => s.trim()).filter(Boolean).join("\n");
        view.innerHTML = `<pre class="text">${esc(text || "(no text on this page)")}</pre>`;
      }],
    ], '<button class="tool" type="button" id="copy-result">Copy HTML</button>');
    $("#copy-result").addEventListener("click", (event) => copyText(html, event.target));
  }

  // ---- Code snippets for the current request -----------------------------------------------------
  function renderSnippet() {
    const url = urlInput.value.trim() || "https://example.com/";
    const base = `${location.origin}/scrape`;
    const field = mode() === "json" ? "parsed" : "content";
    const snippets = {
      curl: `curl -H "X-API-Key: $SCRAPER_KEY" \\\n  "${base}?${new URLSearchParams({ url, mode: mode(), browser: browser() })}"`,
      python: `import os\nimport requests\n\nresponse = requests.get(\n    "${base}",\n    params={"url": "${url}", "mode": "${mode()}", "browser": "${browser()}"},\n`
        + `    headers={"X-API-Key": os.environ["SCRAPER_KEY"]},\n    timeout=120,\n)\nresponse.raise_for_status()\nprint(response.json()["${field}"])`,
      js: `const params = new URLSearchParams({ url: "${url}", mode: "${mode()}", browser: "${browser()}" });\n`
        + `const response = await fetch(\`${base}?\${params}\`, { headers: { "X-API-Key": SCRAPER_KEY } });\n`
        + `if (!response.ok) throw new Error((await response.json()).detail);\nconsole.log((await response.json()).${field});`,
    };
    $("#snippet").textContent = snippets[snippetLanguage];
  }
  $$(".langs [data-lang]").forEach((button) => button.addEventListener("click", () => {
    snippetLanguage = button.dataset.lang;
    $$(".langs [data-lang]").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
    renderSnippet();
  }));
  $("#copy-snippet").addEventListener("click", (event) => copyText($("#snippet").textContent, event.target));

  // ---- Start ---------------------------------------------------------------------------------------
  optionsChanged();
  renderRecent();
  // A result rendered by the server (the no-JavaScript fallback) gets the full view.
  const seed = $("#seed");
  if (seed) {
    try {
      const result = JSON.parse(seed.textContent);
      if (result.parsed !== null && result.parsed !== undefined) renderJSON(result.parsed, result.url);
      else renderHTML(result.content || "");
    } catch (e) { /* keep the plain rendering */ }
  }
})();
