// llm-crossjury home page: the composer pinned under the thread, and what it sends.
//
// Two modes share one message box and its attachments:
// * chat: one model answers (POST /api/chat). A model picker, Send, and a context
//   meter from POST /api/chat/estimate. "Continue with ..." buttons on jury turns
//   switch here and record which answer the chat continues.
// * jury: today's flow. The model rack above the composer, Estimate cost, then Ask N
//   models (POST /api/estimate, then /api/run). An empty conversation starts here.
// The cost readouts (estimate, last cost, today's spend) sit under the composer.
"use strict";

(function () {
  const CJ = window.CJ;
  const {
    $, el, icon, iconButton, money, moneyRange, tokens, shortTokens, plural, planUsage, hasPriced, costText,
    api, showMessage, toast, storageGet, storageSet, randomId, keyboardHints, noteLine, askDialog,
    providerLabel, derivedImagesText, scrollBehavior,
  } = CJ;
  const state = CJ.state;

  const MODEL_KEY = "llm-crossjury-chat-model";
  const ATTACH_MAX_TOTAL = 20 * 1024 * 1024;
  const UPLOAD_HEADER = "X-Crossjury-Upload";
  const KIND_TEXT = { pdf: "PDF", document: "Word", image: "Image" };
  const LAYERS = [
    ["prefix", "Stable prefix", "System prompt and the conversation's files (cached by the vendor)"],
    ["summary", "Summary", "Compressed notes of older turns"],
    ["retrieved", "Retrieved", "Older passages that match this message"],
    ["recent", "Recent turns", "The last turns, word for word"],
    ["message", "Your message", "This message and its files"],
  ];

  const C = {
    mode: "jury",           // "jury" | "chat"
    attachments: [],        // uploaded files: AttachmentInfo from /api/attachments
    sentIds: new Set(),     // attachment ids already used by a turn (never deleted here)
    uploading: 0,
    busy: false,            // jury estimate or run in flight
    chatSending: false,
    estimate: null,         // last jury estimate
    estimateKey: null,
    staleEstimate: false,
    chatOptions: [],        // [{provider, backend, model, label, group}]
    extraOption: null,      // a model added for a continuation that the picker lacks
    continuation: null,     // { continued_from, label }
    chatEstimate: null,
    chatSeq: 0,
    chatTimer: null,
  };

  function chatAvailable() {
    return state.sessionsApi === true;
  }

  // ------------------------------------------------------------------ readouts

  function readout(id, value, sub, cls) {
    const box = $(id);
    box.querySelector(".readout-value").textContent = value;
    box.querySelector(".readout-sub").textContent = sub || "";
    box.classList.toggle("is-stale", cls === "stale");
    box.classList.toggle("is-refused", cls === "refused");
  }

  function renderStrip() {
    const est = C.estimate;
    $("strip-estimate").hidden = C.mode !== "jury";
    if (est) {
      const range = moneyRange(est.currency, est.total_low, est.total_high);
      const priced = hasPriced(est.lines);
      const value = priced || !est.cli_calls ? range : plural(est.cli_calls, "plan call");
      const sub = est.budget.refused ? "Refused by the budget"
        : (priced && est.cli_calls ? "+ " + plural(est.cli_calls, "plan call") : plural(est.lines.length, "model"));
      readout("strip-estimate", value, sub, est.budget.refused ? "refused" : null);
    } else if (C.staleEstimate) {
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

  // ------------------------------------------------------------------ attachments
  //
  // Files are uploaded once (POST /api/attachments, multipart) and then referred to
  // by id (their sha256) in estimate, run, chat and synthesis requests. The server
  // checks the type by content; the accept list only filters the file picker.

  function fileSize(bytes) {
    if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + " MB";
    if (bytes >= 1024) return Math.round(bytes / 1024) + " KB";
    return bytes + " B";
  }

  function attachmentDetails(a) {
    const parts = [KIND_TEXT[a.kind] || a.kind, fileSize(a.size)];
    if (a.pages !== null && a.pages !== undefined) parts.push(plural(a.pages, "page"));
    if (a.image_width && a.image_height) parts.push(a.image_width + "\u00d7" + a.image_height + " px");
    const derived = (a.derived || []).length;
    if (derived) {
      parts.push(a.kind === "pdf"
        ? (derived === 1 ? "1 page rendered as an image" : derived + " pages rendered as images")
        : plural(derived, "image") + " for image-capable models");
    }
    return parts.join(", ");
  }

  function attachmentIds() {
    return C.attachments.map((a) => a.id);
  }

  function renderAttachments() {
    const list = $("attach-list");
    list.replaceChildren(...C.attachments.map((a) => el("li", { class: "attach-item" }, [
      el("div", { class: "attach-main" }, [
        el("span", { class: "attach-name", text: a.filename }),
        el("span", { class: "attach-meta", text: attachmentDetails(a) }),
        iconButton("x", "Remove " + a.filename, () => removeAttachment(a.id),
          { class: "icon-btn attach-remove", disabled: C.busy }),
      ]),
      ...(a.warnings || []).map((w) => el("div", { class: "attach-warn", text: w })),
    ])));
    if (C.uploading) {
      list.append(el("li", { class: "attach-item" }, el("span", { class: "attach-uploading" }, [el("span", { class: "spinner" }), "Uploading\u2026"])));
    }
    list.hidden = list.childElementCount === 0;
    renderPrivacy();
  }

  // ADR 0007: before sending, list every vendor that will receive the files.
  function renderPrivacy() {
    const box = $("attach-privacy");
    if (!C.attachments.length) { box.hidden = true; box.textContent = ""; return; }
    let vendors;
    if (C.mode === "chat") {
      const m = selectedChatModel();
      vendors = m ? [providerLabel(m.provider)] : [];
    } else {
      vendors = Array.from(new Set(CJ.selections().map((s) => providerLabel(s.provider))));
    }
    box.textContent = vendors.length
      ? "These files will be sent to: " + vendors.join(", ") + "."
      : (C.mode === "chat" ? "Pick a model to see which vendor will receive these files."
        : "Select models to see which vendors will receive these files.");
    box.hidden = false;
  }

  async function uploadFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length || C.busy) return;
    showMessage("");
    const known = C.attachments.reduce((n, a) => n + a.size, 0);
    const added = files.reduce((n, f) => n + f.size, 0);
    if (known + added > ATTACH_MAX_TOTAL) {
      showMessage("Attachments are limited to 20 MB in total per message. Remove a file or attach a smaller one.");
      return;
    }
    const form = new FormData();
    for (const f of files) form.append("files", f, f.name);
    C.uploading += 1;
    renderAttachments();
    updateButtons();
    try {
      const response = await fetch("/api/attachments", { method: "POST", headers: { [UPLOAD_HEADER]: "1" }, body: form });
      let data = null;
      try { data = await response.json(); } catch (_) { data = null; }
      if (!response.ok) throw new Error(CJ.describeError(response.status, data));
      for (const a of data.attachments) {
        if (!C.attachments.some((x) => x.id === a.id)) C.attachments.push(a);
      }
    } catch (error) {
      showMessage(error.message);
    } finally {
      C.uploading -= 1;
      renderAttachments();
      invalidateEstimate();
      scheduleChatEstimate(300);
    }
  }

  async function removeAttachment(id) {
    C.attachments = C.attachments.filter((a) => a.id !== id);
    renderAttachments();
    invalidateEstimate();
    scheduleChatEstimate(300);
    $("attach-btn").focus();
    // Files a turn already used stay: the server keeps them with the conversation.
    if (C.sentIds.has(id)) return;
    try {
      await fetch("/api/attachments/" + encodeURIComponent(id), { method: "DELETE" });
    } catch (_) { /* expires on its own */ }
  }

  function clearAttachments() {
    for (const a of C.attachments) C.sentIds.add(a.id);
    C.attachments = [];
    renderAttachments();
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

  function initAttachments() {
    const input = $("attach-input");
    $("attach-btn").addEventListener("click", () => input.click());
    input.addEventListener("change", () => { uploadFiles(input.files); input.value = ""; });
    const zone = $("workspace");
    const composer = $("composer");
    const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
    zone.addEventListener("dragover", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
      composer.classList.add("drop-target");
    });
    zone.addEventListener("dragleave", (e) => {
      if (!zone.contains(e.relatedTarget)) composer.classList.remove("drop-target");
    });
    zone.addEventListener("drop", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      composer.classList.remove("drop-target");
      uploadFiles(e.dataTransfer.files);
    });
    // Paste files (e.g. a screenshot) straight into the message as attachments, like
    // the vendors' chat apps. Plain text still pastes into the box as usual.
    composer.addEventListener("paste", (e) => {
      const files = pastedFiles(e.clipboardData);
      if (!files.length) return;
      const types = Array.from((e.clipboardData && e.clipboardData.types) || []);
      if (!types.includes("text/plain")) e.preventDefault();
      uploadFiles(files);
    });
  }

  // ------------------------------------------------------------------ mode

  function setMode(mode) {
    C.mode = chatAvailable() ? mode : "jury";
    const chat = C.mode === "chat";
    document.body.classList.toggle("mode-chat", chat);
    document.body.classList.toggle("mode-jury", !chat);
    $("jury-panel").hidden = chat;
    $("chat-controls").hidden = !chat;
    $("send-btn").hidden = !chat;
    $("jury-btn").hidden = !chat;
    $("estimate-btn").hidden = chat;
    $("run-btn").hidden = chat;
    $("jury-close").hidden = !chatAvailable();
    $("context-meter").hidden = !chat || !C.chatEstimate;
    $("task").placeholder = chat
      ? "Message " + (selectedChatModel() ? providerLabel(selectedChatModel().provider) : "the model") + "\u2026"
      : "Ask a question or describe a task. Every selected model answers it on its own.";
    if (!chat) clearContinuation();
    renderPrivacy();
    renderStrip();
    updateButtons();
    if (chat) scheduleChatEstimate(0);
  }

  function openJury() {
    // Nothing picked yet: start from the models of the conversation's last jury.
    if (!CJ.selections().length) {
      const s = CJ.sessions.current();
      const last = s ? s.turns.slice().reverse().find((t) => t.kind === "jury" && (t.selections || []).length) : null;
      if (last) CJ.applySelections(last.selections);
    }
    setMode("jury");
    $("jury-panel").scrollIntoView({ block: "nearest", behavior: scrollBehavior() });
    const first = $("providers").querySelector("input[type=checkbox]:not(:disabled)");
    if (first && !$("task").value.trim()) $("task").focus();
    else if (first) first.focus();
  }

  // ------------------------------------------------------------------ chat model picker

  function optionKey(o) {
    return o.provider + "/" + o.backend + "/" + o.model;
  }

  function buildChatOptions() {
    C.chatOptions = CJ.chatModelOptions();
    const select = $("chat-model");
    const keep = select.value ? select.value : storageGet(MODEL_KEY);
    select.replaceChildren();
    const all = C.chatOptions.slice();
    if (C.extraOption && !all.some((o) => optionKey(o) === optionKey(C.extraOption))) all.push(C.extraOption);
    const groups = {};
    for (const o of all) {
      if (!groups[o.group]) {
        groups[o.group] = el("optgroup", { label: o.group });
        select.append(groups[o.group]);
      }
      groups[o.group].append(el("option", { value: optionKey(o), text: o.label }));
    }
    if (!all.length) select.append(el("option", { value: "", text: "No model available" }));
    select.disabled = !all.length;
    if (keep && all.some((o) => optionKey(o) === keep)) select.value = keep;
  }

  function selectedChatModel() {
    const key = $("chat-model").value;
    const all = C.chatOptions.concat(C.extraOption ? [C.extraOption] : []);
    const o = all.find((x) => optionKey(x) === key);
    return o ? { provider: o.provider, backend: o.backend, model: o.model } : null;
  }

  function selectChatModel(model) {
    const key = optionKey(model);
    if (!C.chatOptions.some((o) => optionKey(o) === key)) {
      C.extraOption = { provider: model.provider, backend: model.backend, model: model.model,
        label: providerLabel(model.provider) + ": " + model.model + (model.backend === "cli" ? " (subscription)" : ""), group: providerLabel(model.provider) };
      buildChatOptions();
    }
    $("chat-model").value = key;
  }

  function onChatModelChange() {
    const m = selectedChatModel();
    if (m) storageSet(MODEL_KEY, optionKey(m));
    $("task").placeholder = "Message " + (m ? providerLabel(m.provider) : "the model") + "\u2026";
    renderPrivacy();
    updateButtons();
    scheduleChatEstimate(0);
  }

  // ------------------------------------------------------------------ continuation

  function continueFrom(opts) {
    if (!chatAvailable()) return;
    setMode("chat");
    selectChatModel(opts.model);
    onChatModelChange();
    C.continuation = { continued_from: opts.continued_from, label: opts.label || "a jury answer" };
    renderChip();
    scheduleChatEstimate(0);
    const box = $("task");
    box.focus();
    $("composer").scrollIntoView({ block: "nearest", behavior: scrollBehavior() });
  }

  function clearContinuation() {
    if (!C.continuation) return;
    C.continuation = null;
    renderChip();
    scheduleChatEstimate(0);
  }

  function renderChip() {
    const row = $("continue-chip");
    if (!C.continuation) { row.hidden = true; row.replaceChildren(); return; }
    row.replaceChildren(el("span", { class: "chip" }, [
      icon("chat"),
      el("span", { text: "Continuing from " + C.continuation.label }),
      iconButton("x", "Stop continuing from " + C.continuation.label, () => { clearContinuation(); $("task").focus(); }, { class: "icon-btn chip-x" }),
    ]));
    row.hidden = false;
  }

  // ------------------------------------------------------------------ chat estimate / meter

  function chatRequest(message) {
    return {
      message: message === undefined ? $("task").value : message,
      model: selectedChatModel(),
      attachment_ids: attachmentIds(),
      max_output_tokens: null,
      continued_from: C.continuation ? C.continuation.continued_from : null,
      allow_unknown_cost: false,
      confirmed: false,
    };
  }

  function sessionQuery() {
    const s = CJ.sessions.current();
    return s && s.id ? CJ.query({ session: s.id }) : "";
  }

  function scheduleChatEstimate(delay) {
    clearTimeout(C.chatTimer);
    if (C.mode !== "chat" || !chatAvailable()) return;
    C.chatTimer = setTimeout(runChatEstimate, delay === undefined ? 500 : delay);
  }

  async function runChatEstimate() {
    const req = chatRequest();
    if (!req.model) { C.chatEstimate = null; renderMeter(); return; }
    const seq = ++C.chatSeq;
    try {
      const est = await api("/api/chat/estimate" + sessionQuery(), req);
      if (seq !== C.chatSeq) return;
      C.chatEstimate = est;
    } catch (_) {
      if (seq !== C.chatSeq) return;
      C.chatEstimate = null;
    }
    renderMeter();
  }

  function estCurrency(est) {
    return est.currency || (est.budget && est.budget.currency) || "USD";
  }

  function estCliCalls(est) {
    if (typeof est.cli_calls === "number") return est.cli_calls;
    return est.budget && typeof est.budget.cli_calls === "number" ? est.budget.cli_calls : 0;
  }

  function chatCostText(est) {
    const low = est.total_low !== undefined ? est.total_low : (est.cost_low !== undefined ? est.cost_low : (est.budget && est.budget.estimate_low));
    const high = est.total_high !== undefined ? est.total_high : (est.cost_high !== undefined ? est.cost_high : (est.budget && est.budget.estimate_high));
    const calls = estCliCalls(est);
    const priced = !calls || (Number(high) > 0);
    if (low === undefined || high === undefined || low === null) return calls ? planUsage(calls) : "cost unknown";
    return costText(moneyRange(estCurrency(est), low, high), priced, calls);
  }

  function renderMeter() {
    const box = $("context-meter");
    const est = C.chatEstimate;
    if (!est || C.mode !== "chat") { box.hidden = true; box.replaceChildren(); return; }
    const used = Number(est.context_tokens || 0);
    const limit = Number(est.context_budget_tokens || 0);
    const ratio = limit > 0 ? Math.min(1, used / limit) : 0;
    const fill = el("span");
    fill.style.width = Math.max(ratio > 0 ? 2 : 0, Math.round(ratio * 100)) + "%";  // CSSOM, allowed by the CSP
    const layers = est.context || {};
    const rows = LAYERS.filter((l) => layers[l[0]] !== undefined).map((l) => el("tr", { title: l[2] }, [
      el("td", { text: l[1] }),
      el("td", { class: "num", text: tokens(layers[l[0]]) }),
    ]));
    const b = est.budget || null;
    const summary = el("summary", { class: "meter-summary" }, [
      el("span", { class: "meter-bar" + (ratio > 0.9 ? " is-high" : ""), role: "img",
        "aria-label": "Context " + Math.round(ratio * 100) + "% of the budget" }, fill),
      el("span", { class: "meter-text", text: "Context " + shortTokens(used) + (limit ? " / " + shortTokens(limit) : "") + " tokens" }),
      el("span", { class: "meter-cost" + (b && b.refused ? " is-refused" : ""), text: "\u2248 " + chatCostText(est) }),
    ]);
    const details = el("details", { class: "meter-details" }, [
      summary,
      el("div", { class: "meter-body" }, [
        rows.length ? el("table", { class: "data-table" }, [
          el("thead", {}, el("tr", {}, [el("th", { text: "Context layer" }), el("th", { class: "num", text: "Tokens" })])),
          el("tbody", {}, rows),
          el("tfoot", {}, el("tr", {}, [el("td", { text: "Total" }), el("td", { class: "num", text: tokens(used) })])),
        ]) : null,
        el("p", { class: "help", text: "Each reply gets this context, assembled within " + tokens(limit) +
          " tokens: older turns are summarized and only matching passages come back word for word. Change the budget in Settings." }),
        b && b.refused ? noteLine("err", "Refused: " + (b.refusal_reason || "over budget") + ".") : null,
        b && !b.refused && b.needs_confirmation ? noteLine("warn", "Above your confirmation threshold: you will be asked to confirm.") : null,
      ]),
    ]);
    const wasOpen = box.querySelector("details") && box.querySelector("details").open;
    if (wasOpen) details.open = true;
    box.replaceChildren(details);
    box.hidden = false;
  }

  // ------------------------------------------------------------------ chat send

  function pendingChat() {
    const s = CJ.sessions.current();
    return !!(s && s.turns.some((t) => t.kind === "chat" && t.status === "pending"));
  }

  function consentDialog() {
    return askDialog({
      title: "Use a subscription model?",
      body: [state.consentText || "Subscription calls have unknown cost and count against your plan's usage limits."],
      confirm: "I understand, send",
    });
  }

  function confirmDialog(b, verb) {
    return askDialog({
      title: "Above your confirmation threshold",
      body: "The high estimate (" + money(b.currency, b.estimate_high) + ") is above your confirmation threshold (" +
        money(b.currency, b.confirm_above) + "). " + (verb || "Send anyway?"),
      confirm: "Send",
    });
  }

  async function sendChat(retry) {
    if (!chatAvailable() || C.chatSending) return;
    const base = retry
      ? { message: retry.message, model: retry.model, attachment_ids: (retry.attachment_ids || []).slice(),
        max_output_tokens: retry.max_output_tokens || null, continued_from: retry.continued_from || null,
        allow_unknown_cost: false, confirmed: false }
      : chatRequest();
    if (!String(base.message || "").trim()) { updateButtons(); return; }
    if (!base.model) { showMessage("Pick a model first. Add an API key or sign in to a CLI in Settings to get one."); return; }
    showMessage("");
    C.chatSending = true;
    updateButtons();
    try {
      await CJ.sessions.ensureSession();
    } catch (error) {
      C.chatSending = false;
      updateButtons();
      showMessage(error.message);
      return;
    }
    // Pre-checks from the estimate: budget, confirmation and subscription consent.
    let est = null;
    try { est = await api("/api/chat/estimate" + sessionQuery(), base); } catch (_) { est = null; }
    const req = Object.assign({}, base);
    if (est) {
      const b = est.budget;
      if (b && b.refused) {
        C.chatSending = false;
        updateButtons();
        showMessage("Refused by the budget: " + (b.refusal_reason || "over budget") + ".");
        return;
      }
      if (b && b.needs_confirmation) {
        if (!(await confirmDialog(b))) { C.chatSending = false; updateButtons(); return; }
        req.confirmed = true;
      }
      if (estCliCalls(est) > 0) {
        if (!(await consentDialog())) { C.chatSending = false; updateButtons(); return; }
        req.allow_unknown_cost = true;
      }
    }
    await postChat(req, !!retry);
  }

  async function postChat(req, isRetry) {
    const tid = randomId();
    const turn = {
      kind: "chat", id: tid, created_at: new Date().toISOString(), status: "pending",
      message: req.message, attachment_ids: req.attachment_ids, model: req.model,
      max_output_tokens: req.max_output_tokens, continued_from: req.continued_from,
      reply: null, resolved_model: null, input_tokens: null, output_tokens: null, cost: null,
      currency: null, latency_s: null, context_tokens: null, error: null,
    };
    C.chatSending = true;
    CJ.sessions.addTurn(turn);
    const saved = isRetry ? null : snapshotComposer();
    if (!isRetry) clearAfterSend();
    updateButtons();
    let again = null;
    try {
      const result = await api("/api/chat" + CJ.sessions.params(tid), req);
      const done = result && result.id === tid ? result
        : Object.assign({}, turn, { status: "error", error: "The server sent no reply." });
      CJ.sessions.replaceTurn(done, true);
      if (done.status === "done") {
        const cli = req.model.backend === "cli";
        if (cli) state.planCalls += 1;
        setLastCost("Chat reply" + (cli && (done.cost === null || done.cost === undefined) ? " (plan call)" : ""),
          done.cost !== null && done.cost !== undefined ? money(done.currency, done.cost) : (cli ? planUsage(1) : "unknown"));
      }
    } catch (error) {
      if (error.status === 409) {
        CJ.sessions.removeTurn(tid);
        if (saved) restoreComposer(saved);
        again = await resolveConflict(error, req, "Send anyway?");
      } else {
        CJ.sessions.replaceTurn(Object.assign({}, turn, { status: "error", error: error.message }), true);
      }
    } finally {
      C.chatSending = false;
      updateButtons();
    }
    if (again) {
      await postChat(again, isRetry);
      return;
    }
    refreshBudget();
    scheduleChatEstimate(0);
  }

  // A 409 from a run-type request: ask for what is missing and return the request to
  // send again, or show why it cannot run and return null.
  async function resolveConflict(error, req, verb) {
    const code = error.code;
    if (code === "consent_required" && !req.allow_unknown_cost) {
      return (await consentDialog()) ? Object.assign({}, req, { allow_unknown_cost: true }) : null;
    }
    if (code === "confirmation_required" && !req.confirmed && error.data && error.data.budget) {
      return (await confirmDialog(error.data.budget, verb)) ? Object.assign({}, req, { confirmed: true }) : null;
    }
    showMessage(code === "budget_refused" ? "Refused by the budget: " + error.message : error.message);
    return null;
  }

  function retryChat(turn) {
    if (C.chatSending) return;
    sendChat({ message: turn.message, model: turn.model, attachment_ids: turn.attachment_ids,
      max_output_tokens: turn.max_output_tokens, continued_from: turn.continued_from });
  }

  function snapshotComposer() {
    return { text: $("task").value, attachments: C.attachments.slice(), continuation: C.continuation };
  }

  function restoreComposer(saved) {
    $("task").value = saved.text;
    C.attachments = saved.attachments;
    for (const a of saved.attachments) C.sentIds.delete(a.id);
    C.continuation = saved.continuation;
    renderAttachments();
    renderChip();
    autoGrow();
    updateButtons();
  }

  function clearAfterSend() {
    $("task").value = "";
    clearAttachments();
    C.continuation = null;
    renderChip();
    autoGrow();
  }

  // Put text back into the composer (Ask again, Edit message).
  function prefill(text, mode, turn) {
    setMode(mode || C.mode);
    if (turn && turn.kind === "chat" && turn.model) {
      selectChatModel(turn.model);
      onChatModelChange();
      if (turn.continued_from) {
        C.continuation = { continued_from: turn.continued_from, label: "a jury answer" };
        renderChip();
      }
    }
    $("task").value = text || "";
    autoGrow();
    invalidateEstimate();
    $("task").focus();
  }

  // ------------------------------------------------------------------ jury: estimate and run

  function currentRequest() {
    return { task: $("task").value, selections: CJ.selections(), attachment_ids: attachmentIds() };
  }

  function needsConsent(req) {
    return req.selections.some((s) => s.backend === "cli");
  }

  function estimateKey(req) {
    const s = CJ.sessions.current();
    const hasContext = !!(s && s.id && s.turns.length);
    return JSON.stringify([req, hasContext ? s.id : null]);
  }

  function invalidateEstimate() {
    if (C.estimate) C.staleEstimate = true;
    C.estimate = null;
    C.estimateKey = null;
    $("estimate").hidden = true;
    renderPrivacy();
    renderStrip();
    updateButtons();
  }

  function updateButtons() {
    const box = $("task");
    const text = box.value;
    const uploading = C.uploading > 0;
    const hint = $("action-hint");
    $("attach-btn").disabled = C.busy;

    if (C.mode === "chat") {
      const model = selectedChatModel();
      const waiting = C.chatSending || pendingChat();
      $("send-btn").disabled = uploading || waiting || !text.trim() || !model;
      $("jury-btn").disabled = C.busy;
      if (waiting) hint.textContent = "Waiting for the reply\u2026";
      else if (uploading) hint.textContent = "Uploading files\u2026";
      else if (!model) hint.textContent = "No model available: add an API key or sign in to a CLI in Settings.";
      else if (!text.trim()) hint.textContent = "Write a message" + (keyboardHints() ? ". Enter sends, Shift+Enter adds a line." : ".");
      else hint.textContent = keyboardHints() ? "Enter sends to " + providerLabel(model.provider) + ". Shift+Enter adds a line." : "Ready.";
      return;
    }

    const req = currentRequest();
    const ready = req.task.trim() !== "" && req.selections.length > 0;
    const consentNeeded = needsConsent(req);
    $("consent-box").hidden = !consentNeeded;
    const consented = !consentNeeded || $("consent").checked;
    $("estimate-btn").disabled = C.busy || uploading || !ready;
    const estimateValid = C.estimate && C.estimateKey === estimateKey(req);
    const runBtn = $("run-btn");
    runBtn.disabled = C.busy || uploading || !estimateValid || C.estimate.budget.refused || !consented;
    const n = req.selections.length;
    if (!C.busy) runBtn.textContent = n > 1 ? "Ask " + n + " models" : (n === 1 ? "Ask 1 model" : "Ask models");
    if (C.busy) hint.textContent = "Waiting for the answers\u2026";
    else if (uploading) hint.textContent = "Uploading files\u2026";
    else if (req.task.trim() === "" && n === 0) hint.textContent = "Write a question and pick at least one model.";
    else if (req.task.trim() === "") hint.textContent = "Write a question first.";
    else if (n === 0) hint.textContent = "Pick at least one model.";
    else if (!estimateValid) hint.textContent = "Estimate the cost first" + (keyboardHints() ? " (Enter)." : ".");
    else if (C.estimate.budget.refused) hint.textContent = "The budget refuses this run. See the estimate for why.";
    else if (!consented) hint.textContent = "Tick the subscription consent box under Models.";
    else hint.textContent = keyboardHints() ? "Ready. Enter asks the models." : "Ready.";
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
            : (a.mode === "native" ? "native"
              : a.mode === "text+images" ? "text + " + derivedImagesText(a) : "as text") +
              ", \u2248 " + tokens(a.tokens) +
              (a.vendor_counted ? " (counted by the vendor)" : "") }),
        ]))),
      ])));
    }
    const s = CJ.sessions.current();
    if (s && s.id && s.turns.length) {
      box.append(noteLine("info", "Each model also receives a compact summary of this conversation as context; the estimate includes it."));
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
    C.busy = true;
    const btn = $("estimate-btn");
    btn.replaceChildren(el("span", { class: "spinner" }), "Estimating\u2026");
    updateButtons();
    try {
      const est = await api("/api/estimate" + sessionQueryIfContext(), req);
      C.estimate = est;
      C.estimateKey = estimateKey(req);
      C.staleEstimate = false;
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
      C.busy = false;
      btn.textContent = "Estimate cost";
      renderStrip();
      updateButtons();
    }
  }

  function sessionQueryIfContext() {
    const s = CJ.sessions.current();
    return s && s.id && s.turns.length ? CJ.query({ session: s.id }) : "";
  }

  async function onRun() {
    const req = currentRequest();
    if (!C.estimate || C.estimateKey !== estimateKey(req)) return;
    const b = C.estimate.budget;
    let confirmed = false;
    if (b.needs_confirmation) {
      confirmed = await confirmDialog(b, "Run anyway?");
      if (!confirmed) return;
    }
    const consent = needsConsent(req) && $("consent").checked;
    if (needsConsent(req) && !consent) return;
    showMessage("");
    C.busy = true;
    const runBtn = $("run-btn");
    runBtn.replaceChildren(el("span", { class: "spinner" }), "Asking\u2026");
    renderAttachments();
    updateButtons();
    try {
      await CJ.sessions.ensureSession();
    } catch (error) {
      C.busy = false;
      runBtn.textContent = "Ask models";
      updateButtons();
      showMessage(error.message);
      return;
    }
    const tid = randomId();
    const turn = {
      kind: "jury", id: tid, created_at: new Date().toISOString(), status: "pending",
      task: req.task, attachment_ids: req.attachment_ids, selections: req.selections,
      case_file: null, step1: null, synthesis: {}, synthesis_settings: null, error: null,
    };
    CJ.sessions.addTurn(turn);
    const saved = snapshotComposer();
    clearAfterSend();
    // The thread shows the new turn; the composer goes back to chat for follow-ups.
    if (chatAvailable()) setMode("chat");
    let body = Object.assign({}, req, { confirmed: confirmed, allow_unknown_cost: consent });
    try {
      for (let attempt = 0; attempt < 2; attempt++) {
        try {
          const result = await api("/api/run" + CJ.sessions.params(tid), body);
          const cur = CJ.sessions.turnById(tid) || turn;
          CJ.sessions.replaceTurn(Object.assign({}, cur, { status: "done", step1: result, error: null }), true);
          const plan = result.members.filter((m) => m.backend === "cli" && m.error === null && m.cost === null).length;
          state.planCalls += result.members.filter((m) => m.backend === "cli").length;
          setLastCost("Step 1 answers" + (plan ? " + " + plural(plan, "plan call") : ""), money(result.currency, result.total_cost));
          // The server's copy of the turn also has what only it knows (the case file).
          CJ.sessions.refresh();
          break;
        } catch (error) {
          if (error.status !== 409) {
            const cur = CJ.sessions.turnById(tid) || turn;
            CJ.sessions.replaceTurn(Object.assign({}, cur, { status: "error", error: error.message }), true);
            break;
          }
          // Nothing was written: take the turn back, give the question back, and ask
          // for what is missing (or say why it cannot run).
          CJ.sessions.removeTurn(tid);
          const again = attempt === 0 ? await resolveConflict(error, body, "Run anyway?") : null;
          if (!again) {
            setMode("jury");
            restoreComposer(saved);
            if (attempt > 0) showMessage(error.message);
            break;
          }
          body = again;
          CJ.sessions.addTurn(turn);
        }
      }
    } finally {
      C.busy = false;
      runBtn.textContent = "Ask models";
      renderAttachments();
      $("consent").checked = false;  // consent is given per run
      invalidateEstimate();  // spend changed: a new run needs a fresh estimate
      C.staleEstimate = false;
      renderStrip();
      refreshBudget();
    }
  }

  // ------------------------------------------------------------------ composer box

  function autoGrow() {
    const box = $("task");
    box.style.height = "auto";
    const max = Math.max(160, Math.round(window.innerHeight * 0.4));
    box.style.height = Math.min(box.scrollHeight + 2, max) + "px";
    box.style.overflowY = box.scrollHeight + 2 > max ? "auto" : "hidden";
  }

  // Enter sends (chat) or estimates, then asks (jury). Shift+Enter adds a line.
  function primaryAction() {
    if (C.mode === "chat") {
      if (!$("send-btn").disabled) sendChat();
      else updateButtons();
      return;
    }
    if (!$("run-btn").disabled) onRun();
    else if (!$("estimate-btn").disabled) onEstimate();
    else updateButtons();
  }

  function onKeyDown(event) {
    if (event.key !== "Enter" || event.shiftKey || event.altKey) return;
    if (event.isComposing || event.keyCode === 229) return;
    if (event.ctrlKey || event.metaKey) return;  // the page-wide shortcut handles it
    event.preventDefault();
    primaryAction();
  }

  // ------------------------------------------------------------------ hooks

  function onProvidersChanged() {
    buildChatOptions();
    renderPrivacy();
    updateButtons();
    scheduleChatEstimate(0);
  }

  function onAvailability() {
    $("jury-close").hidden = !chatAvailable();
    if (!chatAvailable()) setMode("jury");
  }

  function onSessionChanged() {
    C.continuation = null;
    C.chatEstimate = null;
    renderChip();
    const s = CJ.sessions.current();
    const empty = !s || !s.turns.length;
    setMode(empty || !chatAvailable() ? "jury" : "chat");
    invalidateEstimate();
    C.staleEstimate = false;
    renderStrip();
  }

  function init() {
    const box = $("task");
    box.addEventListener("input", () => {
      autoGrow();
      invalidateEstimate();
      scheduleChatEstimate(600);
    });
    box.addEventListener("keydown", onKeyDown);
    $("estimate-btn").addEventListener("click", onEstimate);
    $("run-btn").addEventListener("click", onRun);
    $("send-btn").addEventListener("click", () => sendChat());
    $("jury-btn").addEventListener("click", openJury);
    $("jury-close").addEventListener("click", () => { setMode("chat"); $("task").focus(); });
    $("consent").addEventListener("change", updateButtons);
    $("chat-model").addEventListener("change", onChatModelChange);
    window.addEventListener("resize", autoGrow, { passive: true });
    initAttachments();
    renderStrip();
    autoGrow();
    setMode("jury");
  }

  CJ.composer = {
    init: init,
    chatAvailable: chatAvailable,
    isBusy: () => C.busy || C.chatSending,
    invalidateEstimate: invalidateEstimate,
    updateButtons: updateButtons,
    renderStrip: renderStrip,
    setLastCost: setLastCost,
    refreshBudget: refreshBudget,
    primaryAction: primaryAction,
    continueFrom: continueFrom,
    retryChat: retryChat,
    prefill: prefill,
    onProvidersChanged: onProvidersChanged,
    onAvailability: onAvailability,
    onSessionChanged: onSessionChanged,
  };
})();
