/* Front end for both pages: the theme toggle and copy buttons everywhere, and the workbench on the
   home page, which calls GET /scrape directly and renders the result. */
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

  // ---- Theme: follows the system until the toggle is used, then remembers the choice ----------
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)");
  $("#theme-toggle")?.addEventListener("click", () => {
    const root = document.documentElement;
    const isDark = root.dataset.theme ? root.dataset.theme === "dark" : prefersDark.matches;
    root.dataset.theme = isDark ? "light" : "dark";
    store.set("theme", root.dataset.theme);
  });

  // ---- Copy buttons on the docs page -------------------------------------------------------------
  $$("[data-copy-target]").forEach((button) =>
    button.addEventListener("click", () => {
      const target = document.getElementById(button.dataset.copyTarget);
      if (target) copyText(target.textContent, button);
    })
  );

  const form = $("#scrape-form");
  if (!form) return; // everything below is the workbench on the home page

  // ---- Workbench state ---------------------------------------------------------------------------
  const urlInput = $("#url");
  const keyInput = $("#key");
  const runButton = $("#run");
  const statusBar = $("#status");
  const tabBar = $("#tabs");
  const view = $("#view");
  const jsonHosts = $$(".site").map((site) => site.dataset.host);
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
    if (mode() === "json" && !jsonHosts.includes(hostOf(url))) {
      return showError(400, "JSON is not available for this site",
        "Structured data works on the supported sites in the list. Any other page can be scraped as HTML.",
        { label: "Switch to HTML", run: () => { setOption("mode", "html"); run(); } });
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
    const detail = body && (typeof body.detail === "string" ? body.detail
      : body.error || (Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join(" ") : ""));
    const blocked = /bot protection|blocked/i.test(detail || "");
    const switchTo = (name, value, label) => ({ label, run: () => { setOption(name, value); run(); } });
    const known = {
      401: ["The API key was rejected", { label: "Check the key", run: () => keyInput.focus() }],
      400: [detail && detail.startsWith("JSON") ? "JSON is not available for this site" : "The scraper rejected the URL",
        request.mode === "json" ? switchTo("mode", "html", "Switch to HTML") : null],
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
