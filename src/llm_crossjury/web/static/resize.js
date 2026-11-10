// llm-crossjury home page: drag-to-resize panes, like an editor's split panes.
//
// - The composer dock: drag its top edge to make it taller (the message box takes the
//   extra room) or click the chevron in its button row to fold it to one line.
// - The conversations sidebar: drag its right edge.
//
// Handles are separators: focusable, arrow keys resize (Shift for bigger steps), Home
// and End jump to the limits, double-click (or Enter) resets. Sizes are custom
// properties on <html> (see resize.css), saved in localStorage and clamped to the
// window. Below 900px the sidebar is a drawer, so only the fold toggle is active.
// The thread keeps its bottom edge in place when the dock grows or shrinks, so the
// last lines you were reading stay visible.
"use strict";

(function () {
  const root = document.documentElement;
  const NARROW = "(max-width: 899px)";
  const DOCK_KEY = "llm-crossjury-dock-height";
  const SIDE_KEY = "llm-crossjury-sidebar-width";
  const FOLD_KEY = "llm-crossjury-dock-collapsed";
  const STEP = 16;
  const BIG_STEP = 64;
  const SIDE_DEFAULT = 272;
  const SIDE_MIN = 200;
  const SIDE_MAX = 480;
  const MAIN_MIN = 440;  // room the thread keeps next to a wide sidebar
  const DOCK_MAX_RATIO = 0.6;

  function load(key) {
    try { return window.localStorage.getItem(key); } catch (_) { return null; }
  }

  function save(key, value) {
    try {
      if (value === null) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, String(value));
    } catch (_) { /* storage blocked: the size lasts for this page only */ }
  }

  function narrow() {
    return Boolean(window.matchMedia && window.matchMedia(NARROW).matches);
  }

  function clamp(value, min, max) {
    return Math.round(Math.min(Math.max(value, min), Math.max(min, max)));
  }

  function storedNumber(key) {
    const n = Number(load(key));
    return load(key) !== null && Number.isFinite(n) && n > 0 ? n : null;
  }

  function svgIcon(id) {
    const sprite = document.querySelector("svg.sprite");
    if (!sprite) return document.createTextNode("");
    const ns = sprite.namespaceURI;
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    const use = document.createElementNS(ns, "use");
    use.setAttribute("href", "#" + id);
    svg.append(use);
    return svg;
  }

  // ---------------------------------------------------------------- one pane

  // spec: { name, handle class, label, controls, orientation ("horizontal" for a
  // top edge, "vertical" for a side edge), host element, sign (+1 when dragging
  // right/down grows the pane), key (storage), prop (CSS custom property), attr
  // (the data- attribute on <html> that turns the size on), limits(), current() }
  function makePane(spec) {
    const handle = document.createElement("div");
    handle.className = "rz-handle " + spec.cls;
    handle.tabIndex = 0;
    handle.setAttribute("role", "separator");
    handle.setAttribute("aria-orientation", spec.orientation);
    handle.setAttribute("aria-label", spec.label);
    handle.setAttribute("aria-controls", spec.controls);
    handle.title = spec.label + " (double-click to reset)";
    spec.host.append(handle);

    let wanted = storedNumber(spec.key);  // the user's choice, before clamping

    function apply() {
      if (wanted === null) {
        root.removeAttribute(spec.attr);
        root.style.removeProperty(spec.prop);
      } else {
        const lim = spec.limits();
        root.style.setProperty(spec.prop, clamp(wanted, lim.min, lim.max) + "px");
        root.setAttribute(spec.attr, "");
      }
      syncAria();
    }

    function syncAria() {
      const lim = spec.limits();
      const now = Math.round(spec.current());
      // The untouched default can sit a pixel or two outside the drag range.
      handle.setAttribute("aria-valuemin", String(Math.min(now, Math.round(lim.min))));
      handle.setAttribute("aria-valuemax", String(Math.max(now, Math.round(Math.max(lim.min, lim.max)))));
      handle.setAttribute("aria-valuenow", String(now));
    }

    function set(size, persist) {
      const lim = spec.limits();
      wanted = clamp(size, lim.min, lim.max);
      apply();
      if (persist) save(spec.key, wanted);
    }

    function reset() {
      wanted = null;
      save(spec.key, null);
      apply();
    }

    // Pointer drag (mouse, touch, pen) with capture, so the drag survives leaving the edge.
    let drag = null;
    handle.addEventListener("pointerdown", (event) => {
      if (narrow() || drag) return;
      if (event.pointerType === "mouse" && event.button !== 0) return;
      event.preventDefault();
      try { handle.setPointerCapture(event.pointerId); } catch (_) { /* old browser */ }
      const pos = spec.orientation === "horizontal" ? event.clientY : event.clientX;
      drag = { id: event.pointerId, pos: pos, size: spec.current() };
      handle.classList.add("is-dragging");
      root.classList.add("rz-dragging", spec.orientation === "horizontal" ? "rz-dragging-ns" : "rz-dragging-ew");
      handle.focus({ preventScroll: true });
    });
    handle.addEventListener("pointermove", (event) => {
      if (!drag || event.pointerId !== drag.id) return;
      const pos = spec.orientation === "horizontal" ? event.clientY : event.clientX;
      set(drag.size + spec.sign * (pos - drag.pos), false);
    });
    function endDrag(event) {
      if (!drag || event.pointerId !== drag.id) return;
      drag = null;
      handle.classList.remove("is-dragging");
      root.classList.remove("rz-dragging", "rz-dragging-ns", "rz-dragging-ew");
      if (wanted !== null) save(spec.key, wanted);
    }
    handle.addEventListener("pointerup", endDrag);
    handle.addEventListener("pointercancel", endDrag);
    handle.addEventListener("lostpointercapture", endDrag);
    handle.addEventListener("dblclick", (event) => {
      event.preventDefault();
      reset();
    });

    handle.addEventListener("keydown", (event) => {
      if (narrow()) return;
      const lim = spec.limits();
      const step = event.shiftKey ? BIG_STEP : STEP;
      const grow = spec.orientation === "horizontal" ? "ArrowUp" : "ArrowRight";
      const shrink = spec.orientation === "horizontal" ? "ArrowDown" : "ArrowLeft";
      let next = null;
      if (event.key === grow) next = spec.current() + step;
      else if (event.key === shrink) next = spec.current() - step;
      else if (event.key === "Home") next = lim.min;
      else if (event.key === "End") next = lim.max;
      else if (event.key === "Enter") { event.preventDefault(); reset(); return; }
      if (next === null) return;
      event.preventDefault();
      set(next, true);
    });
    handle.addEventListener("focus", syncAria);

    if (window.ResizeObserver) new ResizeObserver(syncAria).observe(spec.target);
    window.addEventListener("resize", apply, { passive: true });
    apply();
  }

  // ---------------------------------------------------------------- sidebar

  function initSidebar() {
    const sidebar = document.getElementById("sidebar");
    if (!sidebar) return;
    makePane({
      cls: "rz-side-handle",
      label: "Resize conversations",
      controls: "sidebar",
      orientation: "vertical",
      host: sidebar,
      target: sidebar,
      sign: 1,
      key: SIDE_KEY,
      prop: "--rz-sidebar-w",
      attr: "data-rz-sidebar",
      limits: () => ({ min: SIDE_MIN, max: Math.min(SIDE_MAX, window.innerWidth - MAIN_MIN) }),
      current: () => sidebar.getBoundingClientRect().width || SIDE_DEFAULT,
    });
  }

  // ---------------------------------------------------------------- composer dock

  // The dock's compact height: its content with the message box at its minimum.
  function dockMin(dock) {
    const had = root.hasAttribute("data-rz-dock");
    const prev = root.style.getPropertyValue("--rz-dock-h");
    root.setAttribute("data-rz-dock", "");
    root.style.setProperty("--rz-dock-h", "0px");
    const h = dock.getBoundingClientRect().height;
    if (!had) root.removeAttribute("data-rz-dock");
    if (prev) root.style.setProperty("--rz-dock-h", prev);
    else root.style.removeProperty("--rz-dock-h");
    return Math.ceil(h);
  }

  function initDock() {
    const dock = document.querySelector(".dock");
    const bar = document.querySelector(".composer-bar");
    if (!dock) return;
    makePane({
      cls: "rz-dock-handle",
      label: "Resize message box",
      controls: "composer",
      orientation: "horizontal",
      host: dock,
      target: dock,
      sign: -1,
      key: DOCK_KEY,
      prop: "--rz-dock-h",
      attr: "data-rz-dock",
      // Measured each time: the compact height changes with the mode (an empty jury
      // thread has a taller box), with attachments and with the window width.
      limits: () => ({ min: dockMin(dock), max: Math.round(window.innerHeight * DOCK_MAX_RATIO) }),
      current: () => dock.getBoundingClientRect().height,
    });

    if (!bar) return;
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "icon-btn rz-collapse";
    toggle.setAttribute("aria-controls", "composer");
    toggle.append(svgIcon("i-chevron"));
    bar.prepend(toggle);

    function setFolded(folded, persist) {
      if (folded) root.setAttribute("data-rz-collapsed", "");
      else root.removeAttribute("data-rz-collapsed");
      const label = folded ? "Expand message box" : "Collapse message box to one line";
      toggle.setAttribute("aria-expanded", String(!folded));
      toggle.setAttribute("aria-label", label);
      toggle.title = label;
      if (persist) save(FOLD_KEY, folded ? "1" : null);
    }
    toggle.addEventListener("click", () => setFolded(!root.hasAttribute("data-rz-collapsed"), true));
    setFolded(load(FOLD_KEY) === "1", false);
  }

  // ---------------------------------------------------------------- thread anchoring

  // When the thread's viewport gets shorter (the dock grew) or taller (it shrank), move
  // the scroll position by the same amount so the bottom edge stays on the same line.
  function initAnchor() {
    const main = document.getElementById("main");
    if (!main || !window.ResizeObserver) return;
    let lastTop = main.scrollTop;
    let lastH = main.clientHeight;
    main.addEventListener("scroll", () => {
      // A scroll that lands before the observer has seen a new height is the browser
      // clamping to the new size; the observer below puts the bottom back itself.
      if (main.clientHeight === lastH) lastTop = main.scrollTop;
    }, { passive: true });
    new ResizeObserver(() => {
      const h = main.clientHeight;
      if (h === lastH) return;
      const delta = lastH - h;
      lastH = h;
      if (h > 0 && main.scrollHeight > h) main.scrollTop = Math.max(0, lastTop + delta);
      lastTop = main.scrollTop;
    }).observe(main);

    // Stick to the bottom: when the reader is at the end of the thread and content
    // below grows (a card gains its buttons, a reply arrives), follow it down. A
    // reader who scrolled up keeps their place.
    const content = main.querySelector(".thread-wrap");
    if (!content) return;
    const SLACK = 8;
    const nearBottom = () => main.scrollHeight - main.clientHeight - main.scrollTop <= SLACK;
    let atBottom = nearBottom();
    let lastContentH = content.offsetHeight;
    main.addEventListener("scroll", () => { atBottom = nearBottom(); }, { passive: true });
    new ResizeObserver(() => {
      const ch = content.offsetHeight;
      const grew = ch > lastContentH;
      lastContentH = ch;
      if (grew && atBottom) main.scrollTop = main.scrollHeight;
    }).observe(content);
  }

  initSidebar();
  initDock();
  initAnchor();
})();
