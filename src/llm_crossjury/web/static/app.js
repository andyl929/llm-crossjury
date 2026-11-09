// llm-crossjury home page: core helpers, safe Markdown, the model rack and start-up.
// Vanilla JS, no build step. The page is split into plain scripts that share the
// window.CJ namespace and load in this order: app.js (this file), sessions.js,
// jury.js, chat.js, composer.js. Start-up runs on DOMContentLoaded, after all of them.
//
// All server-provided text is inserted with textContent, never as HTML. Markdown is
// rendered by building DOM nodes (see renderMarkdown), and links are limited to
// http(s) URLs that open in a new tab without an opener.
"use strict";

(function () {
  const CJ = (window.CJ = window.CJ || {});

  const state = (CJ.state = {
    providers: [],
    defaultMaxTokens: 4096,
    budget: null,
    consentText: "",
    demo: false,
    backends: {},       // provider -> "api" | "cli" (subscription)
    cardValues: {},     // provider -> values kept across card rebuilds
    lastCost: null,     // { label, text }
    planCalls: 0,       // subscription calls made in this page session
    sessionsApi: null,  // null = unknown, true = the server keeps sessions, false = in-memory only
  });

  const $ = (id) => document.getElementById(id);
  const SPRITE = document.querySelector("svg.sprite");
  const SVG_NS = SPRITE ? SPRITE.namespaceURI : null;

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of [].concat(children || [])) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  // An icon from the page's sprite. Decorative: the control carries the text.
  function icon(name) {
    if (!SVG_NS) return null;
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    const use = document.createElementNS(SVG_NS, "use");
    use.setAttribute("href", "#i-" + name);
    svg.append(use);
    return svg;
  }

  function iconButton(name, label, onclick, extra) {
    return el("button", Object.assign({ type: "button", class: "icon-btn", "aria-label": label, title: label, onclick: onclick }, extra || {}),
      icon(name));
  }

  function money(currency, value) {
    if (value === null || value === undefined) return "unknown";
    const n = Number(value);
    const digits = n !== 0 && Math.abs(n) < 0.01 ? 4 : 2;
    const amount = n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
    return currency === "USD" || !currency ? "$" + amount : currency + " " + amount;
  }

  function moneyRange(currency, low, high) {
    return money(currency, low) + "\u2013" + money(currency, high);
  }

  function tokens(value) {
    return value === null || value === undefined ? "?" : Number(value).toLocaleString("en-US");
  }

  // 12345 -> "12.3k": for compact meters.
  function shortTokens(value) {
    const n = Number(value || 0);
    if (n >= 1000000) return (n / 1000000).toFixed(n >= 10000000 ? 0 : 1) + "M";
    if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "k";
    return String(n);
  }

  function plural(n, word) {
    return n + " " + word + (n === 1 ? "" : "s");
  }

  // Subscription (cli) calls have no known cost: they count against the plan.
  function planUsage(calls) {
    return "plan usage (" + plural(calls, "call") + ")";
  }

  function cliCalls(lines) {
    return (lines || []).filter((l) => l.backend === "cli").reduce((n, l) => n + l.calls, 0);
  }

  function hasPriced(lines) {
    return (lines || []).some((l) => l.backend !== "cli");
  }

  // Cost text for a priced range plus a number of subscription calls.
  function costText(range, priced, calls) {
    const parts = [];
    if (priced || !calls) parts.push(range);
    if (calls) parts.push(planUsage(calls));
    return parts.join(" + ");
  }

  // GET without a body, POST with one; pass a method for DELETE.
  async function api(path, body, method) {
    const verb = method || (body === undefined ? "GET" : "POST");
    const options = { method: verb };
    if (body !== undefined || verb === "POST") {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify(body === undefined ? {} : body);
    }
    let response;
    try {
      response = await fetch(path, options);
    } catch (_) {
      const error = new Error("Cannot reach the llm-crossjury server. Check that it is still running in your terminal, then try again.");
      error.status = 0;
      throw error;
    }
    let data = null;
    try { data = await response.json(); } catch (_) { data = null; }
    if (!response.ok) {
      const error = new Error(describeError(response.status, data));
      error.status = response.status;
      error.data = data;
      error.code = data && typeof data.code === "string" ? data.code : null;
      throw error;
    }
    return data;
  }

  function describeError(status, data) {
    if (data && typeof data.detail === "string") return data.detail;
    if (data && Array.isArray(data.detail)) {
      return "Invalid input: " + data.detail.map((d) => (d.loc || []).slice(1).join(".") + " " + d.msg).join("; ");
    }
    return "Request failed (HTTP " + status + ").";
  }

  // "?session=S&turn=T" (only the parts that are set).
  function query(params) {
    const parts = [];
    for (const [key, value] of Object.entries(params || {})) {
      if (value !== null && value !== undefined && value !== "") parts.push(key + "=" + encodeURIComponent(value));
    }
    return parts.length ? "?" + parts.join("&") : "";
  }

  // A fix to offer next to an error message, if the text points at one.
  function fixFor(text) {
    if (/\b(api key|key|unauthori[sz]ed|401|403|permission)\b/i.test(text)) {
      return settingsLink("Open Settings to add or test the key", "message-fix");
    }
    if (/\bbudget|daily limit|run limit\b/i.test(text)) {
      return el("span", { class: "message-fix" }, "Pick fewer or cheaper models, or lower Max output tokens under the model's settings.");
    }
    if (/\b(sign in|signed in|not found on path|cli)\b/i.test(text)) {
      return el("span", { class: "message-fix" }, "Open \u201cInstall and sign in\u201d on the model's row, then press Re-check.");
    }
    return null;
  }

  // With saved sessions, Settings is plain navigation (the conversation comes back).
  // Without them, Settings opens in a new tab so the answers on screen are not lost.
  function settingsLink(text, cls) {
    if (state.sessionsApi) return el("a", { href: "/settings", class: cls || null }, text);
    return el("a", { href: "/settings", class: cls || null, target: "_blank", rel: "noopener" },
      [text, el("span", { class: "visually-hidden", text: " (opens in a new tab)" })]);
  }

  function fillMessage(box, text) {
    box.replaceChildren();
    box.hidden = !text;
    if (!text) return;
    box.append(icon("alert"), el("div", { class: "message-body" }, [el("span", { text: text }), fixFor(text)]));
  }

  function showMessage(text) {
    const box = $("message");
    fillMessage(box, text);
    if (text) box.scrollIntoView({ block: "nearest", behavior: reducedMotion() ? "auto" : "smooth" });
  }

  function reducedMotion() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function scrollBehavior() {
    return reducedMotion() ? "auto" : "smooth";
  }

  let toastTimer = null;
  function toast(text) {
    const box = $("toast");
    box.replaceChildren(icon("check"), document.createTextNode(text));
    box.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { box.hidden = true; }, 3000);
  }

  async function copyText(text, what) {
    let ok = false;
    try {
      await navigator.clipboard.writeText(text);
      ok = true;
    } catch (_) {
      const area = el("textarea", { class: "visually-hidden", "aria-hidden": "true" });
      area.value = text;
      document.body.append(area);
      area.select();
      try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
      area.remove();
    }
    toast(ok ? what + " copied" : "Copy failed: select the text and press Ctrl+C");
  }

  // Storage survives being blocked (private windows, policies): it is a convenience.
  function storageGet(key) {
    try { return window.localStorage.getItem(key); } catch (_) { return null; }
  }

  function storageSet(key, value) {
    try {
      if (value === null || value === undefined) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, value);
    } catch (_) { /* this page only */ }
  }

  // 32 lowercase hex characters (a turn id).
  function randomId() {
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  function relativeTime(iso) {
    const then = Date.parse(iso);
    if (Number.isNaN(then)) return "";
    const secs = Math.round((Date.now() - then) / 1000);
    if (secs < 45) return "just now";
    const mins = Math.round(secs / 60);
    if (mins < 60) return mins + " min ago";
    const hours = Math.round(mins / 60);
    if (hours < 24) return hours + " h ago";
    const days = Math.round(hours / 24);
    if (days === 1) return "yesterday";
    if (days < 7) return days + " days ago";
    return new Date(then).toLocaleDateString(undefined, { day: "numeric", month: "short", year: days > 300 ? "numeric" : undefined });
  }

  // Shortcut hints only where a keyboard is likely (not on small touch screens).
  function keyboardHints() {
    return !(window.matchMedia && window.matchMedia("(pointer: coarse), (max-width: 719px)").matches);
  }

  function noteLine(kind, text) {
    return el("p", { class: "note-line note-" + kind }, [icon(kind === "err" ? "alert" : "info"), el("span", { text: text })]);
  }

  // ------------------------------------------------------------------ dialogs
  //
  // A modal question with two answers, built from DOM nodes. Resolves true when the
  // user confirms. Falls back to window.confirm where <dialog> is unsupported.

  let dialogCount = 0;
  function askDialog(opts) {
    const paragraphs = [].concat(opts.body || []).filter(Boolean);
    const probe = document.createElement("dialog");
    if (typeof probe.showModal !== "function") {
      return Promise.resolve(window.confirm([opts.title].concat(paragraphs).join("\n\n")));
    }
    return new Promise((resolve) => {
      dialogCount += 1;
      const titleId = "dialog-title-" + dialogCount;
      const ok = el("button", { type: "submit", value: "ok", class: "btn " + (opts.danger ? "btn-danger" : "btn-primary"), text: opts.confirm || "Continue" });
      const cancel = el("button", { type: "submit", value: "cancel", class: "btn", text: opts.cancel || "Cancel" });
      const form = el("form", { method: "dialog", class: "dialog-form" }, [
        el("h2", { id: titleId, class: "dialog-title", text: opts.title }),
        ...paragraphs.map((p) => (p instanceof Node ? p : el("p", { class: "dialog-text", text: p }))),
        el("div", { class: "actions dialog-actions" }, [cancel, ok]),
      ]);
      const dlg = el("dialog", { class: "dialog", "aria-labelledby": titleId }, form);
      dlg.addEventListener("close", () => {
        resolve(dlg.returnValue === "ok");
        dlg.remove();
      });
      document.body.append(dlg);
      dlg.showModal();
      (opts.danger ? cancel : ok).focus();
    });
  }

  // ------------------------------------------------------------------ attachment receipts

  // Delivery tags: how a member receives (or would receive) each file.
  const MODE_TEXT = { native: "Native", text: "As text", refused: "Not sent" };
  const MODE_CLASS = { native: "tag-ok", text: "tag-info", "text+images": "tag-info", refused: "tag-err" };

  // "text+images": the text plus derived images (rendered PDF pages, Word images).
  function derivedImagesText(r) {
    return plural(r.images || 0, r.kind === "pdf" ? "page image" : "image");
  }

  function modeText(r) {
    if (r.mode === "text+images") return "Text + " + derivedImagesText(r);
    return MODE_TEXT[r.mode] || r.mode;
  }

  function deliveryList(items) {
    if (!items || !items.length) return null;
    return el("ul", { class: "attach-modes", "aria-label": "Attachments" }, items.map((r) => el("li", {}, [
      el("span", { class: "tag " + (MODE_CLASS[r.mode] || ""), text: modeText(r) }),
      el("span", { class: "attach-name", text: r.filename }),
      ...(r.warnings || []).map((w) => el("div", { class: "attach-warn", text: w })),
    ])));
  }

  // ---- safe Markdown: a common subset rendered as DOM nodes, never as HTML

  function safeHref(raw) {
    try {
      const url = new URL(raw, window.location.href);
      return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
    } catch (_) {
      return null;
    }
  }

  const INLINE = /(`[^`]+`)|(\*\*[^*]+\*\*|(?<!\w)__[^_]+__(?!\w))|(\*[^*\s][^*]*\*|(?<!\w)_[^_\s][^_]*_(?!\w))|(\[[^\]]+\]\([^)\s]+\))|(<?\bhttps?:[^\s<>()]+[^\s<>().,;:!?'"])>?/g;

  function inlineNodes(text) {
    const nodes = [];
    let last = 0;
    let match;
    const pattern = new RegExp(INLINE.source, "g");  // per call: inlineNodes recurses
    while ((match = pattern.exec(text)) !== null) {
      if (match.index > last) nodes.push(document.createTextNode(text.slice(last, match.index)));
      const token = match[0];
      if (match[1]) {
        nodes.push(el("code", { text: token.slice(1, -1) }));
      } else if (match[2]) {
        nodes.push(el("strong", {}, inlineNodes(token.slice(2, -2))));
      } else if (match[3]) {
        nodes.push(el("em", {}, inlineNodes(token.slice(1, -1))));
      } else if (match[4]) {
        const close = token.indexOf("](");
        const label = token.slice(1, close);
        const href = safeHref(token.slice(close + 2, -1));
        nodes.push(href ? externalLink(href, inlineNodes(label)) : document.createTextNode(token));
      } else {
        const bare = match[5];
        const href = safeHref(bare);
        nodes.push(href ? externalLink(href, [bare]) : document.createTextNode(token));
      }
      last = match.index + token.length;
    }
    if (last < text.length) nodes.push(document.createTextNode(text.slice(last)));
    return nodes;
  }

  function externalLink(href, children) {
    return el("a", { href: href, target: "_blank", rel: "noopener noreferrer", title: href }, children);
  }

  function codeBlock(lang, lines) {
    const text = lines.join("\n");
    return el("div", { class: "code-block" }, [
      el("div", { class: "code-head" }, [
        el("span", { text: lang || "code" }),
        iconButton("copy", "Copy code", () => copyText(text, "Code")),
      ]),
      el("pre", {}, el("code", { text: text })),
    ]);
  }

  function splitRow(line) {
    let row = line.trim();
    if (row.startsWith("|")) row = row.slice(1);
    if (row.endsWith("|")) row = row.slice(0, -1);
    return row.split("|").map((c) => c.trim());
  }

  const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

  function newList(tag, marker) {
    // An ordered list keeps its first number (e.g. a list that starts at "3.").
    const n = parseInt(marker, 10);
    return tag === "ol" && n > 1 ? el("ol", { start: String(n) }) : el(tag);
  }

  function renderMarkdown(text) {
    const root = el("div", { class: "md" });
    const lines = String(text || "").split(/\r?\n/);
    let para = [];
    let lists = [];     // stack of { node, indent }
    let quote = null;
    const flushPara = () => {
      if (para.length) root.append(el("p", {}, inlineNodes(para.join("\n"))));
      para = [];
    };
    const flushList = () => { if (lists.length) root.append(lists[0].node); lists = []; };
    const flushQuote = () => { if (quote) root.append(el("blockquote", {}, el("p", {}, inlineNodes(quote.join("\n"))))); quote = null; };
    const flushAll = () => { flushPara(); flushList(); flushQuote(); };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const fence = /^\s*(```|~~~)\s*([\w+-]*)/.exec(line);
      if (fence) {
        flushAll();
        const body = [];
        i += 1;
        while (i < lines.length && !lines[i].trim().startsWith(fence[1])) { body.push(lines[i]); i += 1; }
        root.append(codeBlock(fence[2], body));
        continue;
      }
      if (line.includes("|") && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1]) && line.trim() !== "") {
        flushAll();
        const head = splitRow(line);
        const rows = [];
        i += 2;
        while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") { rows.push(splitRow(lines[i])); i += 1; }
        i -= 1;
        root.append(el("div", { class: "table-scroll" }, el("table", {}, [
          el("thead", {}, el("tr", {}, head.map((c) => el("th", {}, inlineNodes(c))))),
          el("tbody", {}, rows.map((r) => el("tr", {}, head.map((_, j) => el("td", {}, inlineNodes(r[j] || "")))))),
        ])));
        continue;
      }
      const heading = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line);
      const item = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(line);
      const quoted = /^\s*>\s?(.*)$/.exec(line);
      if (heading) {
        flushAll();
        // Answers sit under h3 titles, so their headings start at h4.
        const level = Math.min(6, heading[1].length + 3);
        root.append(el("h" + level, {}, inlineNodes(heading[2])));
      } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
        flushAll();
        root.append(el("hr"));
      } else if (item) {
        flushPara(); flushQuote();
        const indent = item[1].replace(/\t/g, "    ").length;
        const tag = /\d/.test(item[2]) ? "ol" : "ul";
        while (lists.length > 1 && indent < lists[lists.length - 1].indent) lists.pop();
        const top = lists[lists.length - 1];
        if (!top) {
          lists.push({ node: newList(tag, item[2]), indent: indent });
        } else if (indent > top.indent + 1 && lists.length < 3 && top.node.lastElementChild) {
          const nested = el(tag);
          top.node.lastElementChild.append(nested);
          lists.push({ node: nested, indent: indent });
        } else if (top.node.tagName.toLowerCase() !== tag) {
          if (lists.length === 1) {
            flushList();
            lists.push({ node: newList(tag, item[2]), indent: indent });
          } else {
            lists.pop();
            const nested = el(tag);
            lists[lists.length - 1].node.lastElementChild.append(nested);
            lists.push({ node: nested, indent: indent });
          }
        }
        lists[lists.length - 1].node.append(el("li", {}, inlineNodes(item[3])));
      } else if (quoted) {
        flushPara(); flushList();
        if (!quote) quote = [];
        quote.push(quoted[1]);
      } else if (line.trim() === "") {
        // A blank line ends paragraphs and quotes; a list stays open so that a loose
        // list ("1. a", blank, "2. b") renders as one list. Any non-item line closes it.
        flushPara(); flushQuote();
      } else if (lists.length && /^\s+\S/.test(line)) {
        const li = lists[lists.length - 1].node.lastElementChild;
        if (li) li.append(document.createTextNode(" "), ...inlineNodes(line.trim()));
      } else {
        flushList(); flushQuote();
        para.push(line);
      }
    }
    flushAll();
    return root;
  }

  // ------------------------------------------------------------------ providers
  //
  // Each row offers a backend: "api" (API key) or "cli" ("Subscription": the
  // vendor's official CLI the user signed in to). Switching rebuilds the row.

  function providerLabel(name) {
    const p = state.providers.find((x) => x.name === name);
    return p ? p.label : name;
  }

  function backendOf(name) {
    return state.backends[name] || "api";
  }

  function captureCards() {
    for (const p of state.providers) {
      const checkbox = $("use-" + p.name);
      if (!checkbox) continue;
      const values = state.cardValues[p.name] || { models: {} };
      values.checked = checkbox.checked;
      values.temp = $("temp-" + p.name).value;
      values.max = $("max-" + p.name).value;
      values.models[backendOf(p.name)] = $("model-" + p.name).value;
      state.cardValues[p.name] = values;
    }
  }

  function renderProviders() {
    captureCards();
    const box = $("providers");
    box.replaceChildren();
    if (!state.providers.length) {
      box.append(el("p", { class: "help rack-empty", text: "No providers are configured." }));
    }
    for (const p of state.providers) box.append(providerCard(p));
    onSelectionChange();
  }

  function setBackend(p, backend) {
    captureCards();
    state.backends[p.name] = backend;
    $("card-" + p.name).replaceWith(providerCard(p));
    onSelectionChange();
    const radio = document.querySelector("input[name='backend-" + p.name + "'][value='" + backend + "']");
    if (radio) radio.focus();
  }

  function backendToggle(p, backend) {
    const option = (value, text) => el("label", { class: "seg" + (backend === value ? " seg-on" : "") }, [
      el("input", { type: "radio", name: "backend-" + p.name, value: value, checked: backend === value,
        onchange: () => setBackend(p, value) }),
      text,
    ]);
    return el("div", { class: "backend-toggle", role: "radiogroup", "aria-label": p.label + " access" }, [
      option("api", "API key"),
      option("cli", "Subscription"),
    ]);
  }

  function cliStatusBlock(p) {
    const cli = p.cli;
    const name = cli.product + " (" + cli.command + ")" + (cli.version ? " " + cli.version : "");
    const recheck = el("button", { type: "button", class: "btn btn-small", onclick: () => loadProviders(true) },
      [icon("refresh"), "Re-check"]);
    const help = el("details", { class: "cli-help", open: !cli.available }, [
      el("summary", { text: "Install and sign in" }),
      el("p", { class: "help", text: "Install with one of the official commands:" }),
      el("ul", { class: "cli-commands" }, cli.install.map((c) => el("li", {}, el("code", { text: c })))),
      el("p", { class: "help", text: cli.sign_in }),
      el("div", { class: "actions" }, [recheck]),
    ]);
    return el("div", { class: "provider-extra" }, [
      el("p", { class: "cli-line" }, [el("span", { text: name })]),
      cli.available ? null : el("p", { class: "hint", text: cli.detail }),
      help,
    ]);
  }

  function providerCard(p) {
    const backend = p.cli ? backendOf(p.name) : "api";
    state.backends[p.name] = backend;
    const values = state.cardValues[p.name] || { models: {} };
    const isCli = backend === "cli";
    const models = isCli ? p.cli.models : p.models;
    const defaultModel = isCli ? p.cli.default_model : p.default_model;
    const keyMissing = !isCli && p.key_status === "not set";
    const cliMissing = isCli && !p.cli.available;
    const noModels = models.length === 0;
    const disabled = keyMissing || cliMissing || noModels;

    const checkbox = el("input", { type: "checkbox", id: "use-" + p.name, disabled: disabled,
      checked: !disabled && !!values.checked, onchange: onSelectionChange,
      "aria-describedby": "lamp-" + p.name });
    checkbox.dataset.provider = p.name;

    let lamp;
    if (isCli) {
      lamp = cliMissing ? ["lamp-err", "CLI not found"] : ["lamp-ok", "CLI ready"];
    } else if (p.key_status === "not required") {
      lamp = ["lamp-ok", "Ready"];
    } else {
      lamp = keyMissing ? ["lamp-warn", "No API key"] : ["lamp-ok", "Key set"];
    }
    if (!keyMissing && !cliMissing && noModels) lamp = ["lamp-warn", "No usable models"];
    const tags = [];
    if (isCli && p.cli.experimental) tags.push(el("span", { class: "tag tag-warn", text: "Experimental" }));

    const select = el("select", { id: "model-" + p.name, disabled: disabled, "aria-label": p.label + " model", onchange: onSelectionChange });
    if (noModels) select.append(el("option", { text: "No usable models" }));
    const kept = values.models[backend];
    const chosen = models.some((m) => m.id === kept) ? kept : defaultModel;
    for (const m of models) {
      select.append(el("option", { value: m.id, text: m.label, selected: m.id === chosen }));
    }

    const temperature = el("input", {
      id: "temp-" + p.name, type: "number", min: "0", step: "0.1", placeholder: "default", inputmode: "decimal",
      value: values.temp || null, disabled: disabled, oninput: onSelectionChange,
    });
    const maxTokens = el("input", {
      id: "max-" + p.name, type: "number", min: "1", step: "1", inputmode: "numeric",
      value: values.max !== undefined ? values.max : String(state.defaultMaxTokens),
      disabled: disabled, oninput: onSelectionChange,
    });
    const settings = el("details", { class: "settings" }, [
      el("summary", { text: "Options" }),
      el("div", { class: "settings-grid" }, [
        el("label", {}, ["Temperature", temperature]),
        el("label", {}, ["Max output tokens", maxTokens]),
      ]),
    ]);

    let hint = null;
    if (keyMissing) {
      hint = el("p", { class: "hint" }, [settingsLink("Add an API key for " + p.label + " in Settings"),
        p.cli ? ", or switch to Subscription." : "."]);
    } else if (noModels && isCli) {
      if (!cliMissing) hint = el("p", { class: "hint", text: "No usable models: add a registry entry with the cli backend and cli_flags." });
    } else if (noModels) {
      hint = el("p", { class: "hint", text: "No usable models: add a verified api entry with pricing to the model registry." });
    }

    return el("div", { class: "provider" + (disabled ? " disabled" : ""), id: "card-" + p.name }, [
      el("label", { class: "provider-head", for: "use-" + p.name }, [
        checkbox,
        el("span", { class: "provider-name", text: p.label }),
        el("span", { class: "lamp " + lamp[0], id: "lamp-" + p.name, text: lamp[1] }),
        ...tags,
      ]),
      p.cli ? backendToggle(p, backend) : null,
      el("div", { class: "provider-body" }, [select, settings]),
      p.subscription_note || hint || isCli
        ? el("div", { class: "provider-extra" }, [
          p.subscription_note ? el("p", { class: "backend-note", text: p.subscription_note }) : null,
          hint,
        ])
        : null,
      isCli ? cliStatusBlock(p) : null,
    ]);
  }

  function selections() {
    const result = [];
    for (const p of state.providers) {
      const checkbox = $("use-" + p.name);
      if (!checkbox || !checkbox.checked || checkbox.disabled) continue;
      const temp = $("temp-" + p.name).value.trim();
      const maxTokens = $("max-" + p.name).value.trim();
      result.push({
        provider: p.name,
        backend: backendOf(p.name),
        model: $("model-" + p.name).value,
        temperature: temp === "" ? null : Number(temp),
        max_output_tokens: maxTokens === "" ? state.defaultMaxTokens : Number(maxTokens),
      });
    }
    return result;
  }

  // Select exactly these models in the rack (e.g. the previous jury's), keeping
  // each provider's other options.
  function applySelections(sels) {
    captureCards();
    for (const p of state.providers) {
      const sel = (sels || []).find((s) => s.provider === p.name);
      const values = state.cardValues[p.name] || { models: {} };
      values.checked = !!sel;
      if (sel) {
        if (sel.backend === "cli" && !p.cli) continue;
        state.backends[p.name] = sel.backend;
        values.models[sel.backend] = sel.model;
        if (sel.temperature !== null && sel.temperature !== undefined) values.temp = String(sel.temperature);
        if (sel.max_output_tokens) values.max = String(sel.max_output_tokens);
      }
      state.cardValues[p.name] = values;
    }
    const box = $("providers");
    box.replaceChildren(...state.providers.map(providerCard));
    onSelectionChange();
  }

  function onSelectionChange() {
    let count = 0;
    for (const p of state.providers) {
      const card = $("card-" + p.name);
      const checkbox = $("use-" + p.name);
      const on = !!(checkbox && checkbox.checked && !checkbox.disabled);
      if (on) count += 1;
      if (card) card.classList.toggle("selected", on);
    }
    $("models-count").textContent = state.providers.length ? count + " of " + state.providers.length + " selected" : "";
    if (CJ.composer) CJ.composer.invalidateEstimate();
  }

  // Models a single chat can use: every usable api model with a key, and every cli
  // model of a CLI that answered its check.
  function chatModelOptions() {
    const options = [];
    for (const p of state.providers) {
      if (p.key_status !== "not set") {
        for (const m of p.models) options.push({ provider: p.name, backend: "api", model: m.id, label: p.label + ": " + m.label, group: p.label });
      }
      if (p.cli && p.cli.available) {
        for (const m of p.cli.models) options.push({ provider: p.name, backend: "cli", model: m.id, label: p.label + ": " + m.label + " (subscription)", group: p.label });
      }
    }
    return options;
  }

  async function loadProviders(refresh) {
    try {
      const data = await api("/api/providers" + (refresh ? "?refresh=true" : ""));
      state.providers = data.providers;
      state.defaultMaxTokens = data.default_max_output_tokens;
      state.budget = data.budget;
      state.consentText = data.consent_text;
      state.demo = data.demo === true;
      $("demo-badge").hidden = data.demo !== true;
      $("consent-text").textContent = data.consent_text;
      renderProviders();
      if (CJ.composer) CJ.composer.onProvidersChanged();
    } catch (error) {
      if (!state.providers.length) {
        $("providers").replaceChildren(el("p", { class: "help rack-empty", text: "Could not load the models." }));
      }
      showMessage(error.message);
    }
    if (CJ.composer) CJ.composer.updateButtons();
  }

  // Back from Settings (another tab): pick up new keys without losing the selection.
  async function refreshProvidersIfChanged() {
    if (document.visibilityState !== "visible" || !state.providers.length) return;
    if (CJ.composer && CJ.composer.isBusy()) return;
    try {
      const data = await api("/api/providers");
      state.budget = data.budget;
      if (CJ.composer) CJ.composer.renderStrip();
      if (JSON.stringify(data.providers) !== JSON.stringify(state.providers)) {
        state.providers = data.providers;
        renderProviders();
        if (CJ.composer) CJ.composer.onProvidersChanged();
      }
    } catch (_) { /* keep what is on screen */ }
  }

  // ------------------------------------------------------------------ keyboard

  // Ctrl/Cmd+Enter: inside a synthesis panel it synthesizes that turn; elsewhere it
  // runs the composer's primary action. In jury mode that estimates first and runs on
  // a second press, so money is never spent before the estimate has been on screen.
  function onShortcut(event) {
    if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey) return;
    if (event.isComposing || event.keyCode === 229) return;
    const panel = document.activeElement && document.activeElement.closest
      ? document.activeElement.closest(".synthesis") : null;
    event.preventDefault();
    if (panel) {
      const btn = panel.querySelector(".syn-btn");
      if (btn && !btn.disabled) btn.click();
      return;
    }
    if (CJ.composer) CJ.composer.primaryAction();
  }

  function initShortcutHints() {
    const mac = /Mac|iPhone|iPad/.test(navigator.platform || "");
    if (!mac) return;
    for (const node of document.querySelectorAll("kbd")) {
      if (node.textContent === "Ctrl") node.textContent = "\u2318";
    }
  }

  // ------------------------------------------------------------------ init

  async function maybeOnboard() {
    // First run (no key from any source, wizard not dismissed): open the setup wizard.
    try {
      const resp = await fetch("/api/onboarding", { headers: { Accept: "application/json" } });
      if (!resp.ok) return false;
      const data = await resp.json();
      if (data && data.show === true) {
        location.assign("/settings?onboarding=1");
        return true;
      }
    } catch (err) {
      // Onboarding is optional; the main page works without it.
    }
    return false;
  }

  Object.assign(CJ, {
    $: $, el: el, icon: icon, iconButton: iconButton,
    money: money, moneyRange: moneyRange, tokens: tokens, shortTokens: shortTokens, plural: plural,
    planUsage: planUsage, cliCalls: cliCalls, hasPriced: hasPriced, costText: costText,
    api: api, query: query, describeError: describeError, fixFor: fixFor, settingsLink: settingsLink,
    fillMessage: fillMessage, showMessage: showMessage, reducedMotion: reducedMotion, scrollBehavior: scrollBehavior,
    toast: toast, copyText: copyText, storageGet: storageGet, storageSet: storageSet,
    randomId: randomId, relativeTime: relativeTime, keyboardHints: keyboardHints, noteLine: noteLine,
    askDialog: askDialog, deliveryList: deliveryList, modeText: modeText, derivedImagesText: derivedImagesText,
    renderMarkdown: renderMarkdown, providerLabel: providerLabel, backendOf: backendOf,
    selections: selections, applySelections: applySelections, chatModelOptions: chatModelOptions, loadProviders: loadProviders,
  });

  async function init() {
    // The page's other scripts (sessions, jury, chat, composer) must have loaded.
    if (!CJ.sessions || !CJ.jury || !CJ.chat || !CJ.composer) {
      showMessage("Part of this page did not load. Stop llm-crossjury in your terminal, start it again, then reload this page.");
      return;
    }
    if (await maybeOnboard()) return;
    document.addEventListener("keydown", onShortcut);
    document.addEventListener("visibilitychange", () => {
      refreshProvidersIfChanged();
      if (CJ.sessions && document.visibilityState === "visible") CJ.sessions.refresh();
    });
    // Without saved sessions the conversation lives only in this page: ask before
    // leaving it once there is something to lose.
    window.addEventListener("beforeunload", (event) => {
      if (state.sessionsApi !== false || !CJ.sessions || !CJ.sessions.hasTurns()) return;
      event.preventDefault();
      event.returnValue = "";
    });
    initShortcutHints();
    CJ.composer.init();
    CJ.sessions.initSidebar();
    await loadProviders(false);
    await CJ.sessions.start();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
