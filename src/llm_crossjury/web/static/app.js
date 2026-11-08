// llm-crossjury compare UI. Vanilla JS, no build step.
// All server-provided text is inserted with textContent, never as HTML.
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
    busy: false,
    attachments: [],    // uploaded files: AttachmentInfo from /api/attachments
    uploading: 0,       // uploads in flight
  };

  const $ = (id) => document.getElementById(id);

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
      if (child === null || child === undefined) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function money(currency, value) {
    if (value === null || value === undefined) return "unknown";
    const n = Number(value);
    const digits = n !== 0 && Math.abs(n) < 0.01 ? 4 : 2;
    return currency + " " + n.toLocaleString("en-US", {
      minimumFractionDigits: digits,
      maximumFractionDigits: Math.max(digits, 4),
    });
  }

  function tokens(value) {
    return value === null || value === undefined ? "?" : Number(value).toLocaleString("en-US");
  }

  // Subscription (cli) calls have no known cost: they count against the plan.
  function planUsage(calls) {
    return "plan usage (" + calls + " call" + (calls === 1 ? "" : "s") + ")";
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
    const response = await fetch(path, options);
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

  function showMessage(text) {
    const box = $("message");
    box.textContent = text || "";
    box.hidden = !text;
  }

  // ------------------------------------------------------------------ budget

  function renderBudgetSummary() {
    const b = state.budget;
    const box = $("budget-summary");
    if (!b) { box.textContent = ""; return; }
    box.textContent =
      "Run limit " + money(b.currency, b.run_limit) +
      " \u00b7 confirm above " + money(b.currency, b.confirm_above) +
      " \u00b7 today " + money(b.currency, b.spent_today) + " of " + money(b.currency, b.daily_limit) +
      " (" + money(b.currency, b.daily_remaining) + " left)";
  }

  async function refreshBudget() {
    try {
      const data = await api("/api/providers");
      state.budget = data.budget;
      renderBudgetSummary();
    } catch (_) { /* keep the old summary */ }
  }

  // ------------------------------------------------------------------ providers
  //
  // Each card offers a backend: "api" (API key) or "cli" ("Subscription": the
  // vendor's official CLI the user signed in to). Switching rebuilds the card.

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
    for (const p of state.providers) box.append(providerCard(p));
    onSelectionChange();
  }

  function setBackend(p, backend) {
    captureCards();
    state.backends[p.name] = backend;
    $("card-" + p.name).replaceWith(providerCard(p));
    onSelectionChange();
  }

  function backendToggle(p, backend) {
    const option = (value, text) => el("label", { class: "seg" + (backend === value ? " seg-on" : "") }, [
      el("input", { type: "radio", name: "backend-" + p.name, value: value, checked: backend === value,
        onchange: () => setBackend(p, value) }),
      text,
    ]);
    return el("div", { class: "backend-toggle", role: "radiogroup", "aria-label": p.label + " backend" }, [
      option("api", "API key"),
      option("cli", "Subscription"),
    ]);
  }

  function cliStatusBlock(p) {
    const cli = p.cli;
    const name = cli.product + " (" + cli.command + ")" + (cli.version ? " " + cli.version : "");
    const line = el("p", { class: "cli-line" }, [
      el("span", { class: "badge " + (cli.available ? "badge-ok" : "badge-warn"), text: cli.available ? "CLI found" : "CLI not found" }),
      " ", name,
    ]);
    const recheck = el("button", { type: "button", class: "btn btn-small", text: "Re-check", onclick: () => loadProviders(true) });
    const help = el("details", { class: "cli-help", open: !cli.available }, [
      el("summary", { text: "Install and sign in" }),
      el("p", { class: "muted", text: "Install with one of the official commands:" }),
      el("ul", { class: "plain cli-commands" }, cli.install.map((c) => el("li", {}, el("code", { text: c })))),
      el("p", { class: "muted", text: cli.sign_in }),
      el("div", { class: "actions" }, [recheck]),
    ]);
    const detail = cli.available ? null : el("p", { class: "hint", text: cli.detail });
    return el("div", { class: "cli-status" }, [line, detail, help]);
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
      checked: !disabled && !!values.checked, onchange: onSelectionChange });
    checkbox.dataset.provider = p.name;

    const badges = [];
    if (isCli) {
      if (p.cli.experimental) badges.push(el("span", { class: "badge badge-warn", text: "experimental" }));
    } else if (p.key_status !== "not required") {
      badges.push(el("span", { class: "badge " + (keyMissing ? "badge-warn" : "badge-ok"), text: keyMissing ? "key not set" : "key set" }));
    }

    const select = el("select", { id: "model-" + p.name, disabled: disabled, "aria-label": p.label + " model", onchange: onSelectionChange });
    if (noModels) select.append(el("option", { text: "No usable models" }));
    const kept = values.models[backend];
    const chosen = models.some((m) => m.id === kept) ? kept : defaultModel;
    for (const m of models) {
      select.append(el("option", { value: m.id, text: m.label, selected: m.id === chosen }));
    }

    const temperature = el("input", {
      id: "temp-" + p.name, type: "number", min: "0", step: "0.1", placeholder: "default",
      value: values.temp || null, disabled: disabled, oninput: onSelectionChange,
    });
    const maxTokens = el("input", {
      id: "max-" + p.name, type: "number", min: "1", step: "1",
      value: values.max !== undefined ? values.max : String(state.defaultMaxTokens),
      disabled: disabled, oninput: onSelectionChange,
    });
    const settings = el("details", { class: "settings" }, [
      el("summary", { text: "Settings" }),
      el("div", { class: "settings-grid" }, [
        el("label", {}, ["Temperature", temperature]),
        el("label", {}, ["Max output tokens", maxTokens]),
      ]),
    ]);

    let hint = null;
    if (keyMissing) {
      hint = el("p", { class: "hint" }, ["Set ", el("code", { text: p.key_env }), " in .env and restart the UI."]);
    } else if (noModels && isCli) {
      if (!cliMissing) hint = el("p", { class: "hint", text: "No usable models: add a registry entry with the cli backend and cli_flags." });
    } else if (noModels) {
      hint = el("p", { class: "hint", text: "No usable models: add a verified api entry with pricing to the model registry." });
    }

    return el("div", { class: "provider" + (disabled ? " disabled" : ""), id: "card-" + p.name }, [
      el("label", { class: "provider-head", for: "use-" + p.name }, [
        checkbox, el("span", { class: "provider-name", text: p.label }), ...badges,
      ]),
      p.cli ? backendToggle(p, backend) : null,
      p.subscription_note ? el("p", { class: "muted backend-note", text: p.subscription_note }) : null,
      isCli ? cliStatusBlock(p) : null,
      select,
      settings,
      hint,
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
      renderBudgetSummary();
      renderProviders();
    } catch (error) {
      if (!state.providers.length) {
        $("providers").replaceChildren(el("p", { class: "muted", text: "Could not load providers." }));
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
    if (a.pages !== null && a.pages !== undefined) parts.push(a.pages + (a.pages === 1 ? " page" : " pages"));
    if (a.image_width && a.image_height) parts.push(a.image_width + " \u00d7 " + a.image_height + " px");
    return parts.join(" \u00b7 ");
  }

  function attachmentIds() {
    return state.attachments.map((a) => a.id);
  }

  function renderAttachments() {
    const list = $("attach-list");
    list.replaceChildren(...state.attachments.map((a) => el("li", { class: "attach-item" }, [
      el("div", { class: "attach-main" }, [
        el("span", { class: "attach-name", text: a.filename }),
        el("span", { class: "muted", text: attachmentDetails(a) }),
        el("button", { type: "button", class: "btn btn-small attach-remove", text: "Remove",
          "aria-label": "Remove " + a.filename, disabled: state.busy, onclick: () => removeAttachment(a.id) }),
      ]),
      ...a.warnings.map((w) => el("div", { class: "attach-warn", text: w })),
    ])));
    if (state.uploading) list.append(el("li", { class: "attach-item muted" }, [el("span", { class: "spinner" }), "Uploading\u2026"]));
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
      showMessage("Attachments are limited to 20 MB in total per run.");
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

  // Delivery badges: how a member receives (or would receive) each file.
  const MODE_TEXT = { native: "native", text: "as text", refused: "not sent" };
  const MODE_CLASS = { native: "badge-ok", text: "badge-info", refused: "badge-error" };

  function deliveryList(items) {
    if (!items || !items.length) return null;
    return el("ul", { class: "plain attach-modes" }, items.map((r) => el("li", {}, [
      el("span", { class: "badge " + (MODE_CLASS[r.mode] || ""), text: MODE_TEXT[r.mode] || r.mode }),
      " ", el("span", { class: "attach-name", text: r.filename }),
      ...r.warnings.map((w) => el("div", { class: "attach-warn", text: w })),
    ])));
  }

  // ------------------------------------------------------------------ estimate

  function onSelectionChange() {
    for (const p of state.providers) {
      const card = $("card-" + p.name);
      const checkbox = $("use-" + p.name);
      if (card && checkbox) card.classList.toggle("selected", checkbox.checked && !checkbox.disabled);
    }
    invalidateEstimate();
  }

  function invalidateEstimate() {
    state.estimate = null;
    state.estimateKey = null;
    $("estimate").hidden = true;
    renderPrivacy();
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
    $("run-btn").disabled = state.busy || uploading || !estimateValid || state.estimate.budget.refused || !consented;
    $("attach-btn").disabled = state.busy;
    const hint = $("action-hint");
    if (uploading) hint.textContent = "Uploading files\u2026";
    else if (!ready) hint.textContent = "Enter a task and pick at least one model.";
    else if (!estimateValid) hint.textContent = "Estimate the cost before running.";
    else if (state.estimate.budget.refused) hint.textContent = "The budget refuses this run.";
    else if (!consented) hint.textContent = "Tick the consent box to run subscription models.";
    else hint.textContent = "";
  }

  function renderEstimate(est) {
    const b = est.budget;
    const box = $("estimate");
    const range = money(est.currency, est.total_low) + " \u2013 " + money(est.currency, est.total_high);
    box.replaceChildren(
      el("div", { class: "estimate-range" }, [
        "Estimated cost: " + costText(range, hasPriced(est.lines), est.cli_calls),
      ]),
      el("ul", { class: "estimate-lines" }, est.lines.map((line) =>
        el("li", { text: providerLabel(line.provider) + (line.backend === "cli" ? " (subscription)" : "") +
          " \u00b7 " + line.model + ": " + (line.backend === "cli"
            ? planUsage(line.calls)
            : money(est.currency, line.cost_low) + " \u2013 " + money(est.currency, line.cost_high)) }))),
      el("p", { class: "estimate-note muted", text:
        "Budget: run limit " + money(b.currency, b.run_limit) +
        ", confirm above " + money(b.currency, b.confirm_above) +
        ", today " + money(b.currency, b.daily_remaining) + " left of " + money(b.currency, b.daily_limit) + "." }),
    );
    if (est.cli_calls) {
      box.append(el("p", { class: "estimate-note muted", text:
        "Subscription calls have unknown cost and count against your plan's usage limits; budget limits apply to priced calls only." }));
    }
    const attachLines = est.attachments || [];
    if (attachLines.length) {
      box.append(el("p", { class: "estimate-note", text: "Attachments (estimated input tokens per model):" }));
      box.append(el("ul", { class: "estimate-lines" }, attachLines.map((a) =>
        el("li", { text: providerLabel(a.provider) + (a.backend === "cli" ? " (subscription)" : "") +
          " \u00b7 " + a.filename + ": " + (a.mode === "refused"
            ? "not sent (cannot be read by this model)"
            : (a.mode === "native" ? "native" : "as text") + ", \u2248 " + tokens(a.tokens) + " tok" +
              (a.vendor_counted ? " (total counted by the vendor)" : "")) }))));
    }
    for (const w of est.warnings) box.append(el("p", { class: "estimate-note note-warn", text: w }));
    if (b.refused) {
      box.append(el("p", { class: "estimate-note note-error", text: "Refused: " + (b.refusal_reason || "over budget") }));
    } else if (b.needs_confirmation) {
      box.append(el("p", { class: "estimate-note note-warn", text:
        "The high estimate is above your confirmation threshold; you will be asked to confirm." }));
    }
    box.hidden = false;
  }

  async function onEstimate() {
    showMessage("");
    const req = currentRequest();
    state.busy = true;
    updateButtons();
    try {
      const est = await api("/api/estimate", req);
      state.estimate = est;
      state.estimateKey = JSON.stringify(req);
      state.budget = Object.assign({}, state.budget || {}, {
        currency: est.budget.currency,
        run_limit: est.budget.run_limit,
        confirm_above: est.budget.confirm_above,
        daily_limit: est.budget.daily_limit,
        spent_today: est.budget.spent_today,
        daily_remaining: est.budget.daily_remaining,
      });
      renderBudgetSummary();
      renderEstimate(est);
    } catch (error) {
      invalidateEstimate();
      showMessage(error.message);
    } finally {
      state.busy = false;
      updateButtons();
    }
  }

  // ------------------------------------------------------------------ run

  function providerLabel(name) {
    const p = state.providers.find((x) => x.name === name);
    return p ? p.label : name;
  }

  function modelLine(model, backend) {
    return model + (backend === "cli" ? " \u00b7 subscription" : "");
  }

  function renderPending(req) {
    $("results").replaceChildren(...req.selections.map((s) =>
      el("article", { class: "result" }, [
        el("div", { class: "result-head" }, [
          el("div", { class: "result-provider", text: providerLabel(s.provider) }),
          el("div", { class: "result-model", text: modelLine(s.model, s.backend) }),
        ]),
        el("div", { class: "result-body pending" }, [el("span", { class: "spinner" }), "Waiting for the answer..."]),
      ])));
    $("run-total").textContent = "";
    $("results-section").hidden = false;
  }

  function renderResults(result) {
    $("results").replaceChildren(...result.members.map((m) => {
      const meta = [];
      const cli = m.backend === "cli";
      if (m.usage) {
        const known = (v) => v !== null && v !== undefined;
        if (!cli || known(m.usage.input_tokens)) meta.push("in " + tokens(m.usage.input_tokens) + " tok");
        if (!cli || known(m.usage.output_tokens)) meta.push("out " + tokens(m.usage.output_tokens) + " tok");
        if (m.usage.reasoning_tokens) meta.push("reasoning " + tokens(m.usage.reasoning_tokens) + " tok");
        if (m.usage.cached_input_tokens) meta.push("cached " + tokens(m.usage.cached_input_tokens) + " tok");
      }
      if (m.error === null) meta.push(cli && m.cost === null ? planUsage(1) : money(m.currency, m.cost));
      if (m.latency_s !== null && m.latency_s !== undefined) meta.push(Number(m.latency_s).toFixed(1) + " s");
      return el("article", { class: "result" }, [
        el("div", { class: "result-head" }, [
          el("div", { class: "result-provider", text: providerLabel(m.provider) }),
          el("div", { class: "result-model", text: modelLine(m.model, m.backend) }),
          meta.length ? el("div", { class: "result-meta" }, meta.map((t) => el("span", { text: t }))) : null,
        ]),
        deliveryList(m.attachments),
        m.error !== null
          ? el("div", { class: "result-error", text: "Error: " + m.error })
          : el("div", { class: "result-body", text: m.text }),
      ]);
    }));
    const planCalls = result.members.filter((m) => m.backend === "cli" && m.error === null && m.cost === null).length;
    const otherUnknown = result.unknown_cost_members - planCalls;
    let total = "Total " + money(result.currency, result.total_cost);
    if (planCalls) total += " + " + planUsage(planCalls);
    if (otherUnknown > 0) total += " (+ " + otherUnknown + " with unknown cost)";
    $("run-total").textContent = total;
    $("results-section").hidden = false;
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
    renderAttachments();
    updateButtons();
    resetSynthesis();
    renderPending(req);
    try {
      const body = Object.assign({}, req, { confirmed: confirmed, allow_unknown_cost: consent });
      const result = await api("/api/run", body);
      renderResults(result);
      setupSynthesis(req.task, result, req.attachment_ids);
    } catch (error) {
      $("results-section").hidden = true;
      showMessage(error.message);
    } finally {
      state.busy = false;
      renderAttachments();
      $("consent").checked = false;  // consent is given per run
      invalidateEstimate();  // spend changed: a new run needs a fresh estimate
      refreshBudget();
    }
  }

  // ------------------------------------------------------------------ synthesis (step 2)
  //
  // Stateless: every request posts the step-1 material, the label seed and the
  // settings back to the server, plus earlier stage outputs where needed.

  const STAGE_TITLES = { agreement: "Agreement", review: "Review", chair: "Chair" };
  const ICON_DISPUTED = "\u2696\ufe0f";
  const ICON_UNIQUE = "\ud83d\udca1";
  const ICON_WRONG = "\u274c";

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

  function shortMoney(currency, value) {
    if (value === null || value === undefined) return "?";
    const n = Number(value);
    const digits = n !== 0 && Math.abs(n) < 0.01 ? 4 : 2;
    const amount = n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
    return currency === "USD" ? "$" + amount : currency + " " + amount;
  }

  function moneyRange(currency, low, high) {
    return shortMoney(currency, low) + "\u2013" + shortMoney(currency, high);
  }

  function modelText(choice, backend) {
    return providerLabel(choice.provider) + " \u00b7 " + choice.model_id + (backend === "cli" ? " (subscription)" : "");
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
    return info ? label + " (" + providerLabel(info.provider) + " \u00b7 " + info.model + ")" : label;
  }

  function synMessage(text) {
    const box = $("syn-message");
    box.textContent = text || "";
    box.hidden = !text;
  }

  function synStatus(text, spinning) {
    const box = $("syn-status");
    box.replaceChildren();
    if (spinning) box.append(el("span", { class: "spinner" }));
    if (text) box.append(text);
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
      if (node.id !== "syn-reveal") node.disabled = busy;
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
      $("syn-unavailable").textContent = "AI synthesis needs at least two successful answers.";
      return;
    }
    syn.material = { task: task, answers: answers, attachment_ids: ids };
    const note = $("syn-attach-note");
    note.textContent = ids.length
      ? "Only the chair model receives the " + ids.length + " attached file" + (ids.length === 1 ? "" : "s") +
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
      add(a.provider, a.model, a.backend, "Models above");
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
        synStatus("");
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
    if (stage === "agreement") return "judge " + modelText(est.judge, est.chair_backend);
    if (stage === "chair") return "chair " + modelText(est.chair, est.chair_backend);
    const models = est.reviewers.map((r) => r.model.model_id + (r.backend === "cli" ? " (subscription)" : ""));
    return est.reviewers.length + " reviewer(s): " + Array.from(new Set(models)).join(", ");
  }

  function synTotalText(est) {
    const priced = est.stages.some((s) => hasPriced(s.estimate.lines));
    return costText(moneyRange(est.currency, est.total_low, est.total_high), priced, est.cli_calls);
  }

  function renderSynBreakdown(est) {
    const body = $("syn-breakdown-body");
    body.replaceChildren(
      el("table", { class: "cost-table" }, [
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
      ]),
      el("p", { class: "muted", text: "Review can be skipped after the agreement check. Estimates assume no prompt-cache discount." +
        (est.scrubbed ? " Model and vendor names are removed from the answers before review." : "") }),
    );
    $("syn-breakdown").hidden = false;
  }

  function updateSynButton() {
    const btn = $("syn-btn");
    const est = syn.estimate;
    let label = "AI synthesize";
    if (est) label += " (\u2248 " + synTotalText(est) + ")";
    btn.textContent = label;
    const consented = !est || !est.cli_calls || synConsented();
    btn.disabled = syn.busy || !syn.material || !est || est.budget.refused || !consented;
  }

  function addCosts(result) {
    syn.currency = result.currency;
    for (const c of result.stage_costs) syn.costs.push(c);
  }

  async function onSynthesize() {
    const est = syn.estimate;
    if (!est || syn.busy) return;
    let confirmed = false;
    if (est.budget.needs_confirmation) {
      confirmed = window.confirm(
        "The high estimate (" + shortMoney(est.currency, est.total_high) + ") is above your confirmation threshold (" +
        shortMoney(est.currency, est.budget.confirm_above) + "). Continue?");
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
    synStatus("Measuring agreement\u2026", true);
    try {
      const body = Object.assign({}, syn.runSettings, { confirmed: confirmed });
      const step = await api("/api/synthesis/agreement", body);
      syn.runId = step.run_id;
      syn.seed = step.seed;
      syn.labels = step.labels;
      syn.agreement = step.agreement;
      addCosts(step);
      if (step.suggest_skip) {
        synStatus("");
        showSkipDecision(step);
        return;  // stays busy until the user decides
      }
      await finish(false);
    } catch (error) {
      synStatus("");
      synMessage(error.message);
      setSynBusy(false);
      refreshBudget();
    }
  }

  function showSkipDecision(step) {
    const ratio = Math.round(step.agreement.ratio * 100);
    const review = step.review_estimate;
    const saving = costText(shortMoney(review.currency || syn.currency, review.total_high),
      hasPriced(review.lines), cliCalls(review.lines));
    const box = $("syn-decision");
    box.replaceChildren(
      el("p", { class: "decision-title", text: "The answers largely agree (agreement " + ratio + "%)." }),
      el("p", { class: "muted", text: "A peer review may add little. You can skip it and let the chair write the final answer now." }),
      el("div", { class: "actions" }, [
        el("button", { type: "button", class: "btn btn-primary", text: "Skip review (saves \u2248 " + saving + ")", onclick: () => decide(true) }),
        el("button", { type: "button", class: "btn", text: "Continue with review", onclick: () => decide(false) }),
      ]),
    );
    box.hidden = false;
  }

  async function decide(skip) {
    $("syn-decision").hidden = true;
    try {
      await finish(skip);
    } catch (error) {
      synStatus("");
      synMessage(error.message);
      setSynBusy(false);
      refreshBudget();
    }
  }

  async function finish(skip) {
    synStatus(skip ? "Writing final answer\u2026" : "Reviewing\u2026 then writing final answer\u2026", true);
    const body = Object.assign({}, syn.runSettings, { seed: syn.seed, run_id: syn.runId, agreement: syn.agreement, skip_review: skip });
    const step = await api("/api/synthesis/finish", body);
    syn.reviews = step.reviews;
    syn.reviewSkipped = step.review_skipped;
    syn.outcome = step.chair;
    syn.choices = { dispute: {}, unique: {} };
    addCosts(step);
    synStatus("");
    setSynBusy(false);
    renderSynResult();
    scheduleResynthEstimate(0);
    refreshBudget();
  }

  // ---- safe rendering of the final text: a tiny Markdown subset, DOM nodes only

  function inlineNodes(text) {
    const nodes = [];
    const pattern = /(\*\*[^*]+\*\*|`[^`]+`)/g;
    let last = 0;
    let match;
    while ((match = pattern.exec(text)) !== null) {
      if (match.index > last) nodes.push(document.createTextNode(text.slice(last, match.index)));
      const token = match[0];
      if (token.startsWith("**")) nodes.push(el("strong", { text: token.slice(2, -2) }));
      else nodes.push(el("code", { text: token.slice(1, -1) }));
      last = match.index + token.length;
    }
    if (last < text.length) nodes.push(document.createTextNode(text.slice(last)));
    return nodes;
  }

  function renderMarkdown(text) {
    const root = el("div", { class: "md" });
    const lines = String(text || "").split(/\r?\n/);
    let para = [];
    let list = null;
    let code = null;
    const flushPara = () => {
      if (para.length) root.append(el("p", {}, inlineNodes(para.join("\n"))));
      para = [];
    };
    const flushList = () => { if (list) root.append(list); list = null; };
    for (const line of lines) {
      if (code !== null) {
        if (line.trim().startsWith("```")) { root.append(el("pre", {}, el("code", { text: code.join("\n") }))); code = null; }
        else code.push(line);
        continue;
      }
      if (line.trim().startsWith("```")) { flushPara(); flushList(); code = []; continue; }
      const heading = /^(#{1,6})\s+(.*)$/.exec(line);
      const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
      const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
      if (heading) {
        flushPara(); flushList();
        root.append(el(heading[1].length <= 2 ? "h3" : "h4", {}, inlineNodes(heading[2])));
      } else if (bullet || numbered) {
        flushPara();
        const tag = bullet ? "ul" : "ol";
        if (!list || list.tagName.toLowerCase() !== tag) { flushList(); list = el(tag); }
        list.append(el("li", {}, inlineNodes((bullet || numbered)[1])));
      } else if (line.trim() === "") {
        flushPara(); flushList();
      } else {
        flushList();
        para.push(line);
      }
    }
    if (code !== null) root.append(el("pre", {}, el("code", { text: code.join("\n") })));
    flushPara();
    flushList();
    return root;
  }

  // ---- results

  function renderSynResult() {
    const outcome = syn.outcome;
    if (!outcome) return;
    const synthesis = outcome.synthesis;
    const ratio = syn.agreement && syn.agreement.ratio !== null && syn.agreement.ratio !== undefined
      ? Math.round(syn.agreement.ratio * 100) + "%" : "n/a";
    const total = syn.costs.reduce((sum, c) => sum + Number(c.cost), 0);
    const unknown = syn.costs.reduce((sum, c) => sum + c.unknown_cost_calls, 0);
    const totalText = shortMoney(syn.currency, total) + (unknown ? " + " + planUsage(unknown) : "");

    const reveal = el("label", { class: "reveal" }, [
      el("input", { type: "checkbox", id: "syn-reveal", checked: syn.reveal, onchange: (e) => { syn.reveal = e.target.checked; renderSynResult(); } }),
      "Reveal models",
    ]);

    const final = el("article", { class: "final" }, [
      el("div", { class: "final-head" }, [
        el("h3", { text: "Final answer" }),
        el("div", { class: "final-meta" }, [
          el("span", { text: "Agreement " + ratio }),
          el("span", { text: syn.reviewSkipped ? "Review skipped" : "Reviewed" }),
          el("span", { text: "Chair " + modelText(syn.runSettings.chair, syn.runSettings.chair_backend) }),
          el("span", { text: "Cost " + totalText }),
        ]),
      ]),
      renderMarkdown(synthesis.final_text),
    ]);

    const side = el("div", { class: "synth-side" }, [
      renderRanking(),
      renderCosts(total, unknown),
      el("div", { class: "side-block" }, [
        el("h4", { text: "Anonymous labels" }), reveal,
        syn.reveal ? el("ul", { class: "plain" }, syn.labels.map((l) =>
          el("li", { text: l.label + ": " + providerLabel(l.provider) + " \u00b7 " + l.model }))) : null,
      ]),
    ]);

    $("syn-result").replaceChildren(
      el("div", { class: "synth-grid" }, [final, side]),
      renderAdditional(outcome.additional),
    );
    $("syn-result").hidden = false;
    updateResynthButton();
  }

  function renderRanking() {
    const block = el("div", { class: "side-block" }, el("h4", { text: "Ranking" }));
    const ranking = syn.reviews ? syn.reviews.ranking : [];
    if (!ranking || ranking.length === 0) {
      block.append(el("p", { class: "muted", text: syn.reviewSkipped ? "No ranking: review was skipped." : "No ranking available." }));
      return block;
    }
    block.append(el("ol", { class: "ranking" }, ranking.map((r) => el("li", {}, [
      el("span", { text: labelText(r.label) }),
      el("span", { class: "muted", text: r.score === null ? " (no informative review)" : " (score " + Number(r.score).toFixed(2) + ")" }),
    ]))));
    return block;
  }

  // Known cost on the first line, subscription calls (if any) on a second one.
  function costCell(cost, calls, unknown) {
    if (unknown && unknown === calls) return el("td", { class: "num", text: planUsage(unknown) });
    return el("td", { class: "num" }, [
      shortMoney(syn.currency, cost),
      unknown ? el("div", { class: "muted", text: "+ " + planUsage(unknown) }) : null,
    ]);
  }

  function renderCosts(total, unknown) {
    return el("div", { class: "side-block" }, [
      el("h4", { text: "Cost" }),
      el("table", { class: "cost-table" }, [
        el("tbody", {}, syn.costs.map((c) => el("tr", {}, [
          el("td", { text: (STAGE_TITLES[c.stage] || c.stage) + (c.resynthesis ? " (regenerated)" : "") }),
          el("td", { class: "num muted", text: c.calls + " call(s)" }),
          costCell(c.cost, c.calls, c.unknown_cost_calls),
        ]))),
        el("tfoot", {}, el("tr", {}, [el("td", { text: "Total" }), el("td", {}), costCell(total, -1, unknown)])),
      ]),
    ]);
  }

  function renderAdditional(additional) {
    const wrap = el("section", { class: "additional" }, el("h3", { text: "Additional list" }));

    const disputed = el("div", { class: "add-section" }, el("h4", { text: ICON_DISPUTED + " Disputed" }));
    if (additional.disputed.length === 0) disputed.append(el("p", { class: "muted", text: "No disputed points." }));
    for (const d of additional.disputed) {
      const name = "ruling-" + d.id;
      const choices = el("div", { class: "positions" }, [
        el("label", { class: "position" }, [
          el("input", { type: "radio", name: name, value: "", checked: !syn.choices.dispute[d.id], onchange: () => onDisputeChoice(d.id, "") }),
          el("div", {}, [el("strong", { text: "Keep the chair's decision" })]),
        ]),
      ]);
      for (const p of d.positions) {
        const input = el("input", { type: "radio", name: name, value: p.label, checked: syn.choices.dispute[d.id] === p.label,
          onchange: () => onDisputeChoice(d.id, p.label) });
        choices.append(el("label", { class: "position" }, [
          input,
          el("div", {}, [
            el("div", {}, [el("strong", { text: labelText(p.label) + ": " }), p.stance]),
            el("div", { class: "muted", text: p.argument }),
          ]),
        ]));
      }
      disputed.append(el("div", { class: "dispute" }, [
        el("div", { class: "dispute-q", text: d.question }),
        d.chair_assessment ? el("div", { class: "chair-pick" }, [
          el("span", { class: "badge badge-warn", text: "decided by AI" }), " ", d.chair_assessment,
        ]) : null,
        choices,
      ]));
    }

    const unique = el("div", { class: "add-section" }, el("h4", { text: ICON_UNIQUE + " Unique insights not included" }));
    if (additional.unique.length === 0) unique.append(el("p", { class: "muted", text: "None." }));
    for (const [index, u] of additional.unique.entries()) {
      const box = el("input", { type: "checkbox", checked: !!syn.choices.unique[index],
        onchange: (e) => onUniqueChoice(index, e.target.checked) });
      unique.append(el("label", { class: "insight" }, [
        box,
        el("div", {}, [el("strong", { text: labelText(u.label) + ": " }), u.insight, el("span", { class: "muted", text: " (include)" })]),
      ]));
    }

    const wrong = el("div", { class: "add-section" }, el("h4", { text: ICON_WRONG + " Possibly wrong" }));
    if (additional.possibly_wrong.length === 0) {
      wrong.append(el("p", { class: "muted", text: syn.reviewSkipped ? "Review was skipped, so nothing was checked." : "Reviewers flagged no serious issues." }));
    }
    for (const w of additional.possibly_wrong) {
      wrong.append(el("div", { class: "issue" }, [
        el("div", {}, [
          el("strong", { text: labelText(w.target) }),
          el("span", { class: "badge " + (w.severity === "critical" ? "badge-error" : "badge-warn"), text: w.severity }),
          el("span", { class: "muted", text: " flagged by " + w.flagged_by + " reviewer(s)" }),
        ]),
        el("blockquote", { text: w.quote }),
        el("div", { text: w.problem }),
      ]));
    }

    const regen = el("div", { class: "actions" }, [
      el("button", { type: "button", id: "resynth-btn", class: "btn btn-primary", text: "Regenerate final answer", onclick: onResynthesize }),
      el("span", { id: "resynth-hint", class: "muted" }),
    ]);
    wrap.append(disputed, unique, wrong, regen);
    return wrap;
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
    btn.textContent = "Regenerate final answer" + (est
      ? " (\u2248 " + costText(shortMoney(currency, est.estimate.total_high), hasPriced(est.estimate.lines), est.budget.cli_calls) + ")"
      : "");
    const rulings = currentRulings();
    const consented = !est || !est.budget.cli_calls || synConsented();
    btn.disabled = syn.busy || !est || est.budget.refused || rulings.length === 0 || !consented;
    const hint = $("resynth-hint");
    if (!hint) return;
    if (est && est.budget.refused) hint.textContent = "Refused by the budget: " + (est.budget.refusal_reason || "over budget");
    else if (!consented) hint.textContent = "Tick the consent box above to use subscription models.";
    else if (rulings.length === 0) hint.textContent = "Pick a position or include an insight to regenerate.";
    else hint.textContent = rulings.length + " change(s) selected.";
  }

  async function onResynthesize() {
    const est = syn.resynthEstimate;
    if (!est || syn.busy) return;
    let confirmed = false;
    if (est.budget.needs_confirmation) {
      confirmed = window.confirm("The high estimate (" + shortMoney(est.budget.currency, est.budget.estimate_high) +
        ") is above your confirmation threshold. Continue?");
      if (!confirmed) return;
    }
    synMessage("");
    const body = Object.assign(resynthBody(), { confirmed: confirmed });
    setSynBusy(true);
    synStatus("Writing final answer\u2026", true);
    try {
      const result = await api("/api/synthesis/resynthesize", body);
      syn.outcome = result.chair;
      syn.choices = { dispute: {}, unique: {} };
      syn.currency = result.currency;
      for (const c of result.stage_costs) syn.costs.push(Object.assign({}, c, { resynthesis: true }));
      synStatus("");
      setSynBusy(false);
      renderSynResult();
      scheduleResynthEstimate(0);
    } catch (error) {
      synStatus("");
      synMessage(error.message);
      setSynBusy(false);
    } finally {
      refreshBudget();
    }
  }

  function initSynthesis() {
    $("syn-btn").addEventListener("click", onSynthesize);
    $("syn-chair").addEventListener("change", onChairChange);
    $("syn-consent").addEventListener("change", () => { updateSynButton(); updateResynthButton(); });
    $("syn-instructions").addEventListener("input", () => scheduleSynEstimate(500));
    for (const radio of document.querySelectorAll("input[name=syn-tier]")) {
      radio.addEventListener("change", () => scheduleSynEstimate(0));
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
    initAttachments();
    initSynthesis();
    await loadProviders(false);
  }

  init();
})();
