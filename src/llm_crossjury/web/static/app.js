// llm-crossjury compare UI. Vanilla JS, no build step.
// All server-provided text is inserted with textContent, never as HTML.
"use strict";

(function () {
  const state = {
    providers: [],
    defaultMaxTokens: 4096,
    budget: null,
    estimate: null,     // last estimate response
    estimateKey: null,  // JSON of the request the estimate belongs to
    busy: false,
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

  function renderProviders() {
    const box = $("providers");
    box.replaceChildren();
    for (const p of state.providers) box.append(providerCard(p));
  }

  function providerCard(p) {
    const keyMissing = p.key_status === "not set";
    const noModels = p.models.length === 0;
    const disabled = keyMissing || noModels;

    const checkbox = el("input", { type: "checkbox", id: "use-" + p.name, disabled: disabled, onchange: onSelectionChange });
    checkbox.dataset.provider = p.name;

    const badge = p.key_status === "not required"
      ? null
      : el("span", { class: "badge " + (keyMissing ? "badge-warn" : "badge-ok"), text: keyMissing ? "key not set" : "key set" });

    const select = el("select", { id: "model-" + p.name, disabled: disabled, "aria-label": p.label + " model", onchange: onSelectionChange });
    if (noModels) select.append(el("option", { text: "No usable models" }));
    for (const m of p.models) {
      select.append(el("option", { value: m.id, text: m.label, selected: m.id === p.default_model }));
    }

    const temperature = el("input", {
      id: "temp-" + p.name, type: "number", min: "0", step: "0.1", placeholder: "default",
      disabled: disabled, oninput: onSelectionChange,
    });
    const maxTokens = el("input", {
      id: "max-" + p.name, type: "number", min: "1", step: "1", value: String(state.defaultMaxTokens),
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
    } else if (noModels) {
      hint = el("p", { class: "hint", text: "No usable models: add a verified api entry with pricing to the model registry." });
    }

    return el("div", { class: "provider" + (disabled ? " disabled" : ""), id: "card-" + p.name }, [
      el("label", { class: "provider-head", for: "use-" + p.name }, [
        checkbox, el("span", { class: "provider-name", text: p.label }), badge,
      ]),
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
        model: $("model-" + p.name).value,
        temperature: temp === "" ? null : Number(temp),
        max_output_tokens: maxTokens === "" ? state.defaultMaxTokens : Number(maxTokens),
      });
    }
    return result;
  }

  function currentRequest() {
    return { task: $("task").value, selections: selections() };
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
    updateButtons();
  }

  function updateButtons() {
    const req = currentRequest();
    const ready = req.task.trim() !== "" && req.selections.length > 0;
    $("estimate-btn").disabled = state.busy || !ready;
    const estimateValid = state.estimate && state.estimateKey === JSON.stringify(req);
    $("run-btn").disabled = state.busy || !estimateValid || state.estimate.budget.refused;
    const hint = $("action-hint");
    if (!ready) hint.textContent = "Enter a task and pick at least one model.";
    else if (!estimateValid) hint.textContent = "Estimate the cost before running.";
    else if (state.estimate.budget.refused) hint.textContent = "The budget refuses this run.";
    else hint.textContent = "";
  }

  function renderEstimate(est) {
    const b = est.budget;
    const box = $("estimate");
    box.replaceChildren(
      el("div", { class: "estimate-range" }, [
        "Estimated cost: " + money(est.currency, est.total_low) + " \u2013 " + money(est.currency, est.total_high),
      ]),
      el("ul", { class: "estimate-lines" }, est.lines.map((line) =>
        el("li", { text: providerLabel(line.provider) + " \u00b7 " + line.model + ": " +
          money(est.currency, line.cost_low) + " \u2013 " + money(est.currency, line.cost_high) }))),
      el("p", { class: "estimate-note muted", text:
        "Budget: run limit " + money(b.currency, b.run_limit) +
        ", confirm above " + money(b.currency, b.confirm_above) +
        ", today " + money(b.currency, b.daily_remaining) + " left of " + money(b.currency, b.daily_limit) + "." }),
    );
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

  function renderPending(req) {
    $("results").replaceChildren(...req.selections.map((s) =>
      el("article", { class: "result" }, [
        el("div", { class: "result-head" }, [
          el("div", { class: "result-provider", text: providerLabel(s.provider) }),
          el("div", { class: "result-model", text: s.model }),
        ]),
        el("div", { class: "result-body pending" }, [el("span", { class: "spinner" }), "Waiting for the answer..."]),
      ])));
    $("run-total").textContent = "";
    $("results-section").hidden = false;
  }

  function renderResults(result) {
    $("results").replaceChildren(...result.members.map((m) => {
      const meta = [];
      if (m.usage) {
        meta.push("in " + tokens(m.usage.input_tokens) + " tok");
        meta.push("out " + tokens(m.usage.output_tokens) + " tok");
        if (m.usage.reasoning_tokens) meta.push("reasoning " + tokens(m.usage.reasoning_tokens) + " tok");
        if (m.usage.cached_input_tokens) meta.push("cached " + tokens(m.usage.cached_input_tokens) + " tok");
      }
      if (m.error === null) meta.push(money(m.currency, m.cost));
      if (m.latency_s !== null && m.latency_s !== undefined) meta.push(Number(m.latency_s).toFixed(1) + " s");
      return el("article", { class: "result" }, [
        el("div", { class: "result-head" }, [
          el("div", { class: "result-provider", text: providerLabel(m.provider) }),
          el("div", { class: "result-model", text: m.model }),
          meta.length ? el("div", { class: "result-meta" }, meta.map((t) => el("span", { text: t }))) : null,
        ]),
        m.error !== null
          ? el("div", { class: "result-error", text: "Error: " + m.error })
          : el("div", { class: "result-body", text: m.text }),
      ]);
    }));
    let total = "Total " + money(result.currency, result.total_cost);
    if (result.unknown_cost_members) total += " (+ " + result.unknown_cost_members + " with unknown cost)";
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
    showMessage("");
    state.busy = true;
    updateButtons();
    resetSynthesis();
    renderPending(req);
    try {
      const result = await api("/api/run", Object.assign({}, req, { confirmed: confirmed }));
      renderResults(result);
      setupSynthesis(req.task, result);
    } catch (error) {
      $("results-section").hidden = true;
      showMessage(error.message);
    } finally {
      state.busy = false;
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
    material: null,      // { task, answers: [{provider, model, text}] }
    runSettings: null,   // settings snapshot of the current synthesis
    seed: null,
    chair: null,         // { provider, model_id }
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

  function modelText(choice) {
    return providerLabel(choice.provider) + " \u00b7 " + choice.model_id;
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
      review_tier: tier ? tier.value : "economy",
      extra_instructions: extra.trim() === "" ? null : extra,
    };
  }

  function setSynBusy(busy) {
    syn.busy = busy;
    for (const id of ["syn-instructions", "syn-chair"]) $(id).disabled = busy;
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
    synMessage("");
    synStatus("");
  }

  function setupSynthesis(task, result) {
    resetSynthesis();
    const answers = result.members
      .filter((m) => m.error === null && typeof m.text === "string" && m.text.trim() !== "")
      .map((m) => ({ provider: m.provider, model: m.model, text: m.text }));
    $("synthesis-section").hidden = false;
    const enough = answers.length >= 2;
    $("syn-unavailable").hidden = enough;
    $("syn-form").hidden = !enough;
    if (!enough) {
      $("syn-unavailable").textContent = "AI synthesis needs at least two successful answers.";
      return;
    }
    syn.material = { task: task, answers: answers };
    syn.seed = null;
    syn.chair = null;
    syn.reveal = false;
    buildChairOptions(answers);
    scheduleSynEstimate(0);
  }

  function buildChairOptions(answers) {
    const options = [];
    const seen = new Set();
    for (const a of answers) {
      const key = a.provider + "/" + a.model;
      if (seen.has(key)) continue;
      seen.add(key);
      options.push({ provider: a.provider, model_id: a.model, group: "Models above" });
    }
    for (const p of state.providers) {
      if (p.key_status === "not set") continue;
      for (const m of p.models) {
        const key = p.name + "/" + m.id;
        if (seen.has(key)) continue;
        seen.add(key);
        options.push({ provider: p.name, model_id: m.id, group: "Other models" });
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
      groups[o.group].append(el("option", { value: String(index), text: modelText(o) }));
    }
  }

  function selectChair(choice) {
    const index = syn.chairOptions.findIndex((o) => o.provider === choice.provider && o.model_id === choice.model_id);
    if (index >= 0) $("syn-chair").value = String(index);
  }

  function onChairChange() {
    const o = syn.chairOptions[Number($("syn-chair").value)];
    syn.chair = o ? { provider: o.provider, model_id: o.model_id } : null;
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
        selectChair(est.chair);
      }
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
    if (stage === "agreement") return "judge " + modelText(est.judge);
    if (stage === "chair") return "chair " + modelText(est.chair);
    const models = est.reviewers.map((r) => r.model.model_id);
    return est.reviewers.length + " reviewer(s): " + Array.from(new Set(models)).join(", ");
  }

  function renderSynBreakdown(est) {
    const body = $("syn-breakdown-body");
    body.replaceChildren(
      el("table", { class: "cost-table" }, [
        el("thead", {}, el("tr", {}, [el("th", { text: "Stage" }), el("th", { text: "Models" }), el("th", { class: "num", text: "Estimate" })])),
        el("tbody", {}, est.stages.map((s) => el("tr", {}, [
          el("td", { text: STAGE_TITLES[s.stage] || s.stage }),
          el("td", { text: stageDescription(s.stage, est) }),
          el("td", { class: "num", text: moneyRange(est.currency, s.estimate.total_low, s.estimate.total_high) }),
        ]))),
        el("tfoot", {}, el("tr", {}, [
          el("td", { text: "Total" }), el("td", {}),
          el("td", { class: "num", text: moneyRange(est.currency, est.total_low, est.total_high) }),
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
    if (est) label += " (\u2248 " + moneyRange(est.currency, est.total_low, est.total_high) + ")";
    btn.textContent = label;
    btn.disabled = syn.busy || !syn.material || !est || est.budget.refused;
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
    const saving = shortMoney(step.review_estimate.currency || syn.currency, step.review_estimate.total_high);
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
          el("span", { text: "Chair " + modelText(syn.runSettings.chair) }),
          el("span", { text: "Cost " + shortMoney(syn.currency, total) }),
        ]),
      ]),
      renderMarkdown(synthesis.final_text),
    ]);

    const side = el("div", { class: "synth-side" }, [
      renderRanking(),
      renderCosts(total),
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

  function renderCosts(total) {
    return el("div", { class: "side-block" }, [
      el("h4", { text: "Cost" }),
      el("table", { class: "cost-table" }, [
        el("tbody", {}, syn.costs.map((c) => el("tr", {}, [
          el("td", { text: (STAGE_TITLES[c.stage] || c.stage) + (c.resynthesis ? " (regenerated)" : "") }),
          el("td", { class: "num muted", text: c.calls + " call(s)" }),
          el("td", { class: "num", text: shortMoney(syn.currency, c.cost) + (c.unknown_cost_calls ? " +?" : "") }),
        ]))),
        el("tfoot", {}, el("tr", {}, [el("td", { text: "Total" }), el("td", {}), el("td", { class: "num", text: shortMoney(syn.currency, total) })])),
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
    btn.textContent = "Regenerate final answer" +
      (est ? " (\u2248 " + shortMoney(est.estimate.currency || syn.currency, est.estimate.total_high) + ")" : "");
    const rulings = currentRulings();
    btn.disabled = syn.busy || !est || est.budget.refused || rulings.length === 0;
    const hint = $("resynth-hint");
    if (!hint) return;
    if (est && est.budget.refused) hint.textContent = "Refused by the budget: " + (est.budget.refusal_reason || "over budget");
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
    $("syn-instructions").addEventListener("input", () => scheduleSynEstimate(500));
    for (const radio of document.querySelectorAll("input[name=syn-tier]")) {
      radio.addEventListener("change", () => scheduleSynEstimate(0));
    }
  }

  // ------------------------------------------------------------------ init

  async function init() {
    $("task").addEventListener("input", invalidateEstimate);
    $("estimate-btn").addEventListener("click", onEstimate);
    $("run-btn").addEventListener("click", onRun);
    initSynthesis();
    try {
      const data = await api("/api/providers");
      state.providers = data.providers;
      state.defaultMaxTokens = data.default_max_output_tokens;
      state.budget = data.budget;
      renderBudgetSummary();
      renderProviders();
    } catch (error) {
      $("providers").replaceChildren(el("p", { class: "muted", text: "Could not load providers." }));
      showMessage(error.message);
    }
    updateButtons();
  }

  init();
})();
