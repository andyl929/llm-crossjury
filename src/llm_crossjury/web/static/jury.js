// llm-crossjury home page: a jury turn in the thread. Step 1 (the members' answers
// side by side) and step 2 (agreement, anonymous review, chair) for one turn.
//
// Each card owns its step-2 state, so several jury turns can sit in one thread and
// each step-2 action works on its own turn. Step 2 is stateless on the server: every
// request posts the step-1 material, the label seed and the settings back, plus
// earlier stage outputs where needed. With sessions, requests carry
// ?session=S&turn=T and the server stores each stage result in the turn, so a
// reopened turn shows (and can continue) its step 2.
"use strict";

(function () {
  const CJ = window.CJ;
  const {
    el, icon, iconButton, money, moneyRange, tokens, plural, planUsage, cliCalls, hasPriced, costText,
    api, fillMessage, toast, copyText, renderMarkdown, providerLabel, deliveryList, fixFor, scrollBehavior,
  } = CJ;
  const state = CJ.state;

  const STAGE_TITLES = { agreement: "Agreement", review: "Review", chair: "Chair" };

  // One timer for every "waiting" line on the page (jury members and chat replies).
  setInterval(() => {
    for (const node of document.querySelectorAll(".elapsed[data-since]")) {
      const since = Date.parse(node.dataset.since);
      if (Number.isNaN(since)) continue;
      const secs = Math.max(0, Math.round((Date.now() - since) / 1000));
      node.textContent = (node.dataset.prefix || "Waiting for the answer") + ", " + secs + " s";
    }
  }, 1000);

  function modelText(choice, backend) {
    if (!choice) return "automatic";
    return providerLabel(choice.provider) + ", " + choice.model_id + (backend === "cli" ? " (subscription)" : "");
  }

  // Cost text of a CostEstimate: priced range and/or subscription call count.
  function estimateText(currency, est) {
    return costText(moneyRange(currency, est.total_low, est.total_high), hasPriced(est.lines), cliCalls(est.lines));
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

  function create(initialTurn) {
    let turn = initialTurn;
    const tid = turn.id;
    const sfx = tid.slice(0, 12);

    // ---------------------------------------------------------------- skeleton

    const question = el("div", { class: "turn-user" });
    const total = el("div", { class: "run-total" });
    const answers = el("div", { class: "answers" });
    const stepError = el("div", { class: "message", role: "alert", hidden: true });
    const syncBox = el("input", { type: "checkbox" });
    let syncScroll = false;
    syncBox.addEventListener("change", () => { syncScroll = syncBox.checked; });
    const tools = el("div", { class: "toolbar jury-tools", hidden: true }, [
      el("label", { class: "switch" }, [syncBox, el("span", { text: "Scroll together" })]),
      el("button", { type: "button", class: "btn btn-small", onclick: exportTurn }, [icon("download"), "Export"]),
    ]);
    const head = el("div", { class: "jury-head" }, [
      el("h2", { class: "jury-title" }, [el("span", { class: "step", text: "Step 1" }), " Answers"]),
      tools,
    ]);
    const card = el("article", { class: "jury-card", "aria-label": "Jury answers" }, [head, total, stepError, answers]);
    const node = el("section", { class: "turn turn-jury", id: "turn-" + tid }, [question, card]);

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
      estimated: false,
      busy: false,         // this page is running a step-2 request for the turn
      remote: false,       // the server is running one (seen through polling)
      runId: null,
      labels: [],
      agreement: null,
      reviews: null,
      reviewSkipped: false,
      outcome: null,       // ChairOutcome
      outcomeKey: null,
      costs: [],           // [{ stage, calls, cost, unknown_cost_calls, resynthesis }]
      currency: "USD",
      reveal: false,
      choices: { dispute: {}, unique: {} },  // rulings picked in the additional list
      resynthEstimate: null,
      resynthSeq: 0,
      resynthTimer: null,
      saveTimer: null,
    };
    let step1Key = null;
    let synKey = null;
    let ui = null;  // step-2 DOM, built once step 1 is done
    let observer = null;

    // ---------------------------------------------------------------- question

    function renderQuestion() {
      const n = (turn.selections || []).length || (turn.step1 ? turn.step1.members.length : 0);
      const files = (turn.attachment_ids || []).length;
      question.replaceChildren(
        el("div", { class: "bubble bubble-user" }, [
          el("div", { class: "bubble-label" }, [icon("mark"), "Asked the jury" + (n ? " (" + plural(n, "model") + ")" : "")]),
          el("div", { class: "bubble-text", text: turn.task }),
          files ? el("div", { class: "bubble-files" }, [icon("clip"), plural(files, "attached file")]) : null,
          turn.case_file ? el("div", { class: "bubble-files" }, [icon("info"), "Sent with the conversation so far as context"]) : null,
        ]),
      );
    }

    // ---------------------------------------------------------------- step 1

    function renderStep1() {
      if (turn.status === "pending") {
        renderPending();
      } else if (turn.status === "error" || !turn.step1) {
        renderStep1Error();
      } else {
        renderResults(turn.step1);
      }
    }

    function renderPending() {
      const sels = turn.selections || [];
      answers.replaceChildren(...sels.map((s) =>
        el("article", { class: "answer", "aria-busy": "true" }, [
          answerHead(s.provider, s.model, s.backend, null, null),
          el("div", { class: "answer-pending pending" }, [
            el("div", { class: "pending-line" }, [el("span", { class: "spinner" }),
              el("span", { class: "elapsed", "data-since": turn.created_at || new Date().toISOString(), text: "Waiting for the answer" })]),
            el("div", { class: "skeleton" }), el("div", { class: "skeleton" }), el("div", { class: "skeleton" }), el("div", { class: "skeleton" }),
          ]),
        ])));
      if (!sels.length) answers.append(el("p", { class: "status" }, [el("span", { class: "spinner" }), "The models are answering\u2026"]));
      total.textContent = "";
      tools.hidden = true;
      stepError.hidden = true;
    }

    function renderStep1Error() {
      answers.replaceChildren();
      total.textContent = "";
      tools.hidden = true;
      const text = turn.error || "This question did not get answers.";
      fillMessage(stepError, text);
      stepError.querySelector(".message-body").append(el("div", { class: "actions" }, [
        el("button", { type: "button", class: "btn btn-small", text: "Ask again", onclick: () => CJ.composer.prefill(turn.task, "jury") }),
      ]));
    }

    function answerTools(article, m, index) {
      const label = providerLabel(m.provider);
      const wide = iconButton("expand", "Show " + label + " full width", null, { "aria-pressed": "false" });
      const collapse = iconButton("chevron", "Collapse " + label, null, { "aria-expanded": "true" });
      wide.addEventListener("click", () => {
        const on = !article.classList.contains("is-wide");
        for (const other of answers.querySelectorAll(".answer.is-wide")) {
          if (other !== article) other.querySelector("[aria-pressed]").click();
        }
        article.classList.toggle("is-wide", on);
        wide.setAttribute("aria-pressed", String(on));
        wide.replaceChildren(icon(on ? "shrink" : "expand"));
        const text = (on ? "Back to columns: " : "Show full width: ") + label;
        wide.setAttribute("aria-label", text);
        wide.title = text;
        if (on) article.scrollIntoView({ block: "nearest", behavior: scrollBehavior() });
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
      const list = [];
      if (m.error === null) list.push(iconButton("copy", "Copy " + label + " answer", () => copyText(m.text, label + " answer")));
      list.push(wide, collapse);
      void index;
      return el("div", { class: "answer-tools" }, list);
    }

    function renderResults(result) {
      const canChat = CJ.composer.chatAvailable();
      answers.replaceChildren(...result.members.map((m, index) => {
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
        article.append(answerHead(m.provider, m.model, m.backend, memberMeta(m), answerTools(article, m, index)));
        if (delivery) article.append(delivery);
        article.append(body);
        if (canChat && m.error === null) {
          article.append(el("div", { class: "answer-foot" }, el("button", {
            type: "button", class: "btn btn-small btn-continue",
            onclick: () => CJ.composer.continueFrom({
              model: { provider: m.provider, backend: m.backend, model: m.model },
              continued_from: { turn_id: tid, source: "member", member_index: index },
              label: providerLabel(m.provider) + "'s answer",
            }),
          }, [icon("chat"), "Continue with " + providerLabel(m.provider)])));
        }
        return article;
      }));
      const planCalls = result.members.filter((m) => m.backend === "cli" && m.error === null && m.cost === null).length;
      const otherUnknown = result.unknown_cost_members - planCalls;
      let text = "Total " + money(result.currency, result.total_cost);
      if (planCalls) text += " + " + planUsage(planCalls);
      if (otherUnknown > 0) text += " (+ " + otherUnknown + " with unknown cost)";
      const ok = result.members.filter((m) => m.error === null).length;
      total.textContent = plural(ok, "answer") + " of " + result.members.length + ". " + text + ".";
      tools.hidden = false;
      stepError.hidden = true;
      wireSyncScroll();
    }

    // Synchronized scrolling: scrolling one answer moves the others to the same
    // relative position. Off by default.
    let syncing = false;
    function wireSyncScroll() {
      for (const body of answers.querySelectorAll(".answer-body")) {
        body.addEventListener("scroll", () => {
          if (!syncScroll || syncing) return;
          syncing = true;
          const range = body.scrollHeight - body.clientHeight;
          const ratio = range > 0 ? body.scrollTop / range : 0;
          for (const other of answers.querySelectorAll(".answer-body")) {
            if (other !== body) other.scrollTop = ratio * (other.scrollHeight - other.clientHeight);
          }
          requestAnimationFrame(() => { syncing = false; });
        }, { passive: true });
      }
    }

    // ---------------------------------------------------------------- step 2 DOM

    function buildSynthesisUi() {
      const ids = {
        heading: "syn-h-" + sfx, instructions: "syn-i-" + sfx, chair: "syn-c-" + sfx, tier: "syn-tier-" + sfx,
      };
      const r = {};
      r.unavailable = el("p", { class: "empty-inline", hidden: true });
      r.attachNote = el("p", { class: "note", hidden: true });
      r.instructions = el("textarea", { id: ids.instructions, rows: "3", spellcheck: "true",
        placeholder: "Comparison criteria, or a follow-up question for the reviewers and the chair." });
      r.chair = el("select", { id: ids.chair });
      r.tiers = [
        el("input", { type: "radio", name: ids.tier, value: "economy", checked: true }),
        el("input", { type: "radio", name: ids.tier, value: "standard" }),
      ];
      r.consent = el("input", { type: "checkbox" });
      r.consentBox = el("label", { class: "consent", hidden: true }, [r.consent, el("span", { text: state.consentText })]);
      r.btn = el("button", { type: "button", class: "btn btn-primary syn-btn", disabled: true, text: "AI synthesize" });
      r.status = el("span", { class: "status", "aria-live": "polite" });
      r.breakdownBody = el("div");
      r.breakdown = el("details", { class: "breakdown", hidden: true }, [el("summary", { text: "Cost breakdown" }), r.breakdownBody]);
      r.form = el("div", { class: "synth-form" }, [
        r.attachNote,
        el("div", { class: "synth-fields" }, [
          el("div", { class: "field field-wide" }, [
            el("label", { for: ids.instructions, class: "field-label" }, ["Extra instructions ", el("span", { class: "optional", text: "optional" })]),
            r.instructions,
          ]),
          el("div", { class: "field" }, [el("label", { for: ids.chair, class: "field-label", text: "Chair model" }), r.chair]),
          el("fieldset", { class: "field tier" }, [
            el("legend", { class: "field-label", text: "Review tier" }),
            el("label", { class: "radio" }, [r.tiers[0], " Economy ", el("span", { class: "help", text: "fast models" })]),
            el("label", { class: "radio" }, [r.tiers[1], " Standard ", el("span", { class: "help", text: "the models above" })]),
          ]),
        ]),
        r.consentBox,
        el("div", { class: "actions" }, [r.btn, r.status]),
        r.breakdown,
      ]);
      r.progress = el("ol", { class: "stepper", "aria-label": "Synthesis progress", hidden: true });
      r.message = el("div", { class: "message syn-message", role: "alert", hidden: true });
      r.decision = el("div", { class: "decision", hidden: true });
      r.result = el("div", { class: "synth-result", hidden: true });
      r.section = el("section", { class: "panel synthesis", "aria-labelledby": ids.heading }, [
        el("div", { class: "panel-head synth-head" }, [
          el("h3", { id: ids.heading, class: "panel-title" }, [el("span", { class: "step", text: "Step 2" }), " Synthesis"]),
          el("p", { class: "help", text: "Measure agreement, let the models review each other anonymously, and have a chair model write one final answer." }),
        ]),
        r.unavailable, r.form, r.progress, r.message, r.decision, r.result,
      ]);

      r.btn.addEventListener("click", onSynthesize);
      r.chair.addEventListener("change", () => { onChairChange(); saveFormSoon(); });
      r.consent.addEventListener("change", () => {
        updateSynButton();
        updateResynthButton();
        if (!syn.busy && !syn.remote) synStatus("");
      });
      r.instructions.addEventListener("input", () => { scheduleSynEstimate(500); saveFormSoon(); });
      for (const radio of r.tiers) radio.addEventListener("change", () => { scheduleSynEstimate(0); saveFormSoon(); });
      card.append(r.section);
      ui = r;

      // Estimate when the panel comes into view, so a long thread does not estimate
      // every old turn on load.
      if ("IntersectionObserver" in window) {
        observer = new IntersectionObserver((entries) => {
          if (entries.some((e) => e.isIntersecting)) {
            observer.disconnect();
            observer = null;
            if (!syn.estimated) scheduleSynEstimate(0);
          }
        });
        observer.observe(r.section);
      } else {
        scheduleSynEstimate(0);
      }
    }

    function synConsented() {
      return !!(ui && ui.consent.checked);
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
      fillMessage(ui.message, text);
    }

    function synStatus(text, spinning) {
      ui.status.replaceChildren();
      if (spinning) ui.status.append(el("span", { class: "spinner" }));
      if (text) ui.status.append(text);
    }

    // Progress through agreement, review and chair. states: stage -> "done" | "active" | "skipped" | "".
    function renderProgress(states) {
      const box = ui.progress;
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

    function currentTier() {
      const on = ui.tiers.find((r) => r.checked);
      return on ? on.value : "economy";
    }

    function synSettings() {
      const extra = ui.instructions.value;
      return {
        task: syn.material.task,
        answers: syn.material.answers,
        seed: syn.seed,
        chair: syn.chair,
        chair_backend: syn.chair ? syn.chairBackend : null,
        review_tier: currentTier(),
        extra_instructions: extra.trim() === "" ? null : extra,
        allow_unknown_cost: synConsented(),
        attachment_ids: syn.material.attachment_ids,
      };
    }

    function locked() {
      return syn.busy || syn.remote;
    }

    function applyLock() {
      const busy = locked();
      ui.instructions.disabled = busy;
      ui.chair.disabled = busy;
      ui.consent.disabled = busy;
      for (const radio of ui.tiers) radio.disabled = busy;
      for (const n of ui.result.querySelectorAll("input, button")) {
        if (!n.classList.contains("syn-reveal") && !n.classList.contains("icon-btn") && !n.classList.contains("btn-continue")) n.disabled = busy;
      }
      updateSynButton();
      updateResynthButton();
    }

    function setSynBusy(busy) {
      syn.busy = busy;
      CJ.sessions.setBusy(tid, busy);
      applyLock();
    }

    // ---------------------------------------------------------------- step 2 state

    function setupSynthesis() {
      if (syn.material) return;
      const ids = turn.attachment_ids || [];
      const list = turn.step1.members
        .filter((m) => m.error === null && typeof m.text === "string" && m.text.trim() !== "")
        .map((m) => ({ provider: m.provider, backend: m.backend, model: m.model, text: m.text }));
      if (!ui) buildSynthesisUi();
      const enough = list.length >= 2;
      ui.unavailable.hidden = enough;
      ui.form.hidden = !enough;
      if (!enough) {
        ui.unavailable.textContent = list.length === 1
          ? "Synthesis needs at least two successful answers. Only one model answered: pick another model and ask again."
          : "Synthesis needs at least two successful answers. Fix the errors above and ask again.";
        if (observer) { observer.disconnect(); observer = null; }
        return;
      }
      syn.material = { task: turn.task, answers: list, attachment_ids: ids };
      ui.attachNote.textContent = ids.length
        ? "Only the chair model receives the " + plural(ids.length, "attached file") +
          "; the agreement judge and the reviewers see the answers only."
        : "";
      ui.attachNote.hidden = !ids.length;
      buildChairOptions(list);
    }

    function buildChairOptions(list) {
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
      for (const a of list) {
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
      ui.chair.replaceChildren();
      const groups = {};
      for (const [index, o] of options.entries()) {
        if (!groups[o.group]) {
          groups[o.group] = el("optgroup", { label: o.group });
          ui.chair.append(groups[o.group]);
        }
        groups[o.group].append(el("option", { value: String(index), text: modelText(o, o.backend) }));
      }
    }

    function selectChair(choice, backend) {
      if (!choice) return;
      const index = syn.chairOptions.findIndex((o) =>
        o.provider === choice.provider && o.model_id === choice.model_id && o.backend === backend);
      if (index >= 0) ui.chair.value = String(index);
    }

    function onChairChange() {
      const o = syn.chairOptions[Number(ui.chair.value)];
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
      syn.estimated = true;
      if (!locked()) synStatus("Estimating\u2026", true);
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
        ui.consentBox.hidden = !est.cli_calls;
        renderSynBreakdown(est);
        if (!locked()) {
          synStatus(est.cli_calls && !synConsented() ? "Tick the consent box to use subscription models." : "");
          if (!syn.outcome) synMessage(est.budget.refused ? "Refused by the budget: " + (est.budget.refusal_reason || "over budget") : "");
        }
      } catch (error) {
        if (seq !== syn.estimateSeq) return;
        syn.estimate = null;
        if (!locked()) { synStatus(""); synMessage(error.message); }
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
      ui.breakdownBody.replaceChildren(
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
      ui.breakdown.hidden = false;
    }

    function updateSynButton() {
      if (!ui) return;
      const btn = ui.btn;
      const est = syn.estimate;
      if (!locked()) {
        const verb = syn.outcome ? "Synthesize again" : "AI synthesize";
        btn.textContent = verb + (est ? " (\u2248 " + synTotalText(est) + ")" : "");
      }
      const consented = !est || !est.cli_calls || synConsented();
      btn.disabled = locked() || !syn.material || !est || est.budget.refused || !consented;
    }

    function addCosts(result, resynthesis) {
      if (!result || !result.stage_costs) return;
      if (result.currency) syn.currency = result.currency;
      for (const c of result.stage_costs) syn.costs.push(resynthesis ? Object.assign({}, c, { resynthesis: true }) : c);
    }

    function synCostTotals() {
      const sum = syn.costs.reduce((acc, c) => acc + Number(c.cost), 0);
      const unknown = syn.costs.reduce((acc, c) => acc + c.unknown_cost_calls, 0);
      return { total: sum, unknown: unknown };
    }

    function recordSynCost(label, result) {
      const cost = result.stage_costs.reduce((acc, c) => acc + Number(c.cost), 0);
      const unknown = result.stage_costs.reduce((acc, c) => acc + c.unknown_cost_calls, 0);
      state.planCalls += unknown;
      CJ.composer.setLastCost(label + (unknown ? " + " + plural(unknown, "plan call") : ""), money(result.currency, cost));
    }

    // ---------------------------------------------------------------- persistence

    // The page's step-2 settings, stored in the turn (opaque to the server) so a
    // reopened turn can show and continue them.
    function formValues() {
      return {
        instructions: ui.instructions.value,
        tier: currentTier(),
        chair: syn.chair ? { provider: syn.chair.provider, model_id: syn.chair.model_id, backend: syn.chairBackend } : null,
      };
    }

    function runSnapshot() {
      if (!syn.runSettings) return null;
      const copy = Object.assign({}, syn.runSettings);
      delete copy.task;
      delete copy.answers;
      delete copy.attachment_ids;
      delete copy.allow_unknown_cost;
      delete copy.confirmed;
      return copy;
    }

    function settingsObject(outcome) {
      const prev = turn.synthesis_settings || {};
      return {
        form: formValues(),
        run: runSnapshot(),
        outcome: outcome === undefined ? (prev.outcome === undefined ? null : prev.outcome) : outcome,
      };
    }

    function postSettings(settings) {
      if (!CJ.sessions.available()) return;
      const session = CJ.sessions.current().id;
      if (!session) return;
      api("/api/sessions/" + encodeURIComponent(session) + "/turns/" + tid + "/synthesis-settings", { settings: settings })
        .catch(() => { /* best effort: the page keeps working */ });
    }

    function saveFormSoon() {
      clearTimeout(syn.saveTimer);
      syn.saveTimer = setTimeout(() => {
        const settings = settingsObject();
        turn = Object.assign({}, turn, { synthesis_settings: settings });
        commit(false);
        postSettings(settings);
      }, 800);
    }

    // Store a local change of this turn in the session copy (and the thread).
    function commit(done) {
      synKey = synthesisKey(turn);
      CJ.sessions.replaceTurn(turn, done);
    }

    function storeStage(stage, result, outcome) {
      const synthesis = Object.assign({}, turn.synthesis || {});
      delete synthesis.pending;
      delete synthesis[stage + "_error"];
      if (result !== null) synthesis[stage] = result;
      const settings = settingsObject(outcome);
      turn = Object.assign({}, turn, { synthesis: synthesis, synthesis_settings: settings });
      commit(false);
      postSettings(settings);
    }

    function storeError(stage, message) {
      const synthesis = Object.assign({}, turn.synthesis || {});
      delete synthesis.pending;
      synthesis[stage + "_error"] = message;
      turn = Object.assign({}, turn, { synthesis: synthesis });
      commit(false);
    }

    function synthesisKey(t) {
      return JSON.stringify([t.synthesis || {}, t.synthesis_settings || null]);
    }

    function stageQuery() {
      return CJ.sessions.params(tid);
    }

    // ---------------------------------------------------------------- step 2 flow

    async function onSynthesize() {
      const est = syn.estimate;
      if (!est || locked()) return;
      let confirmed = false;
      if (est.budget.needs_confirmation) {
        confirmed = await CJ.askDialog({
          title: "Above your confirmation threshold",
          body: "The high estimate (" + money(est.currency, est.total_high) + ") is above your confirmation threshold (" +
            money(est.currency, est.budget.confirm_above) + "). Continue?",
          confirm: "Synthesize",
        });
        if (!confirmed) return;
      }
      synMessage("");
      syn.runSettings = synSettings();
      syn.costs = [];
      syn.outcome = null;
      syn.agreement = null;
      syn.reviews = null;
      ui.result.hidden = true;
      ui.decision.hidden = true;
      setSynBusy(true);
      ui.btn.replaceChildren(el("span", { class: "spinner" }), "Synthesizing\u2026");
      renderProgress({ agreement: "active" });
      synStatus("Measuring agreement\u2026");
      postSettings(settingsObject(null));
      try {
        const body = Object.assign({}, syn.runSettings, { confirmed: confirmed });
        await CJ.sessions.ensureSession();
        const step = await api("/api/synthesis/agreement" + stageQuery(), body);
        syn.runId = step.run_id;
        syn.seed = step.seed;
        syn.labels = step.labels;
        syn.agreement = step.agreement;
        addCosts(step);
        recordSynCost("Agreement", step);
        storeStage("agreement", step, null);
        if (step.suggest_skip) {
          synStatus("");
          renderProgress({ agreement: "done" });
          setSynBusy(false);
          showSkipDecision(step);
          return;
        }
        await finish(false);
      } catch (error) {
        synFailed(error, "agreement");
      }
    }

    function synFailed(error, stage) {
      synStatus("");
      renderProgress(null);
      synMessage(error.message);
      if (stage && error.status !== 409) storeError(stage, error.message);
      setSynBusy(false);
      CJ.composer.refreshBudget();
    }

    function showSkipDecision(step) {
      const ratio = step.agreement && step.agreement.ratio !== null && step.agreement.ratio !== undefined
        ? Math.round(step.agreement.ratio * 100) : null;
      const review = step.review_estimate;
      const saving = review
        ? costText(money(review.currency || syn.currency, review.total_high), hasPriced(review.lines), cliCalls(review.lines))
        : null;
      const suggest = !!step.suggest_skip;
      const skip = el("button", { type: "button", class: "btn " + (suggest ? "btn-primary" : ""),
        text: "Skip review" + (saving ? " (saves \u2248 " + saving + ")" : ""), onclick: () => decide(true) });
      const go = el("button", { type: "button", class: "btn " + (suggest ? "" : "btn-primary"),
        text: "Continue with review", onclick: () => decide(false) });
      ui.decision.replaceChildren(
        el("p", { class: "decision-title", text: suggest
          ? "The answers largely agree (agreement " + ratio + "%)."
          : "Agreement measured" + (ratio === null ? "." : " (" + ratio + "%).") + " The final answer is not written yet." }),
        el("p", { class: "help", text: suggest
          ? "A peer review may add little. You can skip it and let the chair write the final answer now."
          : "Continue to let the models review each other, then the chair writes the final answer." }),
        el("div", { class: "actions" }, suggest ? [skip, go] : [go, skip]),
      );
      ui.decision.hidden = false;
      if (document.activeElement === ui.btn || document.activeElement === document.body) (suggest ? skip : go).focus();
    }

    async function decide(skip) {
      if (locked()) return;
      ui.decision.hidden = true;
      synMessage("");
      if (!syn.runSettings) syn.runSettings = synSettings();
      setSynBusy(true);
      try {
        await finish(skip);
      } catch (error) {
        synFailed(error, "finish");
      }
    }

    async function finish(skip) {
      renderProgress(skip ? { agreement: "done", review: "skipped", chair: "active" } : { agreement: "done", review: "active" });
      synStatus(skip ? "Writing the final answer\u2026" : "Reviewing, then writing the final answer\u2026");
      const body = Object.assign({}, syn.runSettings, { seed: syn.seed, run_id: syn.runId, agreement: syn.agreement, skip_review: skip });
      const step = await api("/api/synthesis/finish" + stageQuery(), body);
      syn.reviews = step.reviews;
      syn.reviewSkipped = step.review_skipped;
      syn.outcome = step.chair;
      syn.choices = { dispute: {}, unique: {} };
      addCosts(step);
      recordSynCost("Synthesis", step);
      renderProgress({ agreement: "done", review: step.review_skipped ? "skipped" : "done", chair: "done" });
      synStatus("");
      storeStage("finish", step, "finish");
      setSynBusy(false);
      renderSynResult();
      CJ.sessions.replaceTurn(turn, true);
      CJ.composer.refreshBudget();
      const final = ui.result.querySelector(".final");
      if (final) final.scrollIntoView({ block: "start", behavior: scrollBehavior() });
    }

    // ---------------------------------------------------------------- step 2 results

    function chairText() {
      const rs = syn.runSettings;
      if (rs && rs.chair) return modelText(rs.chair, rs.chair_backend);
      if (syn.chair) return modelText(syn.chair, syn.chairBackend);
      return "automatic";
    }

    // The model that chaired, for "Continue with chair".
    function chairModel() {
      const rs = syn.runSettings;
      if (rs && rs.chair) return { provider: rs.chair.provider, backend: rs.chair_backend || "api", model: rs.chair.model_id };
      for (const key of ["resynthesize", "finish"]) {
        const result = turn.synthesis && turn.synthesis[key];
        const call = result && (result.calls || []).find((c) => c.stage === "chair");
        if (call) return { provider: call.provider, backend: call.backend, model: call.model };
      }
      if (syn.chair) return { provider: syn.chair.provider, backend: syn.chairBackend || "api", model: syn.chair.model_id };
      return null;
    }

    function renderSynResult() {
      const outcome = syn.outcome;
      if (!outcome) return;
      const synthesis = outcome.synthesis;
      const totals = synCostTotals();
      const totalText = money(syn.currency, totals.total) + (totals.unknown ? " + " + planUsage(totals.unknown) : "");
      const headingId = "final-h-" + sfx;
      const chair = chairModel();
      const finalTools = [iconButton("copy", "Copy final answer", () => copyText(String(synthesis.final_text || ""), "Final answer"))];
      const continueBtn = CJ.composer.chatAvailable() && chair
        ? el("button", { type: "button", class: "btn btn-small btn-continue", onclick: () => CJ.composer.continueFrom({
          model: chair, continued_from: { turn_id: tid, source: "synthesis" }, label: "the final answer",
        }) }, [icon("chat"), "Continue with chair"])
        : null;

      const final = el("article", { class: "final", "aria-labelledby": headingId }, [
        el("header", { class: "final-head" }, [
          el("div", {}, [
            el("h3", { id: headingId, text: "Final answer" }),
            el("dl", { class: "meta" }, [
              ["Chair", chairText()],
              ["Review", syn.reviewSkipped ? "skipped" : "done"],
              ["Cost", totalText],
            ].map((m) => el("div", {}, [el("dt", { text: m[0] }), el("dd", { text: m[1] })]))),
          ]),
          el("div", { class: "answer-tools" }, finalTools),
        ]),
        el("div", { class: "final-body" }, renderMarkdown(synthesis.final_text)),
        continueBtn ? el("div", { class: "answer-foot" }, continueBtn) : null,
      ]);

      const reveal = el("label", { class: "reveal" }, [
        el("input", { type: "checkbox", class: "syn-reveal", checked: syn.reveal, onchange: (e) => {
          syn.reveal = e.target.checked;
          renderSynResult();
          const again = ui.result.querySelector(".syn-reveal");
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

      ui.result.replaceChildren(
        el("div", { class: "synth-grid" }, [final, side]),
        renderAdditional(outcome.additional),
      );
      ui.result.hidden = false;
      applyLock();
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

    function renderCosts(sum, unknown) {
      return el("div", { class: "side-block" }, [
        el("h4", { text: "Cost" }),
        el("table", { class: "data-table" }, [
          el("tbody", {}, syn.costs.map((c) => el("tr", {}, [
            el("td", { text: (STAGE_TITLES[c.stage] || c.stage) + (c.resynthesis ? " (regenerated)" : "") }),
            el("td", { class: "num help", text: plural(c.calls, "call") }),
            costCell(c.cost, c.calls, c.unknown_cost_calls),
          ]))),
          el("tfoot", {}, el("tr", {}, [el("td", { text: "Total" }), el("td", {}), costCell(sum, -1, unknown)])),
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
        const name = "ruling-" + sfx + "-" + d.id;
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

      const headingId = "additional-h-" + sfx;
      const regen = el("div", { class: "regen" }, [
        el("button", { type: "button", class: "btn btn-primary resynth-btn", text: "Regenerate final answer", onclick: onResynthesize }),
        el("span", { class: "status resynth-hint", "aria-live": "polite" }),
      ]);
      return el("section", { class: "additional", "aria-labelledby": headingId }, [
        el("div", { class: "additional-head" }, [
          el("h3", { id: headingId, text: "Additional list" }),
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
      scheduleResynthEstimate(0);
    }

    function onUniqueChoice(index, include) {
      syn.choices.unique[index] = include;
      scheduleResynthEstimate(0);
    }

    function resynthBody() {
      const base = syn.runSettings || synSettings();
      return Object.assign({}, base, {
        task: syn.material.task,
        answers: syn.material.answers,
        attachment_ids: syn.material.attachment_ids,
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
      if (!currentRulings().length) return;
      syn.resynthTimer = setTimeout(async () => {
        const seq = ++syn.resynthSeq;
        try {
          const est = await api("/api/synthesis/resynthesize-estimate", resynthBody());
          if (seq !== syn.resynthSeq) return;
          syn.resynthEstimate = est;
        } catch (error) {
          if (seq !== syn.resynthSeq) return;
          syn.resynthEstimate = null;
          const hint = ui.result.querySelector(".resynth-hint");
          if (hint) hint.textContent = error.message;
          return;
        }
        updateResynthButton();
      }, delay);
    }

    function updateResynthButton() {
      if (!ui) return;
      const btn = ui.result.querySelector(".resynth-btn");
      if (!btn) return;
      const est = syn.resynthEstimate;
      const currency = est ? est.estimate.currency || syn.currency : syn.currency;
      if (!locked()) {
        btn.textContent = "Regenerate final answer" + (est
          ? " (\u2248 " + costText(money(currency, est.estimate.total_high), hasPriced(est.estimate.lines), est.budget.cli_calls) + ")"
          : "");
      }
      const rulings = currentRulings();
      const consented = !est || !est.budget.cli_calls || synConsented();
      btn.disabled = locked() || !est || est.budget.refused || rulings.length === 0 || !consented;
      const hint = ui.result.querySelector(".resynth-hint");
      if (!hint) return;
      if (est && est.budget.refused) hint.textContent = "Refused by the budget: " + (est.budget.refusal_reason || "over budget");
      else if (!consented) hint.textContent = "Tick the consent box above to use subscription models.";
      else if (rulings.length === 0) hint.textContent = "Pick a position or include an insight to regenerate.";
      else if (!est) hint.textContent = "Estimating\u2026";
      else hint.textContent = plural(rulings.length, "change") + " selected.";
    }

    async function onResynthesize() {
      const est = syn.resynthEstimate;
      if (!est || locked()) return;
      let confirmed = false;
      if (est.budget.needs_confirmation) {
        confirmed = await CJ.askDialog({
          title: "Above your confirmation threshold",
          body: "The high estimate (" + money(est.budget.currency, est.budget.estimate_high) + ") is above your confirmation threshold. Continue?",
          confirm: "Regenerate",
        });
        if (!confirmed) return;
      }
      synMessage("");
      const body = Object.assign(resynthBody(), { confirmed: confirmed });
      setSynBusy(true);
      const btn = ui.result.querySelector(".resynth-btn");
      if (btn) btn.replaceChildren(el("span", { class: "spinner" }), "Regenerating\u2026");
      try {
        await CJ.sessions.ensureSession();
        const result = await api("/api/synthesis/resynthesize" + stageQuery(), body);
        syn.outcome = result.chair;
        syn.choices = { dispute: {}, unique: {} };
        addCosts(result, true);
        recordSynCost("Regenerated answer", result);
        storeStage("resynthesize", result, "resynthesize");
        setSynBusy(false);
        renderSynResult();
        CJ.sessions.replaceTurn(turn, true);
        toast("Final answer regenerated");
        const heading = ui.result.querySelector(".final");
        if (heading) heading.scrollIntoView({ block: "start", behavior: scrollBehavior() });
      } catch (error) {
        synMessage(error.message);
        if (error.status !== 409) storeError("resynthesize", error.message);
        setSynBusy(false);
      } finally {
        CJ.composer.refreshBudget();
      }
    }

    // ---------------------------------------------------------------- restore from the turn

    // Rebuild step 2 from the stored stage results (a reload, a reopened session, or
    // work the server finished while this page was elsewhere).
    function restoreSynthesis() {
      const s = turn.synthesis || {};
      const set = turn.synthesis_settings || {};
      if (set.form && !syn.formRestored) {
        syn.formRestored = true;
        ui.instructions.value = set.form.instructions || "";
        for (const radio of ui.tiers) radio.checked = radio.value === (set.form.tier || "economy");
        if (set.form.chair) {
          syn.chair = { provider: set.form.chair.provider, model_id: set.form.chair.model_id };
          syn.chairBackend = set.form.chair.backend || "api";
          selectChair(syn.chair, syn.chairBackend);
        }
      }
      syn.runSettings = set.run
        ? Object.assign({}, set.run, { task: syn.material.task, answers: syn.material.answers, attachment_ids: syn.material.attachment_ids })
        : null;
      const ag = s.agreement || null;
      const fin = s.finish || null;
      const re = s.resynthesize || null;
      const stage = set.outcome !== undefined && set.outcome !== null ? set.outcome
        : (set.outcome === null && set.run ? null : (re ? "resynthesize" : (fin ? "finish" : null)));
      syn.costs = [];
      syn.labels = [];
      syn.agreement = null;
      syn.reviews = null;
      syn.reviewSkipped = false;
      let outcome = null;
      if (ag) {
        syn.runId = ag.run_id;
        syn.seed = ag.seed;
        syn.labels = ag.labels || [];
        syn.agreement = ag.agreement;
        addCosts(ag);
      }
      if (fin && stage) {
        syn.reviews = fin.reviews;
        syn.reviewSkipped = !!fin.review_skipped;
        if (!syn.labels.length) syn.labels = fin.labels || [];
        if (!syn.runId) syn.runId = fin.run_id;
        outcome = fin.chair;
        addCosts(fin);
      }
      if (re && stage === "resynthesize") {
        outcome = re.chair;
        addCosts(re, true);
      }
      const key = outcome ? JSON.stringify(outcome.synthesis) : null;
      if (key !== syn.outcomeKey) syn.choices = { dispute: {}, unique: {} };
      syn.outcomeKey = key;
      syn.outcome = outcome;

      const pendingStage = s.pending || null;
      syn.remote = !!pendingStage;
      ui.decision.hidden = true;
      if (pendingStage) {
        const states = pendingStage === "agreement" ? { agreement: "active" }
          : pendingStage === "finish" ? { agreement: "done", review: "active" }
            : { agreement: "done", review: syn.reviewSkipped ? "skipped" : "done", chair: "active" };
        renderProgress(states);
        synStatus(pendingStage === "resynthesize" ? "Regenerating on the server\u2026" : "Synthesizing on the server\u2026", true);
        synMessage("");
      } else {
        synStatus("");
        const errors = ["agreement", "finish", "resynthesize"].map((k) => s[k + "_error"]).filter(Boolean);
        if (outcome) {
          renderProgress({ agreement: ag ? "done" : "", review: syn.reviewSkipped ? "skipped" : "done", chair: "done" });
          synMessage(s.resynthesize_error && stage !== "resynthesize" ? s.resynthesize_error : "");
        } else if (ag) {
          renderProgress({ agreement: "done" });
          synMessage(errors.length ? errors[errors.length - 1] : "");
          showSkipDecision(ag);
        } else {
          renderProgress(null);
          synMessage(errors.length ? errors[errors.length - 1] : "");
        }
      }
      if (outcome) renderSynResult();
      else { ui.result.hidden = true; ui.result.replaceChildren(); }
      applyLock();
    }

    function syncSynthesis() {
      if (turn.status !== "done" || !turn.step1) {
        if (ui) ui.section.hidden = true;
        return;
      }
      setupSynthesis();
      ui.section.hidden = false;
      if (!syn.material || syn.busy) return;
      const key = synthesisKey(turn);
      if (key === synKey) return;
      synKey = key;
      restoreSynthesis();
    }

    // ---------------------------------------------------------------- update / export

    function update(next) {
      turn = next;
      renderQuestion();
      const key = JSON.stringify([turn.status, turn.step1, turn.error, turn.selections]);
      if (key !== step1Key) {
        step1Key = key;
        renderStep1();
      }
      syncSynthesis();
    }

    function exportLines(level) {
      const h = "#".repeat(level || 2);
      const r = turn.step1;
      const out = [h + " Question to the jury", "", String(turn.task || "").trim(), ""];
      if (r) {
        const files = Array.from(new Set(r.members.flatMap((m) => (m.attachments || []).map((a) => a.filename))));
        if (files.length) out.push("Attachments: " + files.join(", "), "");
        out.push(h + "# Answers", "");
        for (const m of r.members) {
          out.push(h + "## " + providerLabel(m.provider) + " (" + m.model + (m.backend === "cli" ? ", subscription" : "") + ")", "");
          const meta = memberMeta(m).map((x) => x[0] + ": " + x[1]).join("; ");
          if (meta) out.push("_" + meta + "_", "");
          out.push(m.error !== null ? "No answer: " + m.error : m.text.trim(), "");
        }
      } else {
        out.push(turn.status === "pending" ? "_Still running._" : "_No answers: " + (turn.error || "failed") + "_", "");
      }
      if (syn.outcome) {
        const s = syn.outcome.synthesis;
        const a = syn.outcome.additional;
        out.push(h + "# Final answer", "");
        out.push("_Chair: " + chairText() + "; agreement " + agreementText() + "; " + (syn.reviewSkipped ? "review skipped" : "reviewed") + "_", "");
        out.push(String(s.final_text || "").trim(), "");
        out.push(h + "# Additional list", "", h + "## Disputed", "");
        if (!a.disputed.length) out.push("None.", "");
        for (const d of a.disputed) {
          out.push("- **" + d.question + "**");
          for (const p of d.positions) out.push("  - " + labelText(p.label) + ": " + p.stance + ". " + p.argument);
          if (d.chair_assessment) out.push("  - Chair: " + d.chair_assessment);
        }
        out.push("", h + "## Unique insights not included", "");
        if (!a.unique.length) out.push("None.");
        for (const u of a.unique) out.push("- " + labelText(u.label) + ": " + u.insight);
        out.push("", h + "## Possibly wrong", "");
        if (!a.possibly_wrong.length) out.push("None.");
        for (const w of a.possibly_wrong) out.push("- " + labelText(w.target) + " (" + w.severity + "): \"" + w.quote + "\". " + w.problem);
        out.push("");
      }
      return out;
    }

    function exportTurn() {
      const out = ["# llm-crossjury run", "", "_Exported " + new Date().toLocaleString() + "_", ""].concat(exportLines(2));
      CJ.sessions.download(out, "crossjury");
    }

    function destroy() {
      clearTimeout(syn.estimateTimer);
      clearTimeout(syn.resynthTimer);
      clearTimeout(syn.saveTimer);
      if (observer) observer.disconnect();
    }

    // The composer asks the newest card to synthesize on Ctrl+Enter inside its panel.
    update(turn);
    return { node: node, update: update, exportLines: exportLines, destroy: destroy };
  }

  CJ.jury = { create: create, memberMeta: memberMeta };
})();
