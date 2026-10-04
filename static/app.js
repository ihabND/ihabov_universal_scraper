/* Front page behaviour: calls GET /scrape directly, renders the result, keeps the key and recent scrapes in this browser. */
(function () {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
    del(k) { try { localStorage.removeItem(k); } catch (e) {} },
  };

  // ---- Copy buttons work on every page (docs too) ----
  function copyText(text, btn) {
    const done = () => { const old = btn.textContent; btn.textContent = "Copied"; setTimeout(() => (btn.textContent = old), 1200); };
    const fallback = () => {
      const ta = document.createElement("textarea"); ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); done(); } catch (e) {}
      document.body.removeChild(ta);
    };
    try { navigator.clipboard.writeText(text).then(done, fallback); } catch (e) { fallback(); }
  }
  $$("[data-copy-target]").forEach(btn => btn.addEventListener("click", () => {
    const el = document.getElementById(btn.dataset.copyTarget);
    if (el) copyText(el.textContent, btn);
  }));

  const form = $("#scrape-form");
  if (!form) return; // docs page: only the copy buttons above

  // ---- Elements and state ----
  const urlEl = $("#url"), goBtn = $("#go"), keyEl = $("#key"), keyfield = $("#keyfield"), hintEl = $("#hint");
  const stripEl = $("#strip"), tabsEl = $("#tabs"), bodyEl = $("#body"), snipEl = $("#snipcode");
  const mode = () => (form.querySelector("input[name=mode]:checked") || {}).value || "html";
  const browser = () => (form.querySelector("input[name=browser]:checked") || {}).value || "auto";
  const SITES = JSON.parse($("#sites-data").textContent);
  let lang = "curl", controller = null, timer = null;

  // ---- API key, kept in this browser ----
  keyEl.value = store.get("scraper-key") || keyEl.value || "";
  function paintKey() { keyfield.classList.toggle("set", !!keyEl.value.trim()); $("#keystate").textContent = keyEl.value.trim() ? "saved" : "not set"; }
  keyEl.addEventListener("input", () => { const k = keyEl.value.trim(); k ? store.set("scraper-key", k) : store.del("scraper-key"); paintKey(); renderSnippet(); });
  paintKey();

  // ---- Options ----
  function paintHint() {
    hintEl.textContent = browser() === "always"
      ? "Always goes straight to the headless browser. Slower: a few seconds per page. The key stays in this browser."
      : mode() === "json" ? "JSON works on the supported sites below. Auto uses the browser only when a site blocks the plain fetch. The key stays in this browser."
      : "HTML returns the page's formatted source. Auto uses the browser only when a site blocks the plain fetch. The key stays in this browser.";
    renderSnippet();
  }
  $$("input[name=mode], input[name=browser]", form).forEach(r => r.addEventListener("change", paintHint));
  function setOption(name, value) { const r = form.querySelector('input[name=' + name + '][value="' + value + '"]'); if (r) { r.checked = true; } }

  // ---- Helpers ----
  const hostOf = url => { try { return new URL(url).hostname.replace(/^www\./, ""); } catch (e) { return ""; } };
  const kb = n => n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : (n / 1024).toFixed(1) + " KB";
  const secs = ms => (ms / 1000).toFixed(1) + " s";
  const viaIcon = via => via === "browser"
    ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="3"/><path d="M3 9h18"/></svg>browser'
    : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M4 12h16M4 6h16M4 18h16"/></svg>http';
  const statusChip = code => '<span class="chip code ' + (code < 300 ? "ok" : code < 500 ? "warn" : "bad") + '">' + code + '</span>';
  const strip = html => { stripEl.innerHTML = html; };
  const ago = t => { const m = Math.round((Date.now() - t) / 60000); return m < 1 ? "just now" : m < 60 ? m + " min ago" : m < 1440 ? Math.round(m / 60) + " h ago" : Math.round(m / 1440) + " d ago"; };

  // ---- Recent scrapes ----
  function recentList() { try { return JSON.parse(store.get("scraper-recent") || "[]"); } catch (e) { return []; } }
  function remember(entry) {
    const list = recentList().filter(r => !(r.url === entry.url && r.mode === entry.mode && r.browser === entry.browser));
    list.unshift(entry); store.set("scraper-recent", JSON.stringify(list.slice(0, 8))); renderRecent();
  }
  function renderRecent() {
    const el = $("#recent"), list = recentList(); el.innerHTML = "";
    if (!list.length) { el.innerHTML = '<div class="none">Nothing yet. Your scrapes will show up here.</div>'; return; }
    list.forEach(r => {
      const b = document.createElement("button"); b.type = "button";
      b.innerHTML = statusChip(r.status) + '<span class="url" title="' + esc(r.url) + '">' + esc(r.url.replace(/^https?:\/\/(www\.)?/, "")) + '</span><span class="when">' + esc(ago(r.t)) + '</span>';
      b.addEventListener("click", () => { urlEl.value = r.url; setOption("mode", r.mode); setOption("browser", r.browser); paintHint(); run(); });
      el.appendChild(b);
    });
  }
  renderRecent();
  $("#clear-recent").addEventListener("click", () => { store.del("scraper-recent"); renderRecent(); });

  // ---- Site cards: fill the form instead of navigating ----
  $$(".site[data-example]").forEach(a => a.addEventListener("click", e => {
    e.preventDefault(); urlEl.value = a.dataset.example; setOption("mode", "json"); paintHint(); run();
  }));

  // ---- Running a scrape ----
  function run() {
    const url = urlEl.value.trim();
    if (!/^https?:\/\//i.test(url)) { showError(400, "That is not a web address", "The URL has to start with http:// or https://."); return; }
    if (!keyEl.value.trim()) { showError(401, "Add your API key first", "Paste it into the API key field above. It is kept in this browser only."); keyEl.focus(); return; }
    if (mode() === "json" && !SITES.some(s => s.host === hostOf(url))) {
      showError(400, "JSON is not available for this site", "Structured data only works on the supported sites listed below. Other pages can still be scraped as HTML.",
                { label: "Switch to HTML mode", act: () => { setOption("mode", "html"); paintHint(); run(); } });
      return;
    }
    window.scrollTo({ top: $(".result").offsetTop - 80, behavior: "smooth" });
    const m = mode(), b = browser();
    goBtn.disabled = true; tabsEl.hidden = true;
    strip('<span class="chip">' + esc(m.toUpperCase()) + '</span><span class="final">' + esc(url) + '</span>');
    const steps = b === "always" ? ["Opening the page in the headless browser", "Parsing"] : ["Fetching the page", "Parsing"];
    bodyEl.innerHTML = '<div class="progress"><div class="head"><span>Scraping…</span><span class="elapsed" id="elapsed">0.0 s</span></div><div class="steps">'
      + steps.map(s => '<div class="step"><span class="ico"></span><span>' + esc(s) + '</span></div>').join("")
      + '</div><div class="note" id="note">' + (b === "never" ? "The browser is off for this request." : "If the site blocks the plain fetch, the scraper switches to the headless browser. That can take up to a minute.") + '</div>'
      + '<button class="tool" id="cancel" type="button" style="justify-self:start">Cancel</button></div>';
    const stepEls = $$(".step", bodyEl); stepEls[0].classList.add("active");
    const t0 = performance.now();
    clearInterval(timer);
    timer = setInterval(() => {
      const t = performance.now() - t0; $("#elapsed").textContent = secs(t);
      if (t > 26000 && b === "auto") $("#note").textContent = "The plain fetch budget is over, so the scraper is most likely in the headless browser now.";
    }, 100);
    controller = new AbortController();
    $("#cancel").addEventListener("click", () => { controller.abort(); });

    const params = new URLSearchParams({ url, mode: m, browser: b });
    fetch("/scrape?" + params, { headers: { "X-API-Key": keyEl.value.trim() }, signal: controller.signal })
      .then(async res => {
        stepEls[0].classList.replace("active", "done"); stepEls[1].classList.add("active");
        let data = null; try { data = await res.json(); } catch (e) {}
        const ms = performance.now() - t0;
        clearInterval(timer); goBtn.disabled = false;
        if (!res.ok || !data) { apiError(res.status, data, url, m, b); return; }
        const size = m === "json" ? JSON.stringify(data.parsed).length : (data.content || "").length;
        remember({ url, mode: m, browser: b, status: data.status_code, via: data.fetched_with, t: Date.now() });
        strip(statusChip(data.status_code) + '<span class="chip">' + viaIcon(data.fetched_with) + '</span><span class="final" title="' + esc(data.url) + '">' + esc(data.url) + '</span>'
          + '<span class="num">' + kb(size) + '</span><span class="num">' + secs(ms) + '</span>');
        if (m === "json") renderJSON(data.parsed); else renderHTML(data.content || "");
      })
      .catch(err => {
        clearInterval(timer); goBtn.disabled = false;
        if (err.name === "AbortError") empty("Cancelled. The request keeps running on the server until it finishes, so a retry may come back from the cache.");
        else showError(0, "Could not reach the scraper", "The request never got a response. Check your connection and try again.", { label: "Retry", act: run });
      });
  }
  form.addEventListener("submit", e => { e.preventDefault(); run(); });

  function apiError(status, data, url, m, b) {
    const detail = data && (typeof data.detail === "string" ? data.detail : data.error || (Array.isArray(data.detail) ? data.detail.map(d => d.msg).join(" ") : ""));
    const t = {
      401: ["The API key was rejected", { label: "Check the key", act: () => keyEl.focus() }],
      400: [detail && detail.startsWith("JSON") ? "JSON is not available for this site" : "The scraper rejected the URL", m === "json" ? { label: "Switch to HTML mode", act: () => { setOption("mode", "html"); paintHint(); run(); } } : null],
      403: ["That address is not allowed", null],
      413: ["The page is too large", null],
      415: ["That is not an HTML page", null],
      422: ["A parameter is wrong", null],
      429: ["Rate limit reached", { label: "Retry in a minute", act: run }],
      502: [/bot protection|blocked/i.test(detail || "") && b !== "always" ? "The site blocked the fetch" : "The page could not be fetched",
            /bot protection|blocked/i.test(detail || "") && b !== "always" ? { label: "Retry with Browser: Always", act: () => { setOption("browser", "always"); paintHint(); run(); } } : null],
      503: ["Every headless browser is busy", { label: "Retry now", act: run }],
    }[status] || ["Something went wrong (" + status + ")", null];
    if (status >= 400) remember({ url, mode: m, browser: b, status, via: "http", t: Date.now() });
    showError(status, t[0], detail || "No details were returned.", t[1]);
  }

  function showError(code, title, text, action) {
    clearInterval(timer); goBtn.disabled = false; tabsEl.hidden = true;
    const warn = code && code < 500 && code !== 401;
    strip('<span class="chip code ' + (warn ? "warn" : "bad") + '">' + (code || "—") + '</span><span class="final">' + esc(urlEl.value.trim()) + '</span>');
    bodyEl.innerHTML = '<div class="error"><div class="head"><span class="code' + (warn ? " warn" : "") + '">' + (code || "network") + '</span><h3>' + esc(title) + '</h3></div><p>' + esc(text) + '</p>'
      + (action ? '<div class="next"><span class="eyebrow">What to do next</span><button class="tool" type="button" id="errbtn">' + esc(action.label) + '</button></div>' : '') + '</div>';
    if (action) $("#errbtn").addEventListener("click", action.act);
  }
  function empty(text) { tabsEl.hidden = true; bodyEl.innerHTML = '<div class="empty"><strong>Nothing to show</strong><span>' + esc(text) + '</span></div>'; }

  // ---- Renderers ----
  function renderJSON(data) {
    tabsEl.hidden = false;
    tabsEl.innerHTML = '<button type="button" data-view="tree" aria-selected="true">Parsed</button><button type="button" data-view="raw" aria-selected="false">Raw JSON</button><span class="spacer"></span><button class="tool" type="button" id="expand">Collapse all</button><button class="tool" type="button" id="copy">Copy JSON</button>';
    const tree = document.createElement("div"); tree.className = "tree"; tree.appendChild(node(null, data, true));
    const rawText = JSON.stringify(data, null, 2);
    const views = { tree: () => { bodyEl.innerHTML = ""; bodyEl.appendChild(tree); }, raw: () => { bodyEl.innerHTML = '<pre class="code">' + esc(rawText) + '</pre>'; } };
    views.tree();
    wireTabs(views);
    let open = true;
    $("#expand").addEventListener("click", e => { open = !open; $$("details", tree).forEach(d => (d.open = open)); e.target.textContent = open ? "Collapse all" : "Expand all"; });
    $("#copy").addEventListener("click", e => copyText(rawText, e.target));
  }
  function node(key, value, open) {
    const label = key === null ? "" : '<span class="k">' + esc(key) + '</span>: ';
    if (value !== null && typeof value === "object") {
      const isArr = Array.isArray(value), d = document.createElement("details"); d.open = !!open;
      const n = isArr ? value.length : Object.keys(value).length;
      d.innerHTML = '<summary>' + label + (isArr ? "[" : "{") + '<span class="count">' + n + (isArr ? " items" : " keys") + '</span></summary>';
      (isArr ? value.map((v, i) => [i, v]) : Object.entries(value)).forEach(([k, v]) => d.appendChild(node(String(k), v, true)));
      return d;
    }
    const div = document.createElement("div"); div.className = "leaf";
    const cls = value === null ? "z" : typeof value === "number" ? "n" : typeof value === "boolean" ? "b" : "s";
    const shown = value === null ? "null" : typeof value === "string" ? '"' + esc(value) + '"' : esc(String(value));
    div.innerHTML = label + '<span class="' + cls + '">' + shown + '</span>';
    return div;
  }
  function renderHTML(html) {
    tabsEl.hidden = false;
    tabsEl.innerHTML = '<button type="button" data-view="preview" aria-selected="true">Preview</button><button type="button" data-view="source" aria-selected="false">Source</button><button type="button" data-view="text" aria-selected="false">Text only</button><span class="spacer"></span><button class="tool" type="button" id="copy">Copy HTML</button>';
    const lines = html.split("\n");
    const highlighted = lines.map(l => '<span class="ln"></span>' + esc(l)
      .replace(/(&lt;\/?)([a-zA-Z][a-zA-Z0-9-]*)/g, '$1<span class="tag">$2</span>')
      .replace(/\s([a-zA-Z-]+)=(&quot;[^&]*&quot;)/g, ' <span class="attr">$1</span>=$2')).join("\n");
    const views = {
      preview: () => {
        const frame = document.createElement("iframe"); frame.className = "preview"; frame.setAttribute("sandbox", ""); frame.setAttribute("title", "Rendered page, scripts off");
        frame.srcdoc = '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:;">' + html;
        bodyEl.innerHTML = ""; bodyEl.appendChild(frame);
        const p = document.createElement("p"); p.className = "hint"; p.style.cssText = "margin:10px 0 0;text-align:left"; p.textContent = "Rendered without scripts or external requests, so images and styles from the site are not loaded.";
        bodyEl.appendChild(p);
      },
      source: () => { bodyEl.innerHTML = '<pre class="code lines">' + highlighted + '</pre>'; },
      text: () => {
        const doc = new DOMParser().parseFromString(html, "text/html");
        $$("script, style, noscript, template", doc).forEach(n => n.remove());
        const text = (doc.body ? doc.body.innerText || doc.body.textContent : "").split("\n").map(s => s.trim()).filter(Boolean).join("\n");
        bodyEl.innerHTML = '<pre class="code">' + esc(text || "(no text on this page)") + '</pre>';
      },
    };
    views.preview();
    wireTabs(views);
    $("#copy").addEventListener("click", e => copyText(html, e.target));
  }
  function wireTabs(views) {
    $$("[data-view]", tabsEl).forEach(b => b.addEventListener("click", () => {
      $$("[data-view]", tabsEl).forEach(x => x.setAttribute("aria-selected", "false")); b.setAttribute("aria-selected", "true"); views[b.dataset.view]();
    }));
  }

  // ---- Code snippets for the current request ----
  function renderSnippet() {
    const url = urlEl.value.trim() || "https://example.com/", m = mode(), b = browser();
    const key = keyEl.value.trim() ? "••••••••" : "YOUR_API_KEY", base = location.origin + "/scrape";
    const field = m === "json" ? "parsed" : "content";
    const code = {
      curl: 'curl -H "X-API-Key: ' + key + '" \\\n  "' + base + '?url=' + encodeURIComponent(url) + '&mode=' + m + '&browser=' + b + '"',
      python: 'import requests\n\nresponse = requests.get(\n    "' + base + '",\n    params={"url": "' + url + '", "mode": "' + m + '", "browser": "' + b + '"},\n    headers={"X-API-Key": "' + key + '"},\n    timeout=120,\n)\nresponse.raise_for_status()\nprint(response.json()["' + field + '"])',
      js: 'const params = new URLSearchParams({ url: "' + url + '", mode: "' + m + '", browser: "' + b + '" });\nconst response = await fetch(`' + base + '?${params}`, {\n  headers: { "X-API-Key": "' + key + '" },\n});\nif (!response.ok) throw new Error((await response.json()).detail);\nconsole.log((await response.json()).' + field + ');',
    };
    snipEl.textContent = code[lang];
  }
  $$("[data-lang]").forEach(b => b.addEventListener("click", () => { lang = b.dataset.lang; $$("[data-lang]").forEach(x => x.setAttribute("aria-pressed", String(x === b))); renderSnippet(); }));
  $("#copysnip").addEventListener("click", e => copyText(snipEl.textContent, e.target));
  urlEl.addEventListener("input", renderSnippet);
  paintHint();

  // A result rendered by the server (no-JS fallback) gets the rich view on load.
  const seed = $("#seed");
  if (seed) {
    try {
      const data = JSON.parse(seed.textContent);
      if (data.parsed !== null && data.parsed !== undefined) renderJSON(data.parsed); else if (data.content) renderHTML(data.content);
    } catch (e) {}
  }
})();
