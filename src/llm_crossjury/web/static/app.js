// llm-crossjury compare UI. Vanilla JS, no build step.
// All server-provided text is inserted with textContent, never as HTML. Markdown is
// rendered by building DOM nodes (see renderMarkdown), and links are limited to
// http(s) URLs that open in a new tab without an opener.
"use strict";

(function () {
  const state = {
    providers: [],
    defaultMaxTokens: 4096,
    budget: null,
    consentText: "",
    backends: {},       // provider -> "api" | "cli" (subscription)
    cardValues: {},     // provider -> values kept across card rebuilds
    estimate: null,     // last estimate response
    estimateKey: null,  // JSON of the request the estimate belongs to
    staleEstimate: false,
    busy: false,
    attachments: [],    // uploaded files: AttachmentInfo from /api/attachments
    uploading: 0,       // uploads in flight
    lastCost: null,     // { label, text }
    planCalls: 0,       // subscription calls made in this page session
    run: null,          // { task, attachments, result } of the last step-1 run
    syncScroll: false,
  };

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
    return currency === "USD" ? "$" + amount : currency + " " + amount;
  }

  function moneyRange(currency, low, high) {
    return money(currency, low) + "\u2013" + money(currency, high);
  }

  function tokens(value) {
    return value === null || value === undefined ? "?" : Number(value).toLocaleString("en-US");
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

  async function api(path, body) {
    const options = body === undefined
      ? { method: "GET" }
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
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

  // A fix to offer next to an error message, if the text points at one.
  function fixFor(text) {
    if (/\b(api key|key|unauthori[sz]ed|401|403|permission)\b/i.test(text)) {
      return el("a", { href: "/settings", class: "message-fix" }, "Open Settings to add or test the key");
    }
    if (/\bbudget|daily limit|run limit\b/i.test(text)) {
      return el("span", { class: "message-fix" }, "Pick fewer or cheaper models, or lower Max output tokens under the model's settings.");
    }
    if (/\b(sign in|signed in|not found on path|cli)\b/i.test(text)) {
      return el("span", { class: "message-fix" }, "Open \u201cInstall and sign in\u201d on the model's row, then press Re-check.");
    }
    return null;
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

  // ------------------------------------------------------------------ control strip

  function readout(id, value, sub, cls) {
    const box = $(id);
    box.querySelector(".readout-value").textContent = value;
    box.querySelector(".readout-sub").textContent = sub || "";
    box.classList.toggle("is-stale", cls === "stale");
    box.classList.toggle("is-refused", cls === "refused");
  }

  function renderStrip() {
    const est = state.estimate;
    if (est) {
      const range = moneyRange(est.currency, est.total_low, est.total_high);
      const priced = hasPriced(est.lines);
      const value = priced || !est.cli_calls ? range : plural(est.cli_calls, "plan call");
      const sub = est.budget.refused ? "Refused by the budget"
        : (priced && est.cli_calls ? "+ " + plural(est.cli_calls, "plan call") : plural(est.lines.length, "model"));
      readout("strip-estimate", value, sub, est.budget.refused ? "refused" : null);
    } else if (state.staleEstimate) {
      readout("strip-estimate", "\u2013", "Changed: estimate again", "stale");
    } else {
      readout("strip-estimate", "\u2013", "Not estimated");
    }
    if (state.lastCost) readout("strip-last", state.lastCost.text, state.lastCost.label);

    const b = state.budget;
    if (b) {
      readout("strip-today", money(b.currency, b.spent_today), "of " + money(b.currency, b.daily_limit) + ", " +
        money(b.currency, b.daily_remaining) + " left");
      const gauge = $("strip-today").querySelector(".gauge");
      const ratio = Number(b.daily_limit) > 0 ? Math.min(1, Number(b.spent_today) / Number(b.daily_limit)) : 0;
      const on = Math.round(ratio * 20);
      gauge.replaceChildren(...Array.from({ length: 20 }, (_, i) => el("i", { class: i < on || (i === 0 && ratio > 0) ? "on" : null })));
    }
    const planBox = $("strip-plan");
    planBox.hidden = state.planCalls === 0;
    planBox.querySelector(".readout-value").textContent = String(state.planCalls);
  }

  function setLastCost(label, text) {
    state.lastCost = { label: label, text: text };
    renderStrip();
  }

  async function refreshBudget() {
    try {
      const data = await api("/api/providers");
      state.budget = data.budget;
      renderStrip();
    } catch (_) { /* keep the old numbers */ }
  }

  // ------------------------------------------------------------------ providers
  //
  // Each row offers a backend: "api" (API key) or "cli" ("Subscription": the
  // vendor's official CLI the user signed in to). Switching rebuilds the row.

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
      hint = el("p", { class: "hint" }, [el("a", { href: "/settings", text: "Add an API key for " + p.label + " in Settings" }),
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

  function currentRequest() {
    return { task: $("task").value, selections: selections(), attachment_ids: attachmentIds() };
  }

  function needsConsent(req) {
    return req.selections.some((s) => s.backend === "cli");
  }

  async function loadProviders(refresh) {
    try {
      const data = await api("/api/providers" + (refresh ? "?refresh=true" : ""));
      state.providers = data.providers;
      state.defaultMaxTokens = data.default_max_output_tokens;
      state.budget = data.budget;
      state.consentText = data.consent_text;
      $("consent-text").textContent = data.consent_text;
      $("syn-consent-text").textContent = data.consent_text;
      renderStrip();
      renderProviders();
    } catch (error) {
      if (!state.providers.length) {
        $("providers").replaceChildren(el("p", { class: "help rack-empty", text: "Could not load the models." }));
      }
      showMessage(error.message);
    }
    updateButtons();
  }

  // ------------------------------------------------------------------ attachments
  //
  // Files are uploaded once (POST /api/attachments, multipart) and then referred to
  // by id (their sha256) in estimate, run and synthesis requests. The server checks
  // the type by content; the accept list only filters the file picker.

  const ATTACH_MAX_TOTAL = 20 * 1024 * 1024;
  const UPLOAD_HEADER = "X-Crossjury-Upload";

  function fileSize(bytes) {
    if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + " MB";
    if (bytes >= 1024) return Math.round(bytes / 1024) + " KB";
    return bytes + " B";
  }

  const KIND_TEXT = { pdf: "PDF", document: "Word", image: "Image" };

  function attachmentDetails(a) {
    const parts = [KIND_TEXT[a.kind] || a.kind, fileSize(a.size)];
    if (a.pages !== null && a.pages !== undefined) parts.push(plural(a.pages, "page"));
    if (a.image_width && a.image_height) parts.push(a.image_width + "\u00d7" + a.image_height + " px");
    return parts.join(", ");
  }

  function attachmentIds() {
    return state.attachments.map((a) => a.id);
  }

  function renderAttachments() {
    const list = $("attach-list");
    list.replaceChildren(...state.attachments.map((a) => el("li", { class: "attach-item" }, [
      el("div", { class: "attach-main" }, [
        el("span", { class: "attach-name", text: a.filename }),
        el("span", { class: "attach-meta", text: attachmentDetails(a) }),
        iconButton("x", "Remove " + a.filename, () => removeAttachment(a.id),
          { class: "icon-btn attach-remove", disabled: state.busy }),
      ]),
      ...a.warnings.map((w) => el("div", { class: "attach-warn", text: w })),
    ])));
    if (state.uploading) {
      list.append(el("li", { class: "attach-item" }, el("span", { class: "attach-uploading" }, [el("span", { class: "spinner" }), "Uploading\u2026"])));
    }
    list.hidden = list.childElementCount === 0;
    renderPrivacy();
  }

  // ADR 0007: before a run, list every vendor that will receive the files.
  function renderPrivacy() {
    const box = $("attach-privacy");
    if (!state.attachments.length) { box.hidden = true; box.textContent = ""; return; }
    const vendors = Array.from(new Set(selections().map((s) => providerLabel(s.provider))));
    box.textContent = vendors.length
      ? "These files will be sent to: " + vendors.join(", ") + "."
      : "Select models to see which vendors will receive these files.";
    box.hidden = false;
  }

  async function uploadFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length || state.busy) return;
    showMessage("");
    const known = state.attachments.reduce((n, a) => n + a.size, 0);
    const added = files.reduce((n, f) => n + f.size, 0);
    if (known + added > ATTACH_MAX_TOTAL) {
      showMessage("Attachments are limited to 20 MB in total per run. Remove a file or attach a smaller one.");
      return;
    }
    const form = new FormData();
    for (const f of files) form.append("files", f, f.name);
    state.uploading += 1;
    renderAttachments();
    updateButtons();
    try {
      const response = await fetch("/api/attachments", { method: "POST", headers: { [UPLOAD_HEADER]: "1" }, body: form });
      let data = null;
      try { data = await response.json(); } catch (_) { data = null; }
      if (!response.ok) throw new Error(describeError(response.status, data));
      for (const a of data.attachments) {
        if (!state.attachments.some((x) => x.id === a.id)) state.attachments.push(a);
      }
    } catch (error) {
      showMessage(error.message);
    } finally {
      state.uploading -= 1;
      renderAttachments();
      invalidateEstimate();
    }
  }

  async function removeAttachment(id) {
    state.attachments = state.attachments.filter((a) => a.id !== id);
    renderAttachments();
    invalidateEstimate();
    $("attach-btn").focus();
    // Keep files that the step-2 panel still needs; the server deletes them after 24 h.
    if (syn.material && syn.material.attachment_ids.includes(id)) return;
    try {
      await fetch("/api/attachments/" + encodeURIComponent(id), { method: "DELETE" });
    } catch (_) { /* expires on its own */ }
  }

  function initAttachments() {
    const input = $("attach-input");
    $("attach-btn").addEventListener("click", () => input.click());
    input.addEventListener("change", () => { uploadFiles(input.files); input.value = ""; });
    const panel = $("task-panel");
    const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
    panel.addEventListener("dragover", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
      panel.classList.add("drop-target");
    });
    panel.addEventListener("dragleave", (e) => {
      if (!panel.contains(e.relatedTarget)) panel.classList.remove("drop-target");
    });
    panel.addEventListener("drop", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      panel.classList.remove("drop-target");
      uploadFiles(e.dataTransfer.files);
    });
    // Paste files (e.g. a screenshot) straight into the task as attachments, like the
    // vendors' chat apps. Plain text still pastes into the textarea as usual.
    panel.addEventListener("paste", (e) => {
      const files = pastedFiles(e.clipboardData);
      if (!files.length) return;
      const types = Array.from((e.clipboardData && e.clipboardData.types) || []);
      if (!types.includes("text/plain")) e.preventDefault();
      uploadFiles(files);
    });
  }

  let pasteCounter = 0;

  function pastedFiles(clipboard) {
    if (!clipboard) return [];
    const out = [];
    for (const item of Array.from(clipboard.items || [])) {
      if (item.kind !== "file") continue;
      const blob = item.getAsFile();
      if (!blob) continue;
      out.push(renamePasted(blob));
    }
    return out;
  }

  function renamePasted(blob) {
    // Clipboard images usually arrive as "image.png"; give each paste a distinct name.
    const generic = !blob.name || /^image\.(png|jpe?g|gif|webp)$/i.test(blob.name);
    if (!generic) return blob;
    const ext = (blob.type.split("/")[1] || "png").replace("jpeg", "jpg");
    const stamp = new Date().toISOString().replace(/[-:]/g, "").replace("T", "-").slice(0, 15);
    pasteCounter += 1;
    return new File([blob], "pasted-" + stamp + "-" + pasteCounter + "." + ext, { type: blob.type });
  }

  // Delivery tags: how a member receives (or would receive) each file.
  const MODE_TEXT = { native: "Native", text: "As text", refused: "Not sent" };
  const MODE_CLASS = { native: "tag-ok", text: "tag-info", refused: "tag-err" };

  function deliveryList(items) {
    if (!items || !items.length) return null;
    return el("ul", { class: "attach-modes", "aria-label": "Attachments" }, items.map((r) => el("li", {}, [
      el("span", { class: "tag " + (MODE_CLASS[r.mode] || ""), text: MODE_TEXT[r.mode] || r.mode }),
      el("span", { class: "attach-name", text: r.filename }),
      ...r.warnings.map((w) => el("div", { class: "attach-warn", text: w })),
    ])));
  }

  // ------------------------------------------------------------------ estimate

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
    invalidateEstimate();
  }

  function invalidateEstimate() {
    if (state.estimate) state.staleEstimate = true;
    state.estimate = null;
    state.estimateKey = null;
    $("estimate").hidden = true;
    renderPrivacy();
    renderStrip();
    updateButtons();
  }

  function updateButtons() {
    const req = currentRequest();
    const ready = req.task.trim() !== "" && req.selections.length > 0;
    const consentNeeded = needsConsent(req);
    $("consent-box").hidden = !consentNeeded;
    const consented = !consentNeeded || $("consent").checked;
    const uploading = state.uploading > 0;
    $("estimate-btn").disabled = state.busy || uploading || !ready;
    const estimateValid = state.estimate && state.estimateKey === JSON.stringify(req);
    const runBtn = $("run-btn");
    runBtn.disabled = state.busy || uploading || !estimateValid || state.estimate.budget.refused || !consented;
    const n = req.selections.length;
    if (!state.busy) runBtn.textContent = n > 1 ? "Ask " + n + " models" : (n === 1 ? "Ask 1 model" : "Ask models");
    $("attach-btn").disabled = state.busy;
    const hint = $("action-hint");
    if (state.busy) hint.textContent = "Waiting for the answers\u2026";
    else if (uploading) hint.textContent = "Uploading files\u2026";
    else if (req.task.trim() === "" && n === 0) hint.textContent = "Write a question and pick at least one model.";
    else if (req.task.trim() === "") hint.textContent = "Write a question first.";
    else if (n === 0) hint.textContent = "Pick at least one model.";
    else if (!estimateValid) hint.textContent = "Estimate the cost first" + (keyboardHints() ? " (Ctrl+Enter)." : ".");
    else if (state.estimate.budget.refused) hint.textContent = "The budget refuses this run. See the estimate for why.";
    else if (!consented) hint.textContent = "Tick the subscription consent box under Models.";
    else hint.textContent = keyboardHints() ? "Ready. Ctrl+Enter asks the models." : "Ready.";
  }

  // Shortcut hints only where a keyboard is likely (not on small touch screens).
  function keyboardHints() {
    return !(window.matchMedia && window.matchMedia("(pointer: coarse), (max-width: 719px)").matches);
  }

  function noteLine(kind, text) {
    return el("p", { class: "note-line note-" + kind }, [icon(kind === "err" ? "alert" : "info"), el("span", { text: text })]);
  }

  function renderEstimate(est) {
    const b = est.budget;
    const box = $("estimate");
    const range = moneyRange(est.currency, est.total_low, est.total_high);
    const rows = est.lines.map((line) => el("tr", {}, [
      el("td", { text: providerLabel(line.provider) }),
      el("td", {}, [line.model, line.backend === "cli" ? el("span", { class: "help", text: " subscription" }) : null]),
      el("td", { class: "num", text: line.backend === "cli"
        ? planUsage(line.calls)
        : moneyRange(est.currency, line.cost_low, line.cost_high) }),
    ]));
    box.replaceChildren(
      el("div", { class: "estimate-head" }, [
        el("span", { class: "estimate-title", text: "Cost estimate" }),
        el("span", { class: "estimate-total", text: costText(range, hasPriced(est.lines), est.cli_calls) }),
      ]),
      el("div", { class: "table-scroll" }, el("table", { class: "data-table" }, [
        el("thead", {}, el("tr", {}, [el("th", { text: "Provider" }), el("th", { text: "Model" }), el("th", { class: "num", text: "Low\u2013high" })])),
        el("tbody", {}, rows),
      ])),
      el("p", { class: "help", text:
        "Limits: " + money(b.currency, b.run_limit) + " per run, confirm above " + money(b.currency, b.confirm_above) +
        ", " + money(b.currency, b.daily_remaining) + " left today." }),
    );
    if (est.cli_calls) {
      box.append(el("p", { class: "help", text:
        "Subscription calls have unknown cost and count against your plan's usage limits; budget limits apply to priced calls only." }));
    }
    const attachLines = est.attachments || [];
    if (attachLines.length) {
      box.append(el("div", { class: "table-scroll" }, el("table", { class: "data-table" }, [
        el("thead", {}, el("tr", {}, [el("th", { text: "Attachment" }), el("th", { text: "Model" }), el("th", { class: "num", text: "Input tokens" })])),
        el("tbody", {}, attachLines.map((a) => el("tr", {}, [
          el("td", { text: a.filename }),
          el("td", { text: providerLabel(a.provider) + (a.backend === "cli" ? " (subscription)" : "") }),
          el("td", { class: "num", text: a.mode === "refused"
            ? "not sent: this model cannot read it"
            : (a.mode === "native" ? "native" : "as text") + ", \u2248 " + tokens(a.tokens) +
              (a.vendor_counted ? " (counted by the vendor)" : "") }),
        ]))),
      ])));
    }
    for (const w of est.warnings) box.append(noteLine("warn", w));
    if (b.refused) {
      box.append(noteLine("err", "Refused: " + (b.refusal_reason || "over budget") + ". Pick fewer or cheaper models, or lower Max output tokens."));
    } else if (b.needs_confirmation) {
      box.append(noteLine("warn", "The high estimate is above your confirmation threshold; you will be asked to confirm."));
    }
    box.hidden = false;
  }

  async function onEstimate() {
    showMessage("");
    const req = currentRequest();
    state.busy = true;
    const btn = $("estimate-btn");
    btn.replaceChildren(el("span", { class: "spinner" }), "Estimating\u2026");
    updateButtons();
    try {
      const est = await api("/api/estimate", req);
      state.estimate = est;
      state.estimateKey = JSON.stringify(req);
      state.staleEstimate = false;
      state.budget = Object.assign({}, state.budget || {}, {
        currency: est.budget.currency,
        run_limit: est.budget.run_limit,
        confirm_above: est.budget.confirm_above,
        daily_limit: est.budget.daily_limit,
        spent_today: est.budget.spent_today,
        daily_remaining: est.budget.daily_remaining,
      });
      renderEstimate(est);
    } catch (error) {
      invalidateEstimate();
      showMessage(error.message);
    } finally {
      state.busy = false;
      btn.textContent = "Estimate cost";
      renderStrip();
      updateButtons();
    }
  }

  // ------------------------------------------------------------------ run

  function providerLabel(name) {
    const p = state.providers.find((x) => x.name === name);
    return p ? p.label : name;
  }

  function answerHead(provider, model, backend, meta, tools) {
    return el("header", { class: "answer-head" }, [
      el("div", { class: "answer-title" }, [
        el("h3", { class: "answer-provider", text: providerLabel(provider) }),
        el("span", { class: "answer-model" }, [model, backend === "cli" ? el("span", { class: "tag", text: "Subscription" }) : null]),
      ]),
      tools || null,
      meta && meta.length ? el("dl", { class: "meta" }, meta.map((m) => el("div", {}, [el("dt", { text: m[0] }), el("dd", { text: m[1] })]))) : null,
    ]);
  }

  let pendingTimer = null;

  function renderPending(req) {
    const started = Date.now();
    $("results").replaceChildren(...req.selections.map((s) =>
      el("article", { class: "answer", "aria-busy": "true" }, [
        answerHead(s.provider, s.model, s.backend, null, null),
        el("div", { class: "answer-pending" }, [
          el("div", { class: "pending-line" }, [el("span", { class: "spinner" }), el("span", { class: "elapsed", text: "Waiting for the answer" })]),
          el("div", { class: "skeleton" }), el("div", { class: "skeleton" }), el("div", { class: "skeleton" }), el("div", { class: "skeleton" }),
        ]),
      ])));
    clearInterval(pendingTimer);
    pendingTimer = setInterval(() => {
      const secs = Math.round((Date.now() - started) / 1000);
      for (const node of document.querySelectorAll("#results .elapsed")) node.textContent = "Waiting for the answer, " + secs + " s";
    }, 1000);
    $("run-total").textContent = "";
    $("answers-empty").hidden = true;
    $("answers-tools").hidden = true;
    $("results-section").hidden = false;
  }

  function memberMeta(m) {
    const meta = [];
    const cli = m.backend === "cli";
    if (m.error === null) meta.push(["Cost", cli && m.cost === null ? planUsage(1) : money(m.currency, m.cost)]);
    if (m.latency_s !== null && m.latency_s !== undefined) meta.push(["Time", Number(m.latency_s).toFixed(1) + " s"]);
    if (m.usage) {
      const known = (v) => v !== null && v !== undefined;
      const parts = [];
      if (!cli || known(m.usage.input_tokens)) parts.push(tokens(m.usage.input_tokens) + " in");
      if (!cli || known(m.usage.output_tokens)) parts.push(tokens(m.usage.output_tokens) + " out");
      if (m.usage.reasoning_tokens) parts.push(tokens(m.usage.reasoning_tokens) + " reasoning");
      if (m.usage.cached_input_tokens) parts.push(tokens(m.usage.cached_input_tokens) + " cached");
      if (parts.length) meta.push(["Tokens", parts.join(", ")]);
    }
    return meta;
  }

  function answerTools(article, m) {
    const label = providerLabel(m.provider);
    const wide = iconButton("expand", "Show " + label + " full width", null, { "aria-pressed": "false" });
    const collapse = iconButton("chevron", "Collapse " + label, null, { "aria-expanded": "true" });
    wide.addEventListener("click", () => {
      const on = !article.classList.contains("is-wide");
      for (const other of document.querySelectorAll("#results .answer.is-wide")) {
        if (other !== article) other.querySelector("[aria-pressed]").click();
      }
      article.classList.toggle("is-wide", on);
      wide.setAttribute("aria-pressed", String(on));
      wide.replaceChildren(icon(on ? "shrink" : "expand"));
      const text = (on ? "Back to columns: " : "Show full width: ") + label;
      wide.setAttribute("aria-label", text);
      wide.title = text;
      if (on) article.scrollIntoView({ block: "nearest", behavior: reducedMotion() ? "auto" : "smooth" });
    });
    collapse.addEventListener("click", () => {
      const collapsed = !article.classList.contains("is-collapsed");
      article.classList.toggle("is-collapsed", collapsed);
      collapse.setAttribute("aria-expanded", String(!collapsed));
      const text = (collapsed ? "Expand " : "Collapse ") + label;
      collapse.setAttribute("aria-label", text);
      collapse.title = text;
      collapse.style.transform = collapsed ? "rotate(-90deg)" : "";
    });
    const tools = [];
    if (m.error === null) tools.push(iconButton("copy", "Copy " + label + " answer", () => copyText(m.text, label + " answer")));
    tools.push(wide, collapse);
    return el("div", { class: "answer-tools" }, tools);
  }

  function renderResults(result) {
    clearInterval(pendingTimer);
    $("results").replaceChildren(...result.members.map((m) => {
      const article = el("article", { class: "answer" + (m.error !== null ? " has-error" : ""), "data-provider": m.provider });
      const body = m.error !== null
        ? el("div", { class: "answer-error" }, [
          el("div", { class: "answer-error-title" }, [icon("alert"), "No answer"]),
          el("div", { text: m.error }),
          fixFor(m.error),
        ])
        : el("div", { class: "answer-body", tabindex: "0", role: "region", "aria-label": providerLabel(m.provider) + " answer" },
          renderMarkdown(m.text));
      const delivery = deliveryList(m.attachments);
      article.append(answerHead(m.provider, m.model, m.backend, memberMeta(m), answerTools(article, m)));
      if (delivery) article.append(delivery);
      article.append(body);
      return article;
    }));
    const planCalls = result.members.filter((m) => m.backend === "cli" && m.error === null && m.cost === null).length;
    const otherUnknown = result.unknown_cost_members - planCalls;
    let total = "Total " + money(result.currency, result.total_cost);
    if (planCalls) total += " + " + planUsage(planCalls);
    if (otherUnknown > 0) total += " (+ " + otherUnknown + " with unknown cost)";
    const ok = result.members.filter((m) => m.error === null).length;
    $("run-total").textContent = plural(ok, "answer") + " of " + result.members.length + ". " + total + ".";
    $("answers-empty").hidden = true;
    $("answers-tools").hidden = false;
    $("results-section").hidden = false;
    wireSyncScroll();
  }

  // Synchronized scrolling: scrolling one answer moves the others to the same
  // relative position. Off by default.
  let syncing = false;
  function wireSyncScroll() {
    for (const body of document.querySelectorAll("#results .answer-body")) {
      body.addEventListener("scroll", () => {
        if (!state.syncScroll || syncing) return;
        syncing = true;
        const range = body.scrollHeight - body.clientHeight;
        const ratio = range > 0 ? body.scrollTop / range : 0;
        for (const other of document.querySelectorAll("#results .answer-body")) {
          if (other !== body) other.scrollTop = ratio * (other.scrollHeight - other.clientHeight);
        }
        requestAnimationFrame(() => { syncing = false; });
      }, { passive: true });
    }
  }

  async function onRun() {
    const req = currentRequest();
    if (!state.estimate || state.estimateKey !== JSON.stringify(req)) return;
    const b = state.estimate.budget;
    let confirmed = false;
    if (b.needs_confirmation) {
      confirmed = window.confirm(
        "The high estimate (" + money(b.currency, b.estimate_high) + ") is above your confirmation threshold (" +
        money(b.currency, b.confirm_above) + "). Run anyway?");
      if (!confirmed) return;
    }
    const consent = needsConsent(req) && $("consent").checked;
    if (needsConsent(req) && !consent) return;
    showMessage("");
    state.busy = true;
    const runBtn = $("run-btn");
    runBtn.replaceChildren(el("span", { class: "spinner" }), "Asking\u2026");
    renderAttachments();
    updateButtons();
    resetSynthesis();
    renderPending(req);
    try {
      const body = Object.assign({}, req, { confirmed: confirmed, allow_unknown_cost: consent });
      const result = await api("/api/run", body);
      state.run = {
        task: req.task,
        attachments: state.attachments.filter((a) => req.attachment_ids.includes(a.id)).map((a) => a.filename),
        result: result,
      };
      renderResults(result);
      const plan = result.members.filter((m) => m.backend === "cli" && m.error === null && m.cost === null).length;
      state.planCalls += result.members.filter((m) => m.backend === "cli").length;
      setLastCost("Step 1 answers" + (plan ? " + " + plural(plan, "plan call") : ""), money(result.currency, result.total_cost));
      setupSynthesis(req.task, result, req.attachment_ids);
    } catch (error) {
      clearInterval(pendingTimer);
      if (state.run) {
        renderResults(state.run.result);  // keep the previous answers on screen
      } else {
        $("results-section").hidden = true;
        $("answers-empty").hidden = false;
      }
      showMessage(error.message);
    } finally {
      state.busy = false;
      renderAttachments();
      $("consent").checked = false;  // consent is given per run
      invalidateEstimate();  // spend changed: a new run needs a fresh estimate
      state.staleEstimate = false;
      renderStrip();
      refreshBudget();
    }
  }

  // ------------------------------------------------------------------ export

  function exportMarkdown() {
    if (!state.run) return;
    const r = state.run.result;
    const out = ["# llm-crossjury run", "", "_Exported " + new Date().toLocaleString() + "_", "", "## Question", "", state.run.task.trim(), ""];
    if (state.run.attachments.length) {
      out.push("Attachments: " + state.run.attachments.join(", "), "");
    }
    out.push("## Answers", "");
    for (const m of r.members) {
      out.push("### " + providerLabel(m.provider) + " (" + m.model + (m.backend === "cli" ? ", subscription" : "") + ")", "");
      const meta = memberMeta(m).map((x) => x[0] + ": " + x[1]).join("; ");
      if (meta) out.push("_" + meta + "_", "");
      out.push(m.error !== null ? "No answer: " + m.error : m.text.trim(), "");
    }
    if (syn.outcome) {
      const s = syn.outcome.synthesis;
      const a = syn.outcome.additional;
      out.push("## Final answer", "");
      out.push("_Chair: " + modelText(syn.runSettings.chair, syn.runSettings.chair_backend) +
        "; agreement " + agreementText() + "; " + (syn.reviewSkipped ? "review skipped" : "reviewed") + "_", "");
      out.push(String(s.final_text || "").trim(), "");
      out.push("## Additional list", "", "### Disputed", "");
      if (!a.disputed.length) out.push("None.", "");
      for (const d of a.disputed) {
        out.push("- **" + d.question + "**");
        for (const p of d.positions) out.push("  - " + labelText(p.label) + ": " + p.stance + ". " + p.argument);
        if (d.chair_assessment) out.push("  - Chair: " + d.chair_assessment);
      }
      out.push("", "### Unique insights not included", "");
      if (!a.unique.length) out.push("None.");
      for (const u of a.unique) out.push("- " + labelText(u.label) + ": " + u.insight);
      out.push("", "### Possibly wrong", "");
      if (!a.possibly_wrong.length) out.push("None.");
      for (const w of a.possibly_wrong) out.push("- " + labelText(w.target) + " (" + w.severity + "): \"" + w.quote + "\". " + w.problem);
      out.push("");
    }
    const blob = new Blob([out.join("\n")], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const stamp = new Date().toISOString().replace(/[-:]/g, "").replace("T", "-").slice(0, 13);
    const link = el("a", { href: url, download: "crossjury-" + stamp + ".md", class: "visually-hidden" });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("Markdown file downloaded");
  }

  // ------------------------------------------------------------------ synthesis (step 2)
  //
  // Stateless: every request posts the step-1 material, the label seed and the
  // settings back to the server, plus earlier stage outputs where needed.

  const STAGE_TITLES = { agreement: "Agreement", review: "Review", chair: "Chair" };

  const syn = {
    material: null,      // { task, answers: [{provider, backend, model, text}], attachment_ids }
    runSettings: null,   // settings snapshot of the current synthesis
    seed: null,
    chair: null,         // { provider, model_id }
    chairBackend: null,  // "api" | "cli"
    chairOptions: [],
    estimate: null,
    estimateSeq: 0,
    estimateTimer: null,
    busy: false,
    runId: null,
    labels: [],
    agreement: null,
    reviews: null,
    reviewSkipped: false,
    outcome: null,       // ChairOutcome
    costs: [],           // [{ stage, calls, cost, unknown_cost_calls }]
    currency: "USD",
    reveal: false,
    choices: { dispute: {}, unique: {} },  // rulings picked in the additional list
    resynthEstimate: null,
    resynthSeq: 0,
    resynthTimer: null,
  };

  function modelText(choice, backend) {
    return providerLabel(choice.provider) + ", " + choice.model_id + (backend === "cli" ? " (subscription)" : "");
  }

  // Cost text of a CostEstimate: priced range and/or subscription call count.
  function estimateText(currency, est) {
    return costText(moneyRange(currency, est.total_low, est.total_high), hasPriced(est.lines), cliCalls(est.lines));
  }

  function synConsented() {
    return $("syn-consent").checked;
  }

  function labelText(label) {
    if (!syn.reveal) return label;
    const info = syn.labels.find((x) => x.label === label);
    return info ? label + " (" + providerLabel(info.provider) + ", " + info.model + ")" : label;
  }

  function agreementText() {
    return syn.agreement && syn.agreement.ratio !== null && syn.agreement.ratio !== undefined
      ? Math.round(syn.agreement.ratio * 100) + "%" : "n/a";
  }

  function synMessage(text) {
    fillMessage($("syn-message"), text);
  }

  function synStatus(text, spinning) {
    const box = $("syn-status");
    box.replaceChildren();
    if (spinning) box.append(el("span", { class: "spinner" }));
    if (text) box.append(text);
  }

  // Progress through agreement, review and chair. states: stage -> "done" | "active" | "skipped" | "".
  function renderProgress(states) {
    const box = $("syn-progress");
    if (!states) { box.hidden = true; box.replaceChildren(); return; }
    box.replaceChildren(...["agreement", "review", "chair"].map((stage, i) => {
      const s = states[stage] || "";
      const mark = s === "done" ? icon("check") : (s === "active" ? el("span", { class: "spinner" }) : String(i + 1));
      const suffix = s === "skipped" ? " (skipped)" : (s === "active" ? " (running)" : "");
      return el("li", { class: s ? "is-" + s : null, "aria-current": s === "active" ? "step" : null }, [
        el("span", { class: "step-mark", "aria-hidden": "true" }, mark),
        el("span", { class: "step-label", text: STAGE_TITLES[stage] }),
        suffix ? el("span", { class: "visually-hidden", text: suffix }) : null,
      ]);
    }));
    box.hidden = false;
  }

  function synSettings() {
    const tier = document.querySelector("input[name=syn-tier]:checked");
    const extra = $("syn-instructions").value;
    return {
      task: syn.material.task,
      answers: syn.material.answers,
      seed: syn.seed,
      chair: syn.chair,
      chair_backend: syn.chair ? syn.chairBackend : null,
      review_tier: tier ? tier.value : "economy",
      extra_instructions: extra.trim() === "" ? null : extra,
      allow_unknown_cost: synConsented(),
      attachment_ids: syn.material.attachment_ids,
    };
  }

  function setSynBusy(busy) {
    syn.busy = busy;
    for (const id of ["syn-instructions", "syn-chair", "syn-consent"]) $(id).disabled = busy;
    for (const radio of document.querySelectorAll("input[name=syn-tier]")) radio.disabled = busy;
    for (const node of document.querySelectorAll("#syn-result input, #syn-result button")) {
      if (node.id !== "syn-reveal" && !node.classList.contains("icon-btn")) node.disabled = busy;
    }
    updateSynButton();
    updateResynthButton();
  }

  function resetSynthesis() {
    syn.material = null;
    syn.estimate = null;
    syn.outcome = null;
    clearTimeout(syn.estimateTimer);
    clearTimeout(syn.resynthTimer);
    syn.estimateSeq += 1;
    syn.resynthSeq += 1;
    $("synthesis-section").hidden = true;
    $("syn-decision").hidden = true;
    $("syn-result").hidden = true;
    $("syn-breakdown").hidden = true;
    $("syn-consent-box").hidden = true;
    $("syn-consent").checked = false;
    renderProgress(null);
    synMessage("");
    synStatus("");
  }

  function setupSynthesis(task, result, attachmentIdsUsed) {
    resetSynthesis();
    const ids = attachmentIdsUsed || [];
    const answers = result.members
      .filter((m) => m.error === null && typeof m.text === "string" && m.text.trim() !== "")
      .map((m) => ({ provider: m.provider, backend: m.backend, model: m.model, text: m.text }));
    $("synthesis-section").hidden = false;
    const enough = answers.length >= 2;
    $("syn-unavailable").hidden = enough;
    $("syn-form").hidden = !enough;
    if (!enough) {
      $("syn-unavailable").textContent = answers.length === 1
        ? "Synthesis needs at least two successful answers. Only one model answered: pick another model and ask again."
        : "Synthesis needs at least two successful answers. Fix the errors above and ask again.";
      return;
    }
    syn.material = { task: task, answers: answers, attachment_ids: ids };
    const note = $("syn-attach-note");
    note.textContent = ids.length
      ? "Only the chair model receives the " + plural(ids.length, "attached file") +
        "; the agreement judge and the reviewers see the answers only."
      : "";
    note.hidden = !ids.length;
    syn.seed = null;
    syn.chair = null;
    syn.chairBackend = null;
    syn.reveal = false;
    buildChairOptions(answers);
    scheduleSynEstimate(0);
  }

  function buildChairOptions(answers) {
    // A provider's calls all use the backend of its answer, so a chair of an
    // answering provider is offered on that backend only.
    const options = [];
    const seen = new Set();
    const answered = {};
    const add = (provider, modelId, backend, group) => {
      const key = provider + "/" + modelId + "/" + backend;
      if (seen.has(key)) return;
      seen.add(key);
      options.push({ provider: provider, model_id: modelId, backend: backend, group: group });
    };
    for (const a of answers) {
      answered[a.provider] = a.backend;
      add(a.provider, a.model, a.backend, "Models that answered");
    }
    for (const p of state.providers) {
      const only = answered[p.name];
      if (p.key_status !== "not set" && (!only || only === "api")) {
        for (const m of p.models) add(p.name, m.id, "api", "Other models");
      }
      if (p.cli && p.cli.available && (!only || only === "cli")) {
        for (const m of p.cli.models) add(p.name, m.id, "cli", "Other models");
      }
    }
    syn.chairOptions = options;
    const select = $("syn-chair");
    select.replaceChildren();
    const groups = {};
    for (const [index, o] of options.entries()) {
      if (!groups[o.group]) {
        groups[o.group] = el("optgroup", { label: o.group });
        select.append(groups[o.group]);
      }
      groups[o.group].append(el("option", { value: String(index), text: modelText(o, o.backend) }));
    }
  }

  function selectChair(choice, backend) {
    const index = syn.chairOptions.findIndex((o) =>
      o.provider === choice.provider && o.model_id === choice.model_id && o.backend === backend);
    if (index >= 0) $("syn-chair").value = String(index);
  }

  function onChairChange() {
    const o = syn.chairOptions[Number($("syn-chair").value)];
    syn.chair = o ? { provider: o.provider, model_id: o.model_id } : null;
    syn.chairBackend = o ? o.backend : null;
    scheduleSynEstimate(0);
  }

  function scheduleSynEstimate(delay) {
    if (!syn.material) return;
    clearTimeout(syn.estimateTimer);
    syn.estimate = null;
    updateSynButton();
    syn.estimateTimer = setTimeout(runSynEstimate, delay);
  }

  async function runSynEstimate() {
    const seq = ++syn.estimateSeq;
    if (!syn.busy) synStatus("Estimating\u2026", true);
    try {
      const est = await api("/api/synthesis/estimate", synSettings());
      if (seq !== syn.estimateSeq) return;
      syn.estimate = est;
      syn.seed = est.seed;
      syn.currency = est.currency;
      if (!syn.chair) {
        syn.chair = est.chair;
        syn.chairBackend = est.chair_backend;
        selectChair(est.chair, est.chair_backend);
      }
      $("syn-consent-box").hidden = !est.cli_calls;
      renderSynBreakdown(est);
      if (!syn.busy) {
        synStatus(est.cli_calls && !synConsented() ? "Tick the consent box to use subscription models." : "");
        synMessage(est.budget.refused ? "Refused by the budget: " + (est.budget.refusal_reason || "over budget") : "");
      }
    } catch (error) {
      if (seq !== syn.estimateSeq) return;
      syn.estimate = null;
      if (!syn.busy) { synStatus(""); synMessage(error.message); }
    }
    updateSynButton();
  }

  function stageDescription(stage, est) {
    if (stage === "agreement") return "Judge: " + modelText(est.judge, est.chair_backend);
    if (stage === "chair") return "Chair: " + modelText(est.chair, est.chair_backend);
    const models = est.reviewers.map((r) => r.model.model_id + (r.backend === "cli" ? " (subscription)" : ""));
    return plural(est.reviewers.length, "reviewer") + ": " + Array.from(new Set(models)).join(", ");
  }

  function synTotalText(est) {
    const priced = est.stages.some((s) => hasPriced(s.estimate.lines));
    return costText(moneyRange(est.currency, est.total_low, est.total_high), priced, est.cli_calls);
  }

  function renderSynBreakdown(est) {
    const body = $("syn-breakdown-body");
    body.replaceChildren(
      el("div", { class: "table-scroll" }, el("table", { class: "data-table" }, [
        el("thead", {}, el("tr", {}, [el("th", { text: "Stage" }), el("th", { text: "Models" }), el("th", { class: "num", text: "Estimate" })])),
        el("tbody", {}, est.stages.map((s) => el("tr", {}, [
          el("td", { text: STAGE_TITLES[s.stage] || s.stage }),
          el("td", { text: stageDescription(s.stage, est) }),
          el("td", { class: "num", text: estimateText(est.currency, s.estimate) }),
        ]))),
        el("tfoot", {}, el("tr", {}, [
          el("td", { text: "Total" }), el("td", {}),
          el("td", { class: "num", text: synTotalText(est) }),
        ])),
      ])),
      el("p", { class: "help", text: "Review can be skipped after the agreement check. Estimates assume no prompt-cache discount." +
        (est.scrubbed ? " Model and vendor names are removed from the answers before review." : "") }),
    );
    $("syn-breakdown").hidden = false;
  }

  function updateSynButton() {
    const btn = $("syn-btn");
    const est = syn.estimate;
    if (!syn.busy) btn.textContent = "AI synthesize" + (est ? " (\u2248 " + synTotalText(est) + ")" : "");
    const consented = !est || !est.cli_calls || synConsented();
    btn.disabled = syn.busy || !syn.material || !est || est.budget.refused || !consented;
  }

  function addCosts(result) {
    syn.currency = result.currency;
    for (const c of result.stage_costs) syn.costs.push(c);
  }

  function synCostTotals() {
    const total = syn.costs.reduce((sum, c) => sum + Number(c.cost), 0);
    const unknown = syn.costs.reduce((sum, c) => sum + c.unknown_cost_calls, 0);
    return { total: total, unknown: unknown };
  }

  function recordSynCost(label, result) {
    const cost = result.stage_costs.reduce((sum, c) => sum + Number(c.cost), 0);
    const unknown = result.stage_costs.reduce((sum, c) => sum + c.unknown_cost_calls, 0);
    state.planCalls += unknown;
    setLastCost(label + (unknown ? " + " + plural(unknown, "plan call") : ""), money(result.currency, cost));
  }

  async function onSynthesize() {
    const est = syn.estimate;
    if (!est || syn.busy) return;
    let confirmed = false;
    if (est.budget.needs_confirmation) {
      confirmed = window.confirm(
        "The high estimate (" + money(est.currency, est.total_high) + ") is above your confirmation threshold (" +
        money(est.currency, est.budget.confirm_above) + "). Continue?");
      if (!confirmed) return;
    }
    synMessage("");
    syn.runSettings = synSettings();
    syn.costs = [];
    syn.outcome = null;
    syn.agreement = null;
    syn.reviews = null;
    $("syn-result").hidden = true;
    $("syn-decision").hidden = true;
    setSynBusy(true);
    $("syn-btn").replaceChildren(el("span", { class: "spinner" }), "Synthesizing\u2026");
    renderProgress({ agreement: "active" });
    synStatus("Measuring agreement\u2026");
    try {
      const body = Object.assign({}, syn.runSettings, { confirmed: confirmed });
      const step = await api("/api/synthesis/agreement", body);
      syn.runId = step.run_id;
      syn.seed = step.seed;
      syn.labels = step.labels;
      syn.agreement = step.agreement;
      addCosts(step);
      recordSynCost("Agreement", step);
      if (step.suggest_skip) {
        synStatus("");
        renderProgress({ agreement: "done" });
        showSkipDecision(step);
        return;  // stays busy until the user decides
      }
      await finish(false);
    } catch (error) {
      synFailed(error);
    }
  }

  function synFailed(error) {
    synStatus("");
    renderProgress(null);
    synMessage(error.message);
    setSynBusy(false);
    refreshBudget();
  }

  function showSkipDecision(step) {
    const ratio = Math.round(step.agreement.ratio * 100);
    const review = step.review_estimate;
    const saving = costText(money(review.currency || syn.currency, review.total_high),
      hasPriced(review.lines), cliCalls(review.lines));
    const skip = el("button", { type: "button", class: "btn btn-primary", text: "Skip review (saves \u2248 " + saving + ")", onclick: () => decide(true) });
    const box = $("syn-decision");
    box.replaceChildren(
      el("p", { class: "decision-title", text: "The answers largely agree (agreement " + ratio + "%)." }),
      el("p", { class: "help", text: "A peer review may add little. You can skip it and let the chair write the final answer now." }),
      el("div", { class: "actions" }, [
        skip,
        el("button", { type: "button", class: "btn", text: "Continue with review", onclick: () => decide(false) }),
      ]),
    );
    box.hidden = false;
    skip.focus();
  }

  async function decide(skip) {
    $("syn-decision").hidden = true;
    try {
      await finish(skip);
    } catch (error) {
      synFailed(error);
    }
  }

  async function finish(skip) {
    renderProgress(skip ? { agreement: "done", review: "skipped", chair: "active" } : { agreement: "done", review: "active" });
    synStatus(skip ? "Writing the final answer\u2026" : "Reviewing, then writing the final answer\u2026");
    const body = Object.assign({}, syn.runSettings, { seed: syn.seed, run_id: syn.runId, agreement: syn.agreement, skip_review: skip });
    const step = await api("/api/synthesis/finish", body);
    syn.reviews = step.reviews;
    syn.reviewSkipped = step.review_skipped;
    syn.outcome = step.chair;
    syn.choices = { dispute: {}, unique: {} };
    addCosts(step);
    recordSynCost("Synthesis", step);
    renderProgress({ agreement: "done", review: step.review_skipped ? "skipped" : "done", chair: "done" });
    synStatus("");
    setSynBusy(false);
    renderSynResult();
    scheduleResynthEstimate(0);
    refreshBudget();
    const final = document.querySelector("#syn-result .final");
    if (final) final.scrollIntoView({ block: "start", behavior: reducedMotion() ? "auto" : "smooth" });
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
          lists.push({ node: el(tag), indent: indent });
        } else if (indent > top.indent + 1 && lists.length < 3 && top.node.lastElementChild) {
          const nested = el(tag);
          top.node.lastElementChild.append(nested);
          lists.push({ node: nested, indent: indent });
        } else if (top.node.tagName.toLowerCase() !== tag) {
          if (lists.length === 1) {
            flushList();
            lists.push({ node: el(tag), indent: indent });
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
        flushAll();
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

  // ---- results

  function renderSynResult() {
    const outcome = syn.outcome;
    if (!outcome) return;
    const synthesis = outcome.synthesis;
    const totals = synCostTotals();
    const totalText = money(syn.currency, totals.total) + (totals.unknown ? " + " + planUsage(totals.unknown) : "");

    const final = el("article", { class: "final", "aria-labelledby": "final-heading" }, [
      el("header", { class: "final-head" }, [
        el("div", {}, [
          el("h3", { id: "final-heading", text: "Final answer" }),
          el("dl", { class: "meta" }, [
            ["Chair", modelText(syn.runSettings.chair, syn.runSettings.chair_backend)],
            ["Review", syn.reviewSkipped ? "skipped" : "done"],
            ["Cost", totalText],
          ].map((m) => el("div", {}, [el("dt", { text: m[0] }), el("dd", { text: m[1] })]))),
        ]),
        el("div", { class: "answer-tools" }, [
          iconButton("copy", "Copy final answer", () => copyText(String(synthesis.final_text || ""), "Final answer")),
        ]),
      ]),
      el("div", { class: "final-body" }, renderMarkdown(synthesis.final_text)),
    ]);

    const reveal = el("label", { class: "reveal" }, [
      el("input", { type: "checkbox", id: "syn-reveal", checked: syn.reveal, onchange: (e) => {
        syn.reveal = e.target.checked;
        renderSynResult();
        const again = $("syn-reveal");
        if (again) again.focus();
      } }),
      "Show which model wrote which answer",
    ]);

    const side = el("aside", { class: "synth-side", "aria-label": "Synthesis details" }, [
      renderAgreement(),
      renderRanking(),
      renderCosts(totals.total, totals.unknown),
      el("div", { class: "side-block" }, [
        el("h4", { text: "Anonymous labels" }), reveal,
        syn.reveal ? el("ul", { class: "label-list" }, syn.labels.map((l) =>
          el("li", { text: l.label + ": " + providerLabel(l.provider) + ", " + l.model }))) : null,
      ]),
    ]);

    $("syn-result").replaceChildren(
      el("div", { class: "synth-grid" }, [final, side]),
      renderAdditional(outcome.additional),
    );
    $("syn-result").hidden = false;
    updateResynthButton();
  }

  function renderAgreement() {
    const ratio = syn.agreement && syn.agreement.ratio !== null && syn.agreement.ratio !== undefined ? syn.agreement.ratio : null;
    return el("div", { class: "side-block" }, [
      el("h4", { text: "Agreement" }),
      el("div", { class: "agreement-figure" }, [
        el("span", { class: "agreement-value", text: agreementText() }),
        el("span", { class: "help", text: ratio === null ? "not measured" : "of the claims are shared" }),
      ]),
      ratio === null ? null : el("div", { class: "bar", role: "img", "aria-label": "Agreement " + agreementText() },
        barFill(ratio)),
    ]);
  }

  function barFill(ratio) {
    const fill = el("span");
    fill.style.width = Math.round(ratio * 100) + "%";  // CSSOM, allowed by the CSP
    return fill;
  }

  function renderRanking() {
    const block = el("div", { class: "side-block" }, el("h4", { text: "Peer ranking" }));
    const ranking = syn.reviews ? syn.reviews.ranking : [];
    if (!ranking || ranking.length === 0) {
      block.append(el("p", { class: "help", text: syn.reviewSkipped ? "No ranking: the review was skipped." : "No ranking available." }));
      return block;
    }
    block.append(el("ol", { class: "ranking" }, ranking.map((r) => el("li", {}, [
      el("span", { text: labelText(r.label) }),
      el("span", { class: "score", text: r.score === null ? "no informative review" : Number(r.score).toFixed(2) }),
    ]))));
    return block;
  }

  // Known cost on the first line, subscription calls (if any) on a second one.
  function costCell(cost, calls, unknown) {
    if (unknown && unknown === calls) return el("td", { class: "num", text: planUsage(unknown) });
    return el("td", { class: "num" }, [
      money(syn.currency, cost),
      unknown ? el("div", { class: "help", text: "+ " + planUsage(unknown) }) : null,
    ]);
  }

  function renderCosts(total, unknown) {
    return el("div", { class: "side-block" }, [
      el("h4", { text: "Cost" }),
      el("table", { class: "data-table" }, [
        el("tbody", {}, syn.costs.map((c) => el("tr", {}, [
          el("td", { text: (STAGE_TITLES[c.stage] || c.stage) + (c.resynthesis ? " (regenerated)" : "") }),
          el("td", { class: "num help", text: plural(c.calls, "call") }),
          costCell(c.cost, c.calls, c.unknown_cost_calls),
        ]))),
        el("tfoot", {}, el("tr", {}, [el("td", { text: "Total" }), el("td", {}), costCell(total, -1, unknown)])),
      ]),
    ]);
  }

  function groupHead(iconName, title, count) {
    return el("h4", {}, [icon(iconName), title, el("span", { class: "count", text: String(count) })]);
  }

  function renderAdditional(additional) {
    const disputed = el("section", { class: "add-section", "aria-label": "Disputed points" },
      groupHead("scale", "Disputed", additional.disputed.length));
    if (additional.disputed.length === 0) disputed.append(el("p", { class: "help", text: "No disputed points." }));
    for (const d of additional.disputed) {
      const name = "ruling-" + d.id;
      const choices = el("fieldset", { class: "positions" }, [
        el("legend", { class: "visually-hidden", text: "Your ruling on: " + d.question }),
        el("label", { class: "position" }, [
          el("input", { type: "radio", name: name, value: "", checked: !syn.choices.dispute[d.id], onchange: () => onDisputeChoice(d.id, "") }),
          el("span", {}, [el("strong", { text: "Keep the chair's decision" })]),
        ]),
      ]);
      for (const p of d.positions) {
        const input = el("input", { type: "radio", name: name, value: p.label, checked: syn.choices.dispute[d.id] === p.label,
          onchange: () => onDisputeChoice(d.id, p.label) });
        choices.append(el("label", { class: "position" }, [
          input,
          el("span", {}, [
            el("span", { class: "position-stance" }, [el("strong", { text: labelText(p.label) + ": " }), p.stance]),
            el("span", { class: "position-arg", text: p.argument }),
          ]),
        ]));
      }
      disputed.append(el("div", { class: "dispute" }, [
        el("div", { class: "dispute-q", text: d.question }),
        d.chair_assessment ? el("div", { class: "chair-pick" }, [
          el("span", { class: "tag tag-warn", text: "Decided by AI" }), el("span", { text: d.chair_assessment }),
        ]) : null,
        choices,
      ]));
    }

    const unique = el("section", { class: "add-section", "aria-label": "Unique insights not included" },
      groupHead("bulb", "Unique insights not included", additional.unique.length));
    if (additional.unique.length === 0) unique.append(el("p", { class: "help", text: "None." }));
    for (const [index, u] of additional.unique.entries()) {
      const box = el("input", { type: "checkbox", checked: !!syn.choices.unique[index],
        onchange: (e) => onUniqueChoice(index, e.target.checked) });
      unique.append(el("label", { class: "insight" }, [
        box,
        el("span", {}, [el("strong", { text: labelText(u.label) + ": " }), u.insight,
          el("span", { class: "help", text: " Include in the next version." })]),
      ]));
    }

    const wrong = el("section", { class: "add-section", "aria-label": "Possibly wrong" },
      groupHead("flag", "Possibly wrong", additional.possibly_wrong.length));
    if (additional.possibly_wrong.length === 0) {
      wrong.append(el("p", { class: "help", text: syn.reviewSkipped ? "The review was skipped, so nothing was checked." : "Reviewers flagged no serious issues." }));
    }
    for (const w of additional.possibly_wrong) {
      wrong.append(el("div", { class: "issue" }, [
        el("div", { class: "issue-head" }, [
          el("strong", { text: labelText(w.target) }),
          el("span", { class: "tag " + (w.severity === "critical" ? "tag-err" : "tag-warn"), text: w.severity }),
          el("span", { class: "help", text: "flagged by " + plural(w.flagged_by, "reviewer") }),
        ]),
        el("blockquote", { text: w.quote }),
        el("div", { text: w.problem }),
      ]));
    }

    const regen = el("div", { class: "regen" }, [
      el("button", { type: "button", id: "resynth-btn", class: "btn btn-primary", text: "Regenerate final answer", onclick: onResynthesize }),
      el("span", { id: "resynth-hint", class: "status", "aria-live": "polite" }),
    ]);
    return el("section", { class: "additional", "aria-labelledby": "additional-heading" }, [
      el("div", { class: "additional-head" }, [
        el("h3", { id: "additional-heading", text: "Additional list" }),
        el("p", { class: "help", text: "Rule on a dispute or include an insight, then regenerate the final answer." }),
      ]),
      el("div", { class: "add-groups" }, [disputed, unique, wrong]),
      regen,
    ]);
  }

  function currentRulings() {
    const rulings = [];
    for (const [id, label] of Object.entries(syn.choices.dispute)) {
      if (label) rulings.push({ kind: "dispute", target: id, decision: label });
    }
    for (const [index, include] of Object.entries(syn.choices.unique)) {
      if (include) rulings.push({ kind: "unique", target: String(index), decision: "include" });
    }
    return rulings;
  }

  function onDisputeChoice(id, label) {
    syn.choices.dispute[id] = label;
    updateResynthButton();
  }

  function onUniqueChoice(index, include) {
    syn.choices.unique[index] = include;
    updateResynthButton();
  }

  function resynthBody() {
    return Object.assign({}, syn.runSettings, {
      seed: syn.seed,
      agreement: syn.agreement,
      reviews: syn.reviews,
      previous: syn.outcome.synthesis,
      rulings: currentRulings(),
      allow_unknown_cost: synConsented(),
    });
  }

  function scheduleResynthEstimate(delay) {
    clearTimeout(syn.resynthTimer);
    syn.resynthEstimate = null;
    updateResynthButton();
    syn.resynthTimer = setTimeout(async () => {
      const seq = ++syn.resynthSeq;
      try {
        const est = await api("/api/synthesis/resynthesize-estimate", resynthBody());
        if (seq !== syn.resynthSeq) return;
        syn.resynthEstimate = est;
      } catch (error) {
        if (seq !== syn.resynthSeq) return;
        syn.resynthEstimate = null;
        const hint = $("resynth-hint");
        if (hint) hint.textContent = error.message;
      }
      updateResynthButton();
    }, delay);
  }

  function updateResynthButton() {
    const btn = $("resynth-btn");
    if (!btn) return;
    const est = syn.resynthEstimate;
    const currency = est ? est.estimate.currency || syn.currency : syn.currency;
    if (!syn.busy) {
      btn.textContent = "Regenerate final answer" + (est
        ? " (\u2248 " + costText(money(currency, est.estimate.total_high), hasPriced(est.estimate.lines), est.budget.cli_calls) + ")"
        : "");
    }
    const rulings = currentRulings();
    const consented = !est || !est.budget.cli_calls || synConsented();
    btn.disabled = syn.busy || !est || est.budget.refused || rulings.length === 0 || !consented;
    const hint = $("resynth-hint");
    if (!hint) return;
    if (est && est.budget.refused) hint.textContent = "Refused by the budget: " + (est.budget.refusal_reason || "over budget");
    else if (!consented) hint.textContent = "Tick the consent box above to use subscription models.";
    else if (rulings.length === 0) hint.textContent = "Pick a position or include an insight to regenerate.";
    else hint.textContent = plural(rulings.length, "change") + " selected.";
  }

  async function onResynthesize() {
    const est = syn.resynthEstimate;
    if (!est || syn.busy) return;
    let confirmed = false;
    if (est.budget.needs_confirmation) {
      confirmed = window.confirm("The high estimate (" + money(est.budget.currency, est.budget.estimate_high) +
        ") is above your confirmation threshold. Continue?");
      if (!confirmed) return;
    }
    synMessage("");
    const body = Object.assign(resynthBody(), { confirmed: confirmed });
    setSynBusy(true);
    const btn = $("resynth-btn");
    btn.replaceChildren(el("span", { class: "spinner" }), "Regenerating\u2026");
    try {
      const result = await api("/api/synthesis/resynthesize", body);
      syn.outcome = result.chair;
      syn.choices = { dispute: {}, unique: {} };
      syn.currency = result.currency;
      for (const c of result.stage_costs) syn.costs.push(Object.assign({}, c, { resynthesis: true }));
      recordSynCost("Regenerated answer", result);
      setSynBusy(false);
      renderSynResult();
      scheduleResynthEstimate(0);
      toast("Final answer regenerated");
      const heading = $("final-heading");
      if (heading) heading.scrollIntoView({ block: "start", behavior: reducedMotion() ? "auto" : "smooth" });
    } catch (error) {
      synMessage(error.message);
      setSynBusy(false);
    } finally {
      refreshBudget();
    }
  }

  function initSynthesis() {
    $("syn-btn").addEventListener("click", onSynthesize);
    $("syn-chair").addEventListener("change", onChairChange);
    $("syn-consent").addEventListener("change", () => {
      updateSynButton();
      updateResynthButton();
      if (!syn.busy) synStatus("");
    });
    $("syn-instructions").addEventListener("input", () => scheduleSynEstimate(500));
    for (const radio of document.querySelectorAll("input[name=syn-tier]")) {
      radio.addEventListener("change", () => scheduleSynEstimate(0));
    }
  }

  // ------------------------------------------------------------------ keyboard

  // Ctrl/Cmd+Enter: inside the synthesis panel it synthesizes; elsewhere it runs when
  // the estimate is current, and estimates first otherwise. A second press runs, so
  // money is never spent before the estimate has been on screen.
  function onShortcut(event) {
    if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey) return;
    if (event.isComposing || event.keyCode === 229) return;
    const inSynthesis = $("synthesis-section").contains(document.activeElement);
    event.preventDefault();
    if (inSynthesis) {
      if (!$("syn-btn").disabled) onSynthesize();
      return;
    }
    if (!$("run-btn").disabled) onRun();
    else if (!$("estimate-btn").disabled) onEstimate();
    else updateButtons();
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

  async function init() {
    if (await maybeOnboard()) return;
    $("task").addEventListener("input", invalidateEstimate);
    $("estimate-btn").addEventListener("click", onEstimate);
    $("run-btn").addEventListener("click", onRun);
    $("consent").addEventListener("change", updateButtons);
    $("export-btn").addEventListener("click", exportMarkdown);
    $("sync-scroll").addEventListener("change", (e) => { state.syncScroll = e.target.checked; });
    document.addEventListener("keydown", onShortcut);
    initShortcutHints();
    initAttachments();
    initSynthesis();
    renderStrip();
    await loadProviders(false);
  }

  init();
})();
