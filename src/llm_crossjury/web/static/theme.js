// llm-crossjury theme switch, shared by every page. Loaded in <head> so the stored
// theme applies before the first paint. Stores only "light", "dark" or nothing
// (follow the system), and survives blocked storage.
"use strict";

(function () {
  const KEY = "llm-crossjury-theme";
  const ORDER = ["system", "light", "dark"];
  const ICONS = { system: "#i-system", light: "#i-sun", dark: "#i-moon" };
  const NAMES = { system: "system", light: "light", dark: "dark" };

  function read() {
    try {
      const value = window.localStorage.getItem(KEY);
      return value === "light" || value === "dark" ? value : "system";
    } catch (_) {
      return "system";
    }
  }

  function write(theme) {
    try {
      if (theme === "system") window.localStorage.removeItem(KEY);
      else window.localStorage.setItem(KEY, theme);
    } catch (_) { /* the choice lasts for this page only */ }
  }

  function apply(theme) {
    const root = document.documentElement;
    if (theme === "system") delete root.dataset.theme;
    else root.dataset.theme = theme;
  }

  function label(button, theme) {
    const next = ORDER[(ORDER.indexOf(theme) + 1) % ORDER.length];
    button.setAttribute("aria-label", "Theme: " + NAMES[theme] + ". Switch to " + NAMES[next] + ".");
    button.title = "Theme: " + NAMES[theme];
    const use = button.querySelector("use");
    if (use) use.setAttribute("href", ICONS[theme]);
  }

  let current = read();
  apply(current);

  function wire() {
    const button = document.getElementById("theme-toggle");
    if (!button) return;
    label(button, current);
    button.addEventListener("click", () => {
      current = ORDER[(ORDER.indexOf(current) + 1) % ORDER.length];
      apply(current);
      write(current);
      label(button, current);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();
})();
