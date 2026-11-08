// llm-crossjury settings page: API keys and the first-run onboarding wizard.
// Vanilla JS, no build step. Server text is inserted with textContent only.
// A key typed by the user lives only in a password input until it is sent; it is
// never written into the page, the console or storage.
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);

  const state = {
    keys: [],
    onboarding: null,
    step: 1,
    wizardProvider: null,
    busy: false,
  };
  const STEPS = ["Keys vs subscriptions", "Get a key", "Paste", "Test", "Budget", "Done"];

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

  async function api(method, path, body) {
    const options = { method: method, headers: {} };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
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
    box.textContent = text || "";
    box.hidden = !text;
  }

  function setStatus(node, text, cls) {
    node.textContent = text || "";
    node.className = cls ? cls : "muted";
  }

  function safeHttpsUrl(url) {
    return typeof url === "string" && url.startsWith("https://") ? url : null;
  }

  function vendorLink(url, label) {
    const href = safeHttpsUrl(url);
    if (!href) return document.createTextNode(label);
    return el("a", { href: href, target: "_blank", rel: "noopener noreferrer", text: label });
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
    const status = el("span", {
      class: "badge " + (isSet ? "badge-ok" : "badge-off"),
      text: isSet ? "set" : "not set",
    });
    const result = el("span", { class: "muted", "aria-live": "polite" });
    const formSlot = el("div");

    const testBtn = el("button", {
      type: "button", class: "btn", text: "Test connection", disabled: !isSet,
      onclick: () => testKey(row.name, result, testBtn),
    });
    const editBtn = el("button", {
      type: "button", class: "btn",
      text: row.stored ? "Replace key" : "Add key",
      onclick: () => toggleForm(row, formSlot, result),
    });
    const deleteBtn = row.stored ? el("button", {
      type: "button", class: "btn btn-danger", text: "Delete saved key",
      onclick: () => deleteKey(row, result),
    }) : null;

    const sourceText = isSet
      ? "Source: " + (row.source_label || "unknown")
      : "No key from any source.";
    return el("div", { class: "key-row", "data-provider": row.name }, [
      el("div", { class: "key-row-head" }, [
        el("span", { class: "key-name", text: row.label }),
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

  function toggleForm(row, slot, result) {
    if (slot.firstChild) { slot.replaceChildren(); return; }
    const input = el("input", {
      type: "password", autocomplete: "off", spellcheck: "false",
      placeholder: "Paste the " + row.label + " API key", "aria-label": row.label + " API key",
    });
    const save = el("button", { type: "submit", class: "btn btn-primary", text: "Save" });
    const cancel = el("button", {
      type: "button", class: "btn", text: "Cancel",
      onclick: () => { input.value = ""; slot.replaceChildren(); },
    });
    const form = el("form", { class: "key-inline-form", autocomplete: "off" }, [input, save, cancel]);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!input.value.trim()) { setStatus(result, "Paste a key first.", "result-error"); return; }
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
    input.focus();
  }

  async function saveKey(provider, input) {
    const value = input.value;
    input.value = "";  // clear right away; the value only goes to the server
    return api("PUT", "/api/keys/" + encodeURIComponent(provider), { key: value });
  }

  async function deleteKey(row, result) {
    if (!window.confirm("Delete the " + row.label + " key saved in settings?")) return;
    try {
      await api("DELETE", "/api/keys/" + encodeURIComponent(row.name));
      await loadKeys();
    } catch (err) {
      setStatus(result, err.message, "result-error");
    }
  }

  async function testKey(provider, result, button) {
    if (button) button.disabled = true;
    setStatus(result, "Testing...", "muted");
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

  function wizardRequested() {
    return new URLSearchParams(window.location.search).get("onboarding") === "1";
  }

  function renderWizardStatic() {
    const ob = state.onboarding;
    $("wizard-links").replaceChildren(...ob.providers.map((p) => el("li", {}, [
      el("strong", { text: p.label + ": " }),
      vendorLink(p.key_page_url, p.key_page_label),
    ])));
    const select = $("wizard-provider");
    select.replaceChildren(...ob.providers.map((p) => el("option", { value: p.name, text: p.label })));
    if (state.wizardProvider) select.value = state.wizardProvider;
    const b = ob.budget;
    const money = (v) => b.currency + " " + Number(v).toFixed(2);
    $("wizard-budget").replaceChildren(
      el("dt", { text: "Daily limit" }), el("dd", { text: money(b.daily_limit) }),
      el("dt", { text: "Limit per run" }), el("dd", { text: money(b.run_limit) }),
      el("dt", { text: "Ask before runs above" }), el("dd", { text: money(b.confirm_above) }),
    );
  }

  function renderWizardStep() {
    for (const node of document.querySelectorAll(".wizard-step")) {
      node.hidden = Number(node.dataset.step) !== state.step;
    }
    $("wizard-progress").replaceChildren(...STEPS.map((name, i) => el("li", {
      class: i + 1 === state.step ? "current" : (i + 1 < state.step ? "done" : null),
      "aria-current": i + 1 === state.step ? "step" : null,
      text: (i + 1) + ". " + name,
    })));
    $("wizard-back").disabled = state.step === 1;
    $("wizard-next").hidden = state.step === STEPS.length;
    $("wizard-skip").textContent = state.step === STEPS.length ? "Close" : "Skip setup";
    if (state.step === 4) {
      const p = providerLabel(state.wizardProvider || $("wizard-provider").value);
      $("wizard-test").textContent = "Test " + p + " connection";
    }
  }

  function providerLabel(name) {
    const found = (state.onboarding ? state.onboarding.providers : []).find((p) => p.name === name);
    return found ? found.label : name;
  }

  function anyKeySet() {
    return state.keys.some((k) => k.status === "set");
  }

  function goStep(step) {
    if (step === 4 && !anyKeySet()) {
      setStatus($("wizard-save-status"), "Save a key first, or skip setup.", "result-error");
      return;
    }
    state.step = Math.max(1, Math.min(STEPS.length, step));
    renderWizardStep();
  }

  async function closeWizard() {
    try { await api("POST", "/api/onboarding/dismiss", { dismissed: true }); } catch (_) { /* best effort */ }
    window.location.assign("/settings");
  }

  function initWizard() {
    $("wizard").hidden = false;
    $("open-wizard").hidden = true;
    renderWizardStatic();
    renderWizardStep();
    $("wizard-next").addEventListener("click", () => goStep(state.step + 1));
    $("wizard-back").addEventListener("click", () => goStep(state.step - 1));
    $("wizard-skip").addEventListener("click", closeWizard);
    $("wizard-provider").addEventListener("change", (e) => { state.wizardProvider = e.target.value; });
    $("wizard-save-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = $("wizard-key");
      const status = $("wizard-save-status");
      const provider = $("wizard-provider").value;
      if (!input.value.trim()) { setStatus(status, "Paste a key first.", "result-error"); return; }
      $("wizard-save").disabled = true;
      try {
        const data = await saveKey(provider, input);
        state.wizardProvider = provider;
        await loadKeys();
        setStatus(status, data.message, data.applied ? "result-valid" : "result-network_error");
        goStep(4);
      } catch (err) {
        setStatus(status, err.message, "result-error");
      } finally {
        $("wizard-save").disabled = false;
      }
    });
    $("wizard-test").addEventListener("click", async () => {
      const provider = state.wizardProvider || $("wizard-provider").value;
      await testKey(provider, $("wizard-test-status"), $("wizard-test"));
    });
  }

  // ------------------------------------------------------------------ start

  async function start() {
    try {
      const [, onboarding] = await Promise.all([loadKeys(), api("GET", "/api/onboarding")]);
      state.onboarding = onboarding;
      if (wizardRequested()) initWizard();
    } catch (err) {
      showMessage(err.message);
    }
  }

  start();
})();
