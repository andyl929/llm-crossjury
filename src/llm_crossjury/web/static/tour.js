// llm-crossjury home page: the short "How it works" tour and the onboarding example.
//
// The tour is four coach marks anchored to real elements of the page (the model rack,
// the answers, "Continue with", "Ask the jury"). When an element is not on screen yet
// (an empty conversation), the step points at the matching line of the empty state.
// It opens once after onboarding and again from the "How it works" button. "Seen" is
// saved on the server (POST /api/onboarding/tutorial); localStorage is a convenience.
//
// "/?example=1" (the wizard's "Start with an example") starts a new conversation with
// a sample question and the usable models picked. Nothing runs by itself.
"use strict";

(function () {
  const CJ = window.CJ;
  const { $, el, api, storageGet, storageSet } = CJ;
  const state = CJ.state;

  const SEEN_KEY = "llm-crossjury-tour-seen";
  // The demo's sample question (llm_crossjury.demo.DEMO_QUESTION), so the demo answers fit.
  const EXAMPLE_QUESTION = "I'm comparing reaction times between two groups of 12 participants each. The distributions look somewhat right-skewed. Should I use an independent-samples t-test or a Mann-Whitney U test, and what should I report?";
  const MARGIN = 8;
  const GAP = 12;

  // Each step: candidate targets in order of preference (the first visible one wins;
  // a target taller than the screen is outlined where it is visible), plus optional
  // extra highlights.
  const STEPS = [
    {
      title: "Ask the jury",
      text: "Pick up to four models here and write your question below. Click Estimate cost first: nothing runs until you click Ask.",
      targets: ["#jury-panel", "#jury-btn", "#composer"],
      extra: ["#estimate-btn"],
      // In a conversation the rack is closed; this button opens it.
      alt: { "#jury-btn": "Ask the jury opens the model rack: pick up to four models, then click Estimate cost. Nothing runs until you click Ask." },
    },
    {
      title: "Compare, then synthesize",
      text: "The answers appear side by side. AI synthesize has a chair model write one final answer and list the points still in dispute.",
      targets: [".turn-jury .answers", "#answers-empty .empty-steps li:nth-child(2)"],
      extra: [".turn-jury .syn-btn"],
    },
    {
      title: "Continue with one model",
      text: "Under each answer, Continue with keeps talking to that model, with the conversation so far.",
      targets: [".turn-jury .btn-continue", "#answers-empty .empty-steps li:nth-child(3)"],
      first: true,
    },
    {
      title: "Ask the jury again",
      text: "Mid-conversation, Ask the jury next to Send puts your next question to several models again, with the conversation as context.",
      targets: ["#jury-btn", "#composer"],
    },
  ];

  const T = { open: false, index: 0, box: null, rings: [], returnFocus: null, frame: 0 };

  // ------------------------------------------------------------------ targets

  function visible(node) {
    if (!node || node.closest("[hidden]")) return false;
    const r = node.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  // The newest match (the last jury turn), or with ``first`` the first one of it.
  function lastVisible(selector, first) {
    const nodes = Array.from(document.querySelectorAll(selector)).filter(visible);
    if (!nodes.length) return null;
    if (!first) return nodes[nodes.length - 1];
    const turn = nodes[nodes.length - 1].closest(".turn");
    return turn ? nodes.find((n) => turn.contains(n)) : nodes[0];
  }

  // The step's targets and the selector that matched the main one.
  function resolveTargets(step) {
    let primary = null;
    let matched = null;
    for (const selector of step.targets) {
      primary = lastVisible(selector, step.first);
      if (primary) { matched = selector; break; }
    }
    const extra = (step.extra || []).map((sel) => lastVisible(sel, false)).filter(Boolean);
    return { matched: matched, nodes: primary ? [primary].concat(extra) : extra };
  }

  // The part of a target that is on screen: clipped by its scrolling ancestor.
  function visibleRect(node) {
    const r = node.getBoundingClientRect();
    let top = r.top;
    let bottom = r.bottom;
    const scroller = node.closest(".thread-scroll");
    if (scroller) {
      const s = scroller.getBoundingClientRect();
      top = Math.max(top, s.top);
      bottom = Math.min(bottom, s.bottom);
    }
    return { left: r.left, right: r.right, top: top, bottom: Math.max(top, bottom) };
  }

  // ------------------------------------------------------------------ placement

  function intersects(a, b) {
    return a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
  }

  // Bring the main target into view. A target taller than the free space is scrolled
  // to just below room for the box, so the box can sit above it.
  function reveal(node, boxHeight) {
    const scroller = node.closest(".thread-scroll");
    if (!scroller) { node.scrollIntoView({ block: "nearest", behavior: "auto" }); return; }
    const s = scroller.getBoundingClientRect();
    const r = node.getBoundingClientRect();
    const room = boxHeight + 2 * GAP;
    if (r.height > s.height - room) {
      scroller.scrollTop += r.top - (s.top + room);
    } else {
      node.scrollIntoView({ block: "nearest", behavior: "auto" });
    }
  }

  // Put the box where it covers none of the targets: try positions next to every
  // target edge and the viewport edges, keep the ones that overlap nothing, and take
  // the one closest to the main target. If none fits, use the tallest free band and
  // let the box scroll.
  function place() {
    if (!T.open) return;
    const vw = document.documentElement.clientWidth;
    const vh = window.innerHeight;
    const box = T.box;
    const width = Math.min(360, vw - 2 * MARGIN);
    box.style.width = width + "px";
    box.style.maxHeight = "";
    box.classList.remove("tour-scroll");
    const height = box.offsetHeight;
    const rects = T.targets.map(visibleRect);
    T.rings.forEach((ring, i) => {
      const r = rects[i];
      ring.style.left = (r.left - 4) + "px";
      ring.style.top = (r.top - 4) + "px";
      ring.style.width = (r.right - r.left + 8) + "px";
      ring.style.height = (r.bottom - r.top + 8) + "px";
      ring.hidden = r.bottom - r.top < 1;
    });
    const shown = rects.filter((r) => r.bottom - r.top >= 1);
    const keepOut = shown.map((r) => ({ left: r.left - GAP, right: r.right + GAP, top: r.top - GAP, bottom: r.bottom + GAP }));
    const main = shown[0] || { left: vw / 2, right: vw / 2, top: vh / 2, bottom: vh / 2 };
    const xs = [MARGIN, vw - width - MARGIN];
    const ys = [MARGIN, vh - height - MARGIN];
    for (const r of shown) {
      xs.push(r.left, r.right - width, r.right + GAP, r.left - GAP - width);
      ys.push(r.top, r.bottom - height, r.bottom + GAP, r.top - GAP - height);
    }
    // Prefer not to cover the message box, where people type.
    const dock = document.querySelector(".dock");
    const soft = dock ? dock.getBoundingClientRect() : null;
    let best = null;
    for (const x of xs) {
      if (x < MARGIN || x + width > vw - MARGIN + 0.5) continue;
      for (const y of ys) {
        if (y < MARGIN || y + height > vh - MARGIN + 0.5) continue;
        const cand = { left: x, right: x + width, top: y, bottom: y + height };
        if (keepOut.some((k) => intersects(cand, k))) continue;
        const dx = Math.max(0, main.left - cand.right, cand.left - main.right);
        const dy = Math.max(0, main.top - cand.bottom, cand.top - main.bottom);
        const score = Math.hypot(dx, dy) + Math.abs(cand.left - main.left) / 50
          + (soft && intersects(cand, soft) ? 400 : 0);
        if (!best || score < best.score) best = { x: x, y: y, score: score };
      }
    }
    if (!best) {
      // Tallest free horizontal band; the box scrolls inside it.
      const blocks = keepOut.map((k) => [k.top, k.bottom]).sort((a, b) => a[0] - b[0]);
      let y = MARGIN;
      let band = null;
      for (const [top, bottom] of blocks.concat([[vh - MARGIN, vh - MARGIN]])) {
        if (top > y && (!band || top - y > band[1] - band[0])) band = [y, top];
        y = Math.max(y, bottom);
      }
      band = band || [MARGIN, vh - MARGIN];
      box.style.maxHeight = Math.max(96, band[1] - band[0]) + "px";
      box.classList.add("tour-scroll");
      best = { x: Math.max(MARGIN, Math.min(main.left, vw - width - MARGIN)), y: band[0] };
    }
    box.style.left = best.x + "px";
    box.style.top = best.y + "px";
  }

  function schedulePlace() {
    if (T.frame) return;
    T.frame = window.requestAnimationFrame(() => { T.frame = 0; place(); });
  }

  // ------------------------------------------------------------------ steps

  function render() {
    const step = STEPS[T.index];
    const last = T.index === STEPS.length - 1;
    const titleId = "tour-title";
    const next = el("button", { type: "button", class: "btn btn-primary btn-small", text: last ? "Done" : "Next", onclick: () => go(1) });
    const back = T.index > 0 ? el("button", { type: "button", class: "btn btn-small", text: "Back", onclick: () => go(-1) }) : null;
    const skip = last ? null : el("button", { type: "button", class: "btn btn-link btn-small", text: "Skip tour", onclick: () => close() });
    const resolved = resolveTargets(step);
    const text = (step.alt && step.alt[resolved.matched]) || step.text;
    T.box.replaceChildren(
      el("p", { class: "tour-count", text: "Step " + (T.index + 1) + " of " + STEPS.length }),
      el("h2", { id: titleId, class: "tour-title", tabindex: "-1", text: step.title }),
      el("p", { id: "tour-text", class: "tour-text", text: text }),
      el("div", { class: "tour-actions" }, [skip, el("span", { class: "tour-spacer" }), back, next]),
    );
    T.targets = resolved.nodes;
    T.rings.forEach((ring) => ring.remove());
    T.rings = T.targets.map((_, i) => {
      const ring = el("div", { class: "tour-ring" + (i ? " tour-ring-extra" : ""), "aria-hidden": "true" });
      document.body.append(ring);
      return ring;
    });
    T.box.style.width = Math.min(360, document.documentElement.clientWidth - 2 * MARGIN) + "px";
    if (T.targets[0]) reveal(T.targets[0], T.box.offsetHeight);
    place();
    T.box.querySelector(".tour-title").focus();
  }

  function go(delta) {
    const index = T.index + delta;
    if (index >= STEPS.length) { close(); return; }
    T.index = Math.max(0, index);
    render();
  }

  function onKey(event) {
    if (!T.open) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      close();
      return;
    }
    if (event.key === "Tab") {
      // Keep focus inside the tour while it is open.
      const items = Array.from(T.box.querySelectorAll("button"));
      if (!items.length) return;
      const first = items[0];
      const lastItem = items[items.length - 1];
      const active = document.activeElement;
      if (!T.box.contains(active)) { event.preventDefault(); first.focus(); }
      else if (event.shiftKey && (active === first || !items.includes(active))) { event.preventDefault(); lastItem.focus(); }
      else if (!event.shiftKey && active === lastItem) { event.preventDefault(); first.focus(); }
    }
  }

  function open() {
    if (T.open) return;
    T.open = true;
    T.index = 0;
    T.returnFocus = document.activeElement;
    T.box = el("div", { class: "tour", role: "dialog", "aria-modal": "false", "aria-labelledby": "tour-title", "aria-describedby": "tour-text" });
    document.body.append(T.box);
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", schedulePlace, { passive: true });
    document.addEventListener("scroll", schedulePlace, { passive: true, capture: true });
    $("tour-btn").setAttribute("aria-expanded", "true");
    render();
  }

  async function close() {
    if (!T.open) return;
    T.open = false;
    document.removeEventListener("keydown", onKey, true);
    window.removeEventListener("resize", schedulePlace);
    document.removeEventListener("scroll", schedulePlace, { capture: true });
    T.rings.forEach((ring) => ring.remove());
    T.rings = [];
    T.box.remove();
    T.box = null;
    $("tour-btn").setAttribute("aria-expanded", "false");
    const back = T.returnFocus && document.contains(T.returnFocus) && T.returnFocus !== document.body ? T.returnFocus : $("tour-btn");
    back.focus();
    storageSet(SEEN_KEY, "1");
    try {
      await api("/api/onboarding/tutorial", { seen: true });
    } catch (_) { /* localStorage still remembers it on this browser */ }
  }

  // ------------------------------------------------------------------ example

  // Pick each usable provider once: its API model if a key is set, else its signed-in
  // subscription CLI. At most four, like the rack.
  function exampleSelections() {
    const picks = [];
    for (const p of state.providers) {
      if (picks.length >= 4) break;
      if (p.key_status !== "not set" && p.default_model) {
        picks.push({ provider: p.name, backend: "api", model: p.default_model, temperature: null, max_output_tokens: null });
      } else if (p.cli && p.cli.available && p.cli.default_model) {
        picks.push({ provider: p.name, backend: "cli", model: p.cli.default_model, temperature: null, max_output_tokens: null });
      }
    }
    return picks;
  }

  function startExample() {
    const params = new URLSearchParams(window.location.search);
    if (params.get("example") !== "1") return false;
    window.history.replaceState(null, "", "/");
    CJ.sessions.newConversation(true);
    const picks = exampleSelections();
    if (picks.length) CJ.applySelections(picks);
    CJ.composer.prefill(EXAMPLE_QUESTION, "jury");
    return true;
  }

  // ------------------------------------------------------------------ start

  // Called by app.js once providers and the conversation are loaded.
  function afterStart(onboarding) {
    const button = $("tour-btn");
    button.hidden = false;
    button.setAttribute("aria-expanded", "false");
    button.addEventListener("click", open);
    startExample();
    const seen = !onboarding || onboarding.tutorial_seen === true || storageGet(SEEN_KEY) === "1";
    if (!seen && onboarding.show !== true) window.requestAnimationFrame(open);
  }

  CJ.tour = { afterStart: afterStart, open: open, close: close };
})();
