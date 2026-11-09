// llm-crossjury settings page: API keys, the first-run setup wizard (subscriptions and
// API keys), and the app settings (budget, history, memory).
// Vanilla JS, no build step. Server text is inserted with textContent only.
// A key typed by the user lives only in a password input until it is sent; it is
// never written into the page, the console or storage.
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const SPRITE = document.querySelector("svg.sprite");
  const SVG_NS = SPRITE ? SPRITE.namespaceURI : null;

  const state = {
    keys: [],
    onboarding: null,
    step: "welcome",
    path: "both",
    os: "unix",
    cliLoaded: false,
    cliLoading: false,
    rechecked: false,
    busy: false,
    app: null,
    providers: null,
    budgetInfo: null,
    memoryOptions: [],
  };

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

  async function api(method, path, body) {
    const options = { method: method, headers: {} };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(path, options);
    } catch (_) {
      throw new Error("Cannot reach the llm-crossjury server. Check that it is still running in your terminal, then reload.");
    }
    let data = null;
    try { data = await response.json(); } catch (_) { data = null; }
    if (!response.ok) {
      const message = data && typeof data.detail === "string"
        ? data.detail
        : "Request failed (HTTP " + response.status + ").";
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function showMessage(text) {
    const box = $("message");
    box.replaceChildren();
    box.hidden = !text;
    if (text) box.append(icon("alert"), el("div", { class: "message-body" }, el("span", { text: text })));
  }

  // Status text next to a control. cls: result-valid, result-error, result-network_error...
  function setStatus(node, text, cls) {
    node.replaceChildren();
    node.className = "status" + (cls ? " " + cls : "");
    if (!text) return;
    if (cls === "result-valid") node.append(icon("check"));
    else if (cls && cls !== "muted") node.append(icon("alert"));
    node.append(el("span", { text: text }));
  }

  function safeHttpsUrl(url) {
    return typeof url === "string" && url.startsWith("https://") ? url : null;
  }

  function vendorLink(url, label) {
    const href = safeHttpsUrl(url);
    if (!href) return document.createTextNode(label);
    return el("a", { href: href, target: "_blank", rel: "noopener noreferrer", class: "ext-link" }, [
      label, icon("external"), el("span", { class: "visually-hidden", text: " (opens in a new tab)" }),
    ]);
  }

  // ------------------------------------------------------------------ keys page

  async function loadKeys() {
    const data = await api("GET", "/api/keys");
    state.keys = data.providers;
    renderKeys();
    $("store-note").textContent = data.store_path
      ? "Settings key store: " + data.store_path + ". " + data.store_note
      : data.store_note;
  }

  function renderKeys() {
    const box = $("keys");
    box.replaceChildren(...state.keys.map(renderKeyRow));
  }

  function renderKeyRow(row) {
    const isSet = row.status === "set";
    const status = el("span", { class: "lamp " + (isSet ? "lamp-ok" : "lamp-warn"), text: isSet ? "Key set" : "No key" });
    const result = el("span", { class: "status", "aria-live": "polite" });
    const formSlot = el("div", { class: "key-form-slot" });

    const testBtn = el("button", {
      type: "button", class: "btn", text: "Test connection", disabled: !isSet,
      title: isSet ? null : "Add a key first",
      onclick: () => testKey(row.name, result, testBtn),
    });
    const editBtn = el("button", {
      type: "button", class: "btn",
      text: row.stored ? "Replace key" : "Add key",
      "aria-expanded": "false",
    });
    editBtn.addEventListener("click", () => toggleForm(row, formSlot, result, editBtn));
    const deleteBtn = row.stored ? el("button", {
      type: "button", class: "btn btn-danger", text: "Delete saved key",
      onclick: () => deleteKey(row, result),
    }) : null;

    const sourceText = isSet
      ? "Source: " + (row.source_label || "unknown")
      : "No key from any source.";
    return el("div", { class: "key-row", "data-provider": row.name }, [
      el("div", { class: "key-row-head" }, [
        el("h3", { class: "key-name", text: row.label }),
        status,
        el("span", { class: "key-env" }, ["Variable ", el("code", { text: row.env_var })]),
      ]),
      el("div", { class: "key-source", text: sourceText }),
      row.how_to_remove && row.source !== "settings"
        ? el("p", { class: "key-help", text: "How to remove: " + row.how_to_remove })
        : null,
      row.overridden
        ? el("p", {
          class: "key-warn",
          text: "A key is also saved in settings, but the source above takes precedence."
            + " Delete removes only the saved one.",
        })
        : null,
      !isSet && row.stored
        ? el("p", {
          class: "key-warn",
          text: "A key is saved in settings but is not loaded. Save it again or restart the app.",
        })
        : null,
      !isSet
        ? el("p", { class: "key-help" }, ["Get a key: ", vendorLink(row.key_page_url, row.key_page_label)])
        : null,
      el("div", { class: "actions" }, [testBtn, editBtn, deleteBtn, result]),
      formSlot,
    ]);
  }

  function toggleForm(row, slot, result, opener) {
    if (slot.firstChild) {
      slot.replaceChildren();
      opener.setAttribute("aria-expanded", "false");
      return;
    }
    const inputId = "key-input-" + row.name;
    const input = el("input", {
      id: inputId, type: "password", autocomplete: "off", spellcheck: "false",
      placeholder: "Paste the " + row.label + " API key",
    });
    const save = el("button", { type: "submit", class: "btn btn-primary", text: "Save key" });
    const cancel = el("button", {
      type: "button", class: "btn", text: "Cancel",
      onclick: () => { input.value = ""; slot.replaceChildren(); opener.setAttribute("aria-expanded", "false"); opener.focus(); },
    });
    const form = el("form", { class: "key-inline-form", autocomplete: "off" }, [
      el("label", { class: "field-label", for: inputId, text: "New " + row.label + " API key" }),
      el("div", { class: "key-inline-row" }, [input, save, cancel]),
    ]);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!input.value.trim()) { setStatus(result, "Paste a key first.", "result-error"); input.focus(); return; }
      save.disabled = true;
      try {
        const data = await saveKey(row.name, input);
        await loadKeys();
        const row2 = document.querySelector('.key-row[data-provider="' + row.name + '"] [aria-live]');
        if (row2) setStatus(row2, data.message, data.applied ? "result-valid" : "result-network_error");
      } catch (err) {
        setStatus(result, err.message, "result-error");
        save.disabled = false;
      }
    });
    slot.replaceChildren(form);
    opener.setAttribute("aria-expanded", "true");
    input.focus();
  }

  async function saveKey(provider, input) {
    const value = input.value;
    input.value = "";  // clear right away; the value only goes to the server
    return api("PUT", "/api/keys/" + encodeURIComponent(provider), { key: value });
  }

  async function deleteKey(row, result) {
    if (!window.confirm("Delete the " + row.label + " key saved in settings? Other sources are not touched.")) return;
    try {
      await api("DELETE", "/api/keys/" + encodeURIComponent(row.name));
      await loadKeys();
    } catch (err) {
      setStatus(result, err.message, "result-error");
    }
  }

  async function testKey(provider, result, button) {
    if (button) button.disabled = true;
    result.replaceChildren(el("span", { class: "spinner" }), el("span", { text: "Testing\u2026" }));
    result.className = "status";
    try {
      const data = await api("POST", "/api/keys/" + encodeURIComponent(provider) + "/test", {});
      const label = {
        valid: "Valid", invalid: "Invalid", network_error: "Network error",
        not_set: "Not set", error: "Error",
      }[data.status] || "Error";
      setStatus(result, label + ": " + data.message, "result-" + data.status);
      return data;
    } catch (err) {
      setStatus(result, err.message, "result-error");
      return null;
    } finally {
      if (button) button.disabled = false;
    }
  }

  // ------------------------------------------------------------------ wizard
  //
  // Up to four steps: Welcome (pick a path), Subscriptions, API keys, Done. The path
  // decides which of the middle steps are shown. Every step can be skipped. The
  // subscription step only shows commands and re-checks; it never runs anything.

  const STEP_NAMES = { welcome: "Welcome", subs: "Subscriptions", keys: "API keys", done: "Done" };
  const PATHS = { subs: ["welcome", "subs", "done"], keys: ["welcome", "keys", "done"], both: ["welcome", "subs", "keys", "done"] };
  const CLI_STATE_TEXT = {
    not_checked: ["lamp", "Checking"],
    not_installed: ["lamp-warn", "Not installed"],
    signed_out: ["lamp-warn", "Installed, not signed in"],
    ready: ["lamp-ok", "Ready"],
  };

  function wizardRequested() {
    return new URLSearchParams(window.location.search).get("onboarding") === "1";
  }

  function wizardSteps() {
    return PATHS[state.path] || PATHS.both;
  }

  function detectOs() {
    const platform = (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || navigator.userAgent || "";
    return /win/i.test(platform) ? "windows" : "unix";
  }

  function renderProgress() {
    const steps = wizardSteps();
    const index = steps.indexOf(state.step);
    $("wizard-progress").replaceChildren(...steps.map((name, i) => {
      const cls = i === index ? "current" : (i < index ? "done" : null);
      return el("li", { class: cls, "aria-current": i === index ? "step" : null }, [
        el("span", { class: "step-mark", "aria-hidden": "true" }, i < index ? icon("check") : String(i + 1)),
        el("span", { class: "step-name", text: STEP_NAMES[name] }),
        el("span", { class: "visually-hidden", text: " (step " + (i + 1) + " of " + steps.length + (i < index ? ", done)" : ")") }),
      ]);
    }));
  }

  function renderWizardStep(moveFocus) {
    const steps = wizardSteps();
    const index = steps.indexOf(state.step);
    for (const node of document.querySelectorAll(".wizard-step")) {
      node.hidden = node.dataset.step !== state.step;
    }
    renderProgress();
    $("wizard-back").disabled = index === 0;
    $("wizard-next").hidden = index === steps.length - 1;
    $("wizard-skip").hidden = index === steps.length - 1;  // Done has its own two exits
    // One primary action in view: on the key step that is "Save and test".
    $("wizard-next").className = "btn" + (state.step === "keys" ? "" : " btn-primary");
    if (state.step === "subs") loadCliStatus(false);
    if (state.step === "keys") renderKeyStep();
    if (state.step === "done") renderDone();
    if (moveFocus) {
      const heading = document.querySelector('.wizard-step[data-step="' + state.step + '"] h3');
      if (heading) { heading.setAttribute("tabindex", "-1"); heading.focus(); }
    }
  }

  function goStep(delta) {
    const steps = wizardSteps();
    const index = Math.max(0, Math.min(steps.length - 1, steps.indexOf(state.step) + delta));
    state.step = steps[index];
    renderWizardStep(true);
  }

  // -------------------------------------------------------------- subscriptions

  function copyButton(text, what) {
    const button = el("button", {
      type: "button", class: "icon-btn copy-btn", "aria-label": "Copy " + what, title: "Copy",
    }, icon("copy"));
    button.addEventListener("click", async () => {
      let ok = false;
      try { await navigator.clipboard.writeText(text); ok = true; } catch (_) { ok = false; }
      button.setAttribute("title", ok ? "Copied" : "Copy failed: select the text and copy it");
      setStatus($("cli-status"), ok ? "Copied: " + text : "Copy failed. Select the command and copy it.", ok ? "result-valid" : "result-error");
    });
    return button;
  }

  function commandLine(text, what) {
    return el("div", { class: "cmd-line" }, [el("code", { class: "cmd", text: text }), copyButton(text, what)]);
  }

  function installCommands(cli) {
    const all = cli.install_commands || [];
    const mine = all.filter((c) => c.platform === state.os || c.platform === "any");
    return mine.length ? mine : all;
  }

  function cliRow(p) {
    const cli = p.cli;
    // Without a login check, "ready" only means the tool was found.
    const [lampCls, lampText] = cli.state === "ready" && !cli.login_checked
      ? ["lamp-ok", "Found"]
      : CLI_STATE_TEXT[cli.state] || CLI_STATE_TEXT.not_checked;
    const version = cli.version ? " (" + cli.version + ")" : "";
    const head = el("div", { class: "cli-row-head" }, [
      el("h4", { class: "cli-name", text: cli.product }),
      el("span", { class: "cli-plan", text: "for " + cli.plan }),
      cli.experimental ? el("span", { class: "tag tag-warn", text: "Experimental" }) : null,
      el("span", { class: "lamp " + lampCls, text: lampText }),
    ]);
    const body = [];
    if (cli.state === "ready") {
      body.push(el("p", { class: "help", text: cli.login_checked
        ? "Signed in" + version + ". Pick Subscription on its model row."
        : "Installed" + version + ". It cannot report its sign-in, so if you have not signed in yet, run " + cli.sign_in_command + " once in a terminal." }));
    } else if (cli.state !== "not_checked") {
      const steps = [];
      if (cli.state === "not_installed") {
        const commands = installCommands(cli);
        steps.push(el("li", {}, [
          el("span", { class: "cmd-label", text: "Install" }),
          ...commands.map((c, i) => el("div", { class: "cmd-option" }, [
            i > 0 ? el("span", { class: "help", text: "or, " + c.label }) : (commands.length > 1 ? el("span", { class: "help", text: c.label }) : null),
            commandLine(c.command, cli.product + " install command"),
          ])),
        ]));
      }
      steps.push(el("li", {}, [
        el("span", { class: "cmd-label", text: "Sign in" }),
        commandLine(cli.sign_in_command, cli.product + " sign-in command"),
        el("span", { class: "help", text: cli.sign_in }),
      ]));
      body.push(el("ol", { class: "cmd-steps" }, steps));
    }
    return el("div", { class: "cli-row", "data-provider": p.name }, [head, ...body]);
  }

  function renderCliRows() {
    const rows = (state.providers || []).filter((p) => p.cli);
    const box = $("cli-rows");
    if (!rows.length) {
      box.replaceChildren(el("p", { class: "help rack-empty", text: "No subscription tools are offered in this setup." }));
      return;
    }
    box.replaceChildren(...rows.map(cliRow));
    document.querySelector(".os-switch").hidden = !rows.some((p) => p.cli.state === "not_installed");
    for (const button of document.querySelectorAll(".os-switch [data-os]")) {
      button.setAttribute("aria-pressed", String(button.dataset.os === state.os));
    }
  }

  async function loadCliStatus(refresh) {
    if (state.cliLoading) return;
    if (!refresh && state.providers && state.cliLoaded) { renderCliRows(); return; }
    state.cliLoading = true;
    const button = $("cli-recheck");
    button.disabled = true;
    const status = $("cli-status");
    status.replaceChildren(el("span", { class: "spinner" }), el("span", { text: "Checking\u2026" }));
    status.className = "status";
    try {
      const data = await api("GET", "/api/providers" + (refresh ? "?refresh=true" : ""));
      state.providers = data.providers;
      state.budgetInfo = data.budget;
      state.cliLoaded = true;
      renderCliRows();
      const clis = data.providers.filter((p) => p.cli);
      const ready = clis.filter((p) => p.cli.state === "ready").length;
      setStatus(status, ready + " of " + clis.length + " ready.", ready ? "result-valid" : "muted");
      if (refresh) state.rechecked = true;
      $("cli-tip").hidden = !(state.os === "windows" && state.rechecked && clis.some((p) => p.cli.state === "not_installed"));
    } catch (err) {
      setStatus(status, err.message, "result-error");
    } finally {
      state.cliLoading = false;
      button.disabled = false;
    }
  }

  // -------------------------------------------------------------- API keys

  function renderKeyStep() {
    const select = $("wizard-provider");
    if (!select.options.length) {
      select.replaceChildren(...state.onboarding.providers.map((p) => el("option", { value: p.name, text: p.label })));
      const firstMissing = state.keys.find((k) => k.status !== "set");
      if (firstMissing) select.value = firstMissing.name;
    }
    renderKeyLink();
    $("wizard-key-list").replaceChildren(...state.keys.map((k) => el("li", {}, [
      el("span", { class: "status-name", text: k.label }),
      el("span", { class: "lamp " + (k.status === "set" ? "lamp-ok" : "lamp"), text: k.status === "set" ? "Key set" : "No key" }),
    ])));
  }

  function renderKeyLink() {
    const vendor = state.onboarding.providers.find((p) => p.name === $("wizard-provider").value);
    $("wizard-key-link").replaceChildren(vendor ? el("span", {}, ["Get a key: ", vendorLink(vendor.key_page_url, vendor.key_page_label)]) : "");
  }

  async function saveAndTest(event) {
    event.preventDefault();
    const input = $("wizard-key");
    const status = $("wizard-save-status");
    const provider = $("wizard-provider").value;
    if (!input.value.trim()) { setStatus(status, "Paste a key first.", "result-error"); input.focus(); return; }
    $("wizard-save").disabled = true;
    try {
      const saved = await saveKey(provider, input);
      await loadKeys();
      renderKeyStep();
      if (!saved.applied) {
        setStatus(status, saved.message, "result-network_error");
        return;
      }
      await testKey(provider, status, null);
      renderKeyStep();
    } catch (err) {
      setStatus(status, err.message, "result-error");
    } finally {
      $("wizard-save").disabled = false;
    }
  }

  // -------------------------------------------------------------- done

  function renderDone() {
    const b = state.onboarding.budget;
    const fmt = (v) => money(v, b.currency);
    $("wizard-budget").replaceChildren(
      el("div", {}, [el("dt", { text: "Per run" }), el("dd", { text: fmt(b.run_limit) })]),
      el("div", {}, [el("dt", { text: "Per day" }), el("dd", { text: fmt(b.daily_limit) })]),
      el("div", {}, [el("dt", { text: "Confirm above" }), el("dd", { text: fmt(b.confirm_above) })]),
    );
    const rows = state.keys.map((k) => {
      const p = (state.providers || []).find((x) => x.name === k.name);
      const ways = [];
      if (k.status === "set") ways.push("API key");
      if (p && p.cli && p.cli.state === "ready") ways.push(p.cli.product);
      return { label: k.label, ways: ways };
    });
    const ready = rows.filter((r) => r.ways.length).length;
    $("wizard-done-title").textContent = ready ? "Ready to go" : "Nothing connected yet";
    $("wizard-done-intro").textContent = ready
      ? (ready === 1 ? "1 vendor is connected." : ready + " vendors are connected.") + " Comparing works best with two or more."
      : "You can still look around. Add a key or sign in to a tool later: Settings has everything from this setup.";
    $("wizard-summary").replaceChildren(...rows.map((r) => el("li", {}, [
      el("span", { class: "status-name", text: r.label }),
      el("span", { class: "lamp " + (r.ways.length ? "lamp-ok" : "lamp"), text: r.ways.length ? r.ways.join(" and ") : "Not connected" }),
    ])));
    // The subscription states may not be loaded yet when the subscription step was skipped.
    if (!state.cliLoaded && !state.cliLoading) loadCliStatus(false).then(() => { if (state.step === "done") renderDone(); });
  }

  async function finishWizard(target) {
    try { await api("POST", "/api/onboarding/dismiss", { dismissed: true }); } catch (_) { /* best effort */ }
    window.location.assign(target);
  }

  function leaveTarget() {
    return new URLSearchParams(window.location.search).get("from") === "settings" ? "/settings" : "/";
  }

  function initWizard() {
    $("wizard").hidden = false;
    $("open-wizard").hidden = true;
    $("main").classList.add("wizard-open");
    state.os = detectOs();
    state.step = "welcome";
    renderWizardStep(false);
    for (const radio of document.querySelectorAll('input[name="wizard-path"]')) {
      radio.addEventListener("change", () => { state.path = radio.value; renderProgress(); });
    }
    for (const button of document.querySelectorAll(".os-switch [data-os]")) {
      button.addEventListener("click", () => { state.os = button.dataset.os; renderCliRows(); });
    }
    $("wizard-next").addEventListener("click", () => goStep(1));
    $("wizard-back").addEventListener("click", () => goStep(-1));
    $("wizard-skip").addEventListener("click", () => finishWizard(leaveTarget()));
    $("wizard-finish").addEventListener("click", () => finishWizard("/"));
    $("wizard-example").addEventListener("click", () => finishWizard("/?example=1"));
    $("cli-recheck").addEventListener("click", () => loadCliStatus(true));
    $("wizard-provider").addEventListener("change", () => { renderKeyLink(); setStatus($("wizard-save-status"), "", null); });
    $("wizard-save-form").addEventListener("submit", saveAndTest);
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      const t = event.target;
      if (t && t.id === "wizard-key" && t.value) { t.value = ""; return; }  // first Esc clears the key
      event.preventDefault();
      finishWizard(leaveTarget());
    });
    // The subscription states load in the background (the first check can take a few seconds).
    loadCliStatus(false);
  }

  // ------------------------------------------------------------------ app settings
  // Budget, History and Memory: GET/PUT /api/settings/app. Each section saves the whole
  // settings object with only its own fields changed.

  const MONEY_RE = /^\d{1,7}(\.\d{1,2})?$/;

  function money(value, currency) {
    return (currency === "USD" ? "$" : currency + " ") + Number(value).toFixed(2);
  }

  async function saveAppSettings(changes, status, button) {
    if (button) button.disabled = true;
    setStatus(status, "Saving\u2026", "muted");
    try {
      const next = Object.assign({}, state.app.settings, changes);
      const data = await api("PUT", "/api/settings/app", next);
      state.app.settings = data.settings;
      renderAppSettings();
      setStatus(status, "Saved.", "result-valid");
      return true;
    } catch (err) {
      setStatus(status, err.message, "result-error");
      return false;
    } finally {
      if (button) button.disabled = false;
    }
  }

  function renderBudget() {
    const b = state.app.settings.budget;
    const d = state.app.defaults.budget;
    const currency = state.budgetInfo ? state.budgetInfo.currency : "USD";
    $("budget-currency").textContent = currency;
    $("budget-run").value = Number(b.run_limit).toFixed(2);
    $("budget-daily").value = Number(b.daily_limit).toFixed(2);
    $("budget-confirm").value = Number(b.confirm_above).toFixed(2);
    $("budget-defaults").textContent = "Defaults: " + money(d.run_limit, currency) + " per run, "
      + money(d.daily_limit, currency) + " per day, ask above " + money(d.confirm_above, currency) + ".";
    const info = state.budgetInfo;
    $("budget-spent").textContent = info
      ? "Spent today: " + money(info.spent_today, currency) + "."
      : "";
  }

  function readBudget(status) {
    const fields = [["budget-run", "run_limit", "The limit per run"],
      ["budget-daily", "daily_limit", "The daily limit"],
      ["budget-confirm", "confirm_above", "The confirmation amount"]];
    const budget = {};
    for (const [id, key, label] of fields) {
      const text = $(id).value.trim();
      if (!MONEY_RE.test(text) || (key !== "confirm_above" && Number(text) <= 0)) {
        setStatus(status, label + " must be an amount like 5.00"
          + (key !== "confirm_above" ? ", above zero." : "."), "result-error");
        $(id).focus();
        return null;
      }
      budget[key] = text;
    }
    return budget;
  }

  function renderHistory() {
    $("history-save").checked = state.app.settings.save_history === true;
  }

  function modelKey(ref) {
    return ref ? ref.provider + "/" + ref.backend + "/" + ref.model : "auto";
  }

  function memoryModelOptions() {
    const options = [];
    for (const p of (state.providers || [])) {
      for (const m of p.models || []) {
        options.push({ ref: { provider: p.name, backend: "api", model: m.id }, label: p.label + ": " + m.label + " (API)" });
      }
      if (p.cli && p.cli.available) {
        for (const m of p.cli.models || []) {
          options.push({ ref: { provider: p.name, backend: "cli", model: m.id }, label: p.label + ": " + m.label + " (" + p.cli.product + ")" });
        }
      }
    }
    return options;
  }

  function renderMemory() {
    const current = state.app.settings.memory_model;
    const select = $("memory-model");
    const options = memoryModelOptions();
    state.memoryOptions = options;
    const nodes = [el("option", { value: "auto", text: "Automatic" })];
    options.forEach((o, i) => nodes.push(el("option", { value: String(i), text: o.label })));
    let selected = "auto";
    if (current) {
      const index = options.findIndex((o) => modelKey(o.ref) === modelKey(current));
      if (index >= 0) {
        selected = String(index);
      } else {
        // Saved model not offered right now (no key, CLI not signed in): keep it visible.
        state.memoryOptions.push({ ref: current, label: modelKey(current) + " (not available now)" });
        selected = String(state.memoryOptions.length - 1);
        nodes.push(el("option", { value: selected, text: state.memoryOptions[state.memoryOptions.length - 1].label }));
      }
    }
    select.replaceChildren(...nodes);
    select.value = selected;
    $("memory-budget").value = String(state.app.settings.context_budget_tokens);
  }

  function renderAppSettings() {
    renderBudget();
    renderHistory();
    renderMemory();
  }

  async function loadProvidersInfo() {
    try {
      const data = await api("GET", "/api/providers");
      state.providers = data.providers;
      state.budgetInfo = data.budget;
      if (state.app) { renderBudget(); renderMemory(); }
    } catch (_) {
      // Optional: without it the page shows no spend and only "Automatic".
    }
  }

  function initAppSettings() {
    $("budget-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const status = $("budget-status");
      const budget = readBudget(status);
      if (!budget) return;
      if (await saveAppSettings({ budget: budget }, status, $("budget-save"))) loadProvidersInfo();
    });
    $("budget-reset").addEventListener("click", async () => {
      const status = $("budget-status");
      if (await saveAppSettings({ budget: state.app.defaults.budget }, status, $("budget-reset"))) loadProvidersInfo();
    });
    $("history-save").addEventListener("change", async (event) => {
      const box = event.target;
      const wanted = box.checked;
      if (!wanted && !window.confirm("Stop saving conversations? New ones will be lost when the server stops. Saved ones are kept.")) {
        box.checked = true;
        return;
      }
      box.disabled = true;
      await saveAppSettings({ save_history: wanted }, $("history-status"), null);
      renderHistory();
      box.disabled = false;
    });
    $("history-clear").addEventListener("click", async () => {
      const status = $("history-clear-status");
      if (!window.confirm("Delete all conversations? This cannot be undone.")) return;
      $("history-clear").disabled = true;
      try {
        const data = await api("DELETE", "/api/sessions");
        const n = Number(data.deleted) || 0;
        setStatus(status, n === 1 ? "Deleted 1 conversation." : "Deleted " + n + " conversations.", "result-valid");
      } catch (err) {
        setStatus(status, err.message, "result-error");
      } finally {
        $("history-clear").disabled = false;
      }
    });
    $("memory-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const status = $("memory-status");
      const text = $("memory-budget").value.trim();
      const tokens = Number(text);
      if (!/^\d+$/.test(text) || tokens < 8000) {
        setStatus(status, "The context budget must be a whole number of at least 8000.", "result-error");
        $("memory-budget").focus();
        return;
      }
      const choice = $("memory-model").value;
      const option = choice === "auto" ? null : state.memoryOptions[Number(choice)];
      await saveAppSettings({
        memory_model: option ? option.ref : null,
        context_budget_tokens: tokens,
      }, status, $("memory-save"));
    });
  }

  async function loadAppSettings() {
    state.app = await api("GET", "/api/settings/app");
    renderAppSettings();
    initAppSettings();
  }

  // ------------------------------------------------------------------ start

  async function start() {
    loadProvidersInfo();
    try {
      const [, onboarding] = await Promise.all([
        loadKeys(), api("GET", "/api/onboarding"), loadAppSettings(),
      ]);
      state.onboarding = onboarding;
      if (wizardRequested()) initWizard();
    } catch (err) {
      showMessage(err.message);
    }
  }

  start();
})();
