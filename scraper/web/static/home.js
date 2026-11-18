// The home page: a stored response first, then real scrapes of the example sites with the public test key.

const EXAMPLES = [
  { label: "Car listing", url: "https://webscraper.io/test-sites/product/bmw-e24-635csi-1954-c002" },
  { label: "Book listing", url: "https://books.toscrape.com/catalogue/page-2.html" },
  { label: "Quotes", url: "https://quotes.toscrape.com/page/2/" },
  { label: "Shop product", url: "https://scrapeme.live/shop/Bulbasaur/" },
];
const STEPS = { http: 1, "http-safari": 2, browser: 3, stealth: 4 };
const STEP_NAMES = { 1: "plain HTTP", 2: "Safari fingerprint", 3: "browser", 4: "stealth browser" };
const WAKING_MS = 2000;

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => document.querySelectorAll(selector);
const esc = (value) => String(value).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const hostOf = (url) => {
  try {
    return new URL(url.includes("://") ? url : `https://${url}`).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
};

const form = $("#ask");
const state = {
  key: form?.dataset.testKey || "",
  mode: "json",
  browser: "auto",
  tab: "raw",
  lang: "curl",
  url: EXAMPLES[0].url,
  body: JSON.parse($("#example").textContent), // the last response shown
  seconds: null, // null for the stored response: it wasn't timed
  error: null,
  busy: false,
};

// ---- Rendering -----------------------------------------------------------------------------

function jsonView(value) {
  const text = esc(JSON.stringify(value, null, 2));
  const colored = text.replace(/(&quot;(?:[^&]|&(?!quot;))*?&quot;)(\s*:)?|(-?\b\d+(?:\.\d+)?(?:e[+-]?\d+)?\b)|\b(true|false|null)\b/g,
    (match, str, colon, num, word) => {
      if (str) return colon ? `<span class="h-k">${str}</span><span class="h-p">${colon}</span>` : `<span class="h-s">${str}</span>`;
      return num ? `<span class="h-n">${num}</span>` : `<span class="h-p">${word}</span>`;
    });
  return `<pre>${colored}</pre>`;
}

function htmlView(html) {
  const colored = esc(html)
    .replace(/(&lt;\/?)([a-z0-9-]+)/gi, '$1<span class="h-n">$2</span>')
    .replace(/([a-z-]+)=(&quot;.*?&quot;)/gi, '<span class="h-p">$1=</span><span class="h-s">$2</span>');
  return `<pre>${colored}</pre>`;
}

function snippet() {
  const base = `${location.origin}/scrape`;
  const extra = state.browser !== "auto";
  const s = (text) => `<span class="h-s">${esc(text)}</span>`;
  const url = JSON.stringify(state.url);
  const key = JSON.stringify(state.key);
  const code = {
    curl: `curl -G "${base}" \\\n  -H "X-API-Key: ${s(state.key)}" \\\n  --data-urlencode "url=${s(state.url)}" \\\n  -d mode=${state.mode}${extra ? ` \\\n  -d browser=${state.browser}` : ""}`,
    python: `import requests\n\nresponse = requests.get(\n    "${base}",\n    params={"url": ${s(url)}, "mode": "${state.mode}"${extra ? `, "browser": "${state.browser}"` : ""}},\n    headers={"X-API-Key": ${s(key)}},\n)\nprint(response.json())`,
    js: `const params = new URLSearchParams({ url: ${s(url)}, mode: "${state.mode}"${extra ? `, browser: "${state.browser}"` : ""} });\nconst response = await fetch(\`${base}?\${params}\`, {\n  headers: { "X-API-Key": ${s(key)} },\n});\nconsole.log(await response.json());`,
  };
  const button = (lang, label) => `<button type="button" data-lang="${lang}" aria-pressed="${state.lang === lang}">${label}</button>`;
  return `<div class="h-langs">${button("curl", "curl")}${button("python", "Python")}${button("js", "JavaScript")}<button type="button" class="h-copy">Copy</button></div><pre>${code[state.lang]}</pre>`;
}

function notice(title, text) {
  return `<div class="h-notice"><b>${esc(title)}</b>${esc(text)}</div>`;
}

function render() {
  const body = state.body;
  const html = Boolean(body && body.content != null);
  $("[data-tab=raw]").textContent = html ? "HTML" : "JSON";

  // Status line
  const code = $("#code");
  if (state.busy) {
    code.textContent = "…";
    code.className = "h-pill idle";
  } else if (state.error) {
    code.textContent = String(state.error.status);
    code.className = "h-pill bad";
  } else {
    code.textContent = String(body.status_code);
    code.className = body.ok ? "h-pill" : "h-pill bad";
  }
  let size = "";
  if (body && !state.error) {
    const parsed = body.parsed;
    if (html) size = `${Math.max(1, Math.round(body.content.length / 1024))} KB of HTML`;
    else if (Array.isArray(parsed)) size = `${parsed.length} items`;
    else if (parsed) size = `${Object.keys(parsed).length} fields`;
  }
  const parts = [`<b>${esc(hostOf(state.url))}</b>`];
  if (size) parts.push(size);
  if (state.seconds != null && !state.busy) parts.push(`${state.seconds.toFixed(1)} s`);
  $("#meta").innerHTML = parts.join(" · ");

  // Response pane
  let pane;
  if (state.tab === "code") pane = snippet();
  else if (state.busy) pane = `<div class="h-loading"><span>${esc(state.busy)}</span></div>`;
  else if (state.error) pane = notice(state.error.title, state.error.text);
  else pane = html ? htmlView(body.content) : jsonView(body);
  $("#pane").innerHTML = pane;
  $$("[role=tab]").forEach((tab) => tab.setAttribute("aria-selected", String(tab.dataset.tab === state.tab)));
  $$(".h-chip").forEach((chip) => chip.setAttribute("aria-pressed", String(chip.dataset.url === state.url)));
  if (state.tab === "code") {
    $$(".h-langs [data-lang]").forEach((button) => (button.onclick = () => { state.lang = button.dataset.lang; render(); }));
    const copy = $(".h-copy");
    copy.onclick = async () => {
      try {
        await navigator.clipboard.writeText($("#pane pre").innerText);
        copy.textContent = "Copied";
      } catch {
        copy.textContent = "Select and copy";
      }
    };
  }

  // How it got the page
  const step = !state.busy && !state.error && body ? STEPS[body.fetched_with] : null;
  $$(".h-step").forEach((tile) => tile.classList.toggle("served", Number(tile.dataset.step) === step));
  let note = "";
  if (step && state.browser === "always" && step >= 3) note = `The browser was forced by the "Always" option; on Auto this site may need less.`;
  else if (step === 1) note = `Served on the first step, so no browser was started. ${hostOf(state.url)} is remembered as a plain-HTTP site.`;
  else if (step) note = `Served on step ${step}: the cheaper steps were blocked or not enough. ${hostOf(state.url)} now starts there.`;
  $("#ladder-note").textContent = note;
  if (step) {
    $("#mini-site").textContent = hostOf(state.url);
    $("#mini-step").textContent = STEP_NAMES[step];
  }
}

// ---- Scraping ------------------------------------------------------------------------------

async function scrape(url) {
  if (state.busy) return;
  state.url = url;
  state.tab = "raw";
  state.error = null;
  state.busy = `Fetching ${hostOf(url) || "the page"}…`;
  $("#go").disabled = true;
  render();
  const waking = setTimeout(() => { state.busy = "Waking the server…"; render(); }, WAKING_MS);
  const started = performance.now();
  const params = new URLSearchParams({ url, mode: state.mode });
  if (state.browser !== "auto") params.set("browser", state.browser);
  try {
    const response = await fetch(`/scrape?${params}`, { headers: { "X-API-Key": state.key } });
    const data = await response.json().catch(() => ({}));
    state.seconds = (performance.now() - started) / 1000;
    if (response.ok) state.body = data;
    else state.error = explain(response, data);
  } catch {
    state.error = { status: "—", title: "The server could not be reached", text: "Check your connection and try again." };
  } finally {
    clearTimeout(waking);
    state.busy = false;
    $("#go").disabled = false;
    render();
  }
}

function explain(response, data) {
  const detail = typeof data.detail === "string" ? data.detail : "The scrape did not go through.";
  if (response.status === 403 && detail.startsWith("The test key works on the example sites")) {
    return { status: 403, title: "The test key works on the example sites", text: "Pick one of them." };
  }
  if (response.status === 429 && detail.includes("unavailable")) {
    return { status: 429, title: "The demo is paused for a moment", text: "Its rate limits can't be checked right now. Try again shortly." };
  }
  if (response.status === 429) {
    const minutes = Math.max(1, Math.ceil(Number(response.headers.get("Retry-After") || 60) / 60));
    return { status: 429, title: "This hour's test scrapes are used up", text: `Try again in ${minutes} minute${minutes === 1 ? "" : "s"}.` };
  }
  return { status: response.status, title: "The scrape did not go through", text: detail };
}

// ---- Wiring --------------------------------------------------------------------------------

if (form) {
  for (const example of EXAMPLES) {
    const chip = Object.assign(document.createElement("button"), { type: "button", className: "h-chip", textContent: example.label });
    chip.dataset.url = example.url;
    chip.onclick = () => { $("#url").value = example.url; scrape(example.url); };
    $("#chips").append(chip);
  }
  $("#url").value = state.url;
  form.onsubmit = (event) => {
    event.preventDefault();
    const url = $("#url").value.trim();
    if (url) scrape(url.includes("://") ? url : `https://${url}`);
  };
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") form.requestSubmit();
  });
  $("#opts-btn").onclick = () => {
    const opening = $("#opts").hidden;
    $("#opts").hidden = !opening;
    $("#opts-btn").setAttribute("aria-expanded", String(opening));
  };
  // An option changes the next request and the code, but doesn't scrape: each scrape counts against the hourly limit.
  $$(".h-seg").forEach((seg) => seg.querySelectorAll("button").forEach((button) => {
    button.onclick = () => {
      seg.querySelectorAll("button").forEach((other) => other.setAttribute("aria-pressed", String(other === button)));
      state[seg.dataset.opt] = button.dataset.v;
      render();
    };
  }));
}
$$("[role=tab]").forEach((tab) => (tab.onclick = () => { state.tab = tab.dataset.tab; render(); }));
render();
