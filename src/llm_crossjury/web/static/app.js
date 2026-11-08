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
    renderPending(req);
    try {
      const result = await api("/api/run", Object.assign({}, req, { confirmed: confirmed }));
      renderResults(result);
    } catch (error) {
      $("results-section").hidden = true;
      showMessage(error.message);
    } finally {
      state.busy = false;
      invalidateEstimate();  // spend changed: a new run needs a fresh estimate
      refreshBudget();
    }
  }

  // ------------------------------------------------------------------ init

  async function init() {
    $("task").addEventListener("input", invalidateEstimate);
    $("estimate-btn").addEventListener("click", onEstimate);
    $("run-btn").addEventListener("click", onRun);
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
