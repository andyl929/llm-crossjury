// llm-crossjury home page: sessions (ADR 0008). The current conversation, its
// restore and polling, the history sidebar, and the thread that shows its turns.
//
// The server keeps every session. This page holds a copy of the current one and
// updates it from two sources: the response of a request this page made (applied at
// once), and GET /api/sessions/{id}, polled every 2 s while some work is pending (a
// reload, or a return from Settings, while a run is still going). A turn this page is
// working on, or updated after a poll was sent, keeps its local version.
//
// Without the sessions API (an older server), everything works in memory as before.
"use strict";

(function () {
  const CJ = window.CJ;
  const { $, el, icon, iconButton, api, storageGet, storageSet, relativeTime, toast, askDialog, scrollBehavior } = CJ;
  const state = CJ.state;

  const LAST_KEY = "llm-crossjury-session";
  const SIDEBAR_KEY = "llm-crossjury-sidebar";
  const POLL_MS = 2000;
  const WATCH_MS = 1000;
  const LIST_POLL_MS = 10000;
  const NARROW = "(max-width: 899px)";

  const S = {
    current: emptySession(),
    list: [],
    views: new Map(),     // turn id -> view { node, update(turn), json, exportLines() }
    busy: new Set(),      // turn ids with a request of this page in flight
    touched: new Map(),   // turn id -> performance.now() of the last local update
    pollTimer: null,
    listTimer: null,
    loading: null,        // session id being opened
  };

  function emptySession() {
    return { id: null, title: "", turns: [], created_at: null, updated_at: null };
  }

  function available() {
    return state.sessionsApi === true;
  }

  // ------------------------------------------------------------------ query params

  // "?session=S&turn=T" for run-type requests; empty without saved sessions.
  function params(turnId) {
    if (!available() || !S.current.id) return "";
    return CJ.query({ session: S.current.id, turn: turnId || null });
  }

  async function ensureSession() {
    if (!available()) return null;
    if (S.current.id) return S.current.id;
    const created = await api("/api/sessions", {});
    S.current = Object.assign({}, created, { turns: S.current.turns.concat(created.turns || []) });
    remember(created.id);
    upsertListRow(created);
    renderList();
    return created.id;
  }

  // ------------------------------------------------------------------ local turn changes

  function turnById(id) {
    return S.current.turns.find((t) => t.id === id) || null;
  }

  function addTurn(turn) {
    S.current.turns = S.current.turns.concat([turn]);
    S.busy.add(turn.id);
    S.touched.set(turn.id, performance.now());
    renderThread();
    const view = S.views.get(turn.id);
    // A jury card from its question; a chat reply at the bottom, like a chat app.
    if (view) view.node.scrollIntoView({ block: turn.kind === "chat" ? "end" : "start", behavior: scrollBehavior() });
    markListPending();
  }

  // Replace a turn with a newer local version. done=true ends this page's work on it.
  function replaceTurn(turn, done) {
    S.current.turns = S.current.turns.map((t) => (t.id === turn.id ? turn : t));
    S.touched.set(turn.id, performance.now());
    if (done) S.busy.delete(turn.id);
    renderThread();
    schedulePoll();
    if (done) refreshListSoon();
  }

  function removeTurn(id) {
    S.current.turns = S.current.turns.filter((t) => t.id !== id);
    S.busy.delete(id);
    renderThread();
    refreshListSoon();
  }

  function setBusy(id, busy) {
    if (busy) S.busy.add(id);
    else { S.busy.delete(id); S.touched.set(id, performance.now()); }
  }

  function hasTurns() {
    return S.current.turns.length > 0;
  }

  // ------------------------------------------------------------------ polling

  function pending(turn) {
    if (turn.status === "pending") return true;
    if (turn.kind !== "jury") return false;
    return !!(turn.synthesis && turn.synthesis.pending) || (Array.isArray(turn.retrying) && turn.retrying.length > 0);
  }

  function schedulePoll() {
    clearTimeout(S.pollTimer);
    S.pollTimer = null;
    if (!available() || !S.current.id) return;
    if (!S.current.turns.some(pending)) return;
    S.pollTimer = setTimeout(poll, POLL_MS);
  }

  async function poll() {
    const id = S.current.id;
    if (!id) return;
    const started = performance.now();
    try {
      const data = await api("/api/sessions/" + encodeURIComponent(id));
      if (S.current.id !== id) return;
      const wasPending = S.current.turns.some(pending);
      merge(data, started);
      if (wasPending && !S.current.turns.some(pending)) refreshListSoon();
    } catch (error) {
      if (error.status === 404 && S.current.id === id) {
        newConversation(true);
        toast("This conversation was deleted");
        return;
      }
    }
    schedulePoll();
  }

  // While this page awaits POST /api/run for a jury turn, the turn keeps its local
  // copy (S.busy). Watch the server copy every second and take over only the members
  // that finished (members_done), so the cards appear one by one; nothing else of the
  // local turn changes, and the run's own response still wins when it arrives.
  function watchTurn(id) {
    if (!available()) return () => {};
    let active = true;
    let timer = null;
    const tick = async () => {
      timer = null;
      const sid = S.current.id;
      if (!active || !sid) return;
      try {
        const data = await api("/api/sessions/" + encodeURIComponent(sid));
        const st = active && S.current.id === sid ? (data.turns || []).find((t) => t.id === id) : null;
        const lt = st ? turnById(id) : null;
        if (st && lt && lt.kind === "jury" && lt.status === "pending") {
          const got = st.members_done || {};
          if (Object.keys(got).length > Object.keys(lt.members_done || {}).length) {
            S.current.turns = S.current.turns.map((t) => (t.id === id ? Object.assign({}, t, { members_done: got }) : t));
            renderThread();
          }
        }
      } catch (_) { /* the run's own response still arrives */ }
      if (active) timer = setTimeout(tick, WATCH_MS);
    };
    timer = setTimeout(tick, WATCH_MS);
    return () => { active = false; clearTimeout(timer); };
  }

  // Refresh now (e.g. back from another tab): a poll outside the 2 s rhythm.
  function refresh() {
    if (!available()) return;
    refreshList();
    if (S.current.id) poll();
  }

  function keepLocal(id, started) {
    return S.busy.has(id) || (S.touched.get(id) || 0) > started;
  }

  function merge(server, started) {
    const local = new Map(S.current.turns.map((t) => [t.id, t]));
    const turns = [];
    for (const st of server.turns || []) {
      const lt = local.get(st.id);
      turns.push(lt && keepLocal(st.id, started) ? lt : st);
      local.delete(st.id);
    }
    // Turns the server does not know yet (this page's request is still on its way).
    for (const [id, lt] of local) {
      if (keepLocal(id, started)) turns.push(lt);
    }
    S.current = Object.assign({}, server, { turns: turns });
    renderThread();
  }

  // ------------------------------------------------------------------ open / new

  function hashId() {
    const match = /^#s=([0-9a-f]{32})$/.exec(window.location.hash || "");
    return match ? match[1] : null;
  }

  function remember(id) {
    storageSet(LAST_KEY, id);
    const want = id ? "#s=" + id : "";
    if ((window.location.hash || "") !== want) {
      history.replaceState(null, "", id ? want : window.location.pathname + window.location.search);
    }
  }

  function clearThread() {
    for (const view of S.views.values()) {
      view.node.remove();
      if (view.destroy) view.destroy();
    }
    S.views.clear();
    S.busy.clear();
    S.touched.clear();
  }

  async function openSession(id) {
    if (!available() || S.loading === id) return;
    S.loading = id;
    try {
      const data = await api("/api/sessions/" + encodeURIComponent(id));
      clearTimeout(S.pollTimer);
      clearThread();
      S.current = data;
      remember(data.id);
      renderThread();
      renderList();
      CJ.composer.onSessionChanged();
      const scroller = $("main");
      scroller.scrollTop = scroller.scrollHeight;
      schedulePoll();
    } catch (error) {
      if (error.status === 404) {
        if (storageGet(LAST_KEY) === id) storageSet(LAST_KEY, null);
        if (S.current.id === id || !S.current.id) newConversation(true);
        S.list = S.list.filter((row) => row.id !== id);
        renderList();
        if (hashId() === id) toast("That conversation no longer exists");
      } else {
        CJ.showMessage(error.message);
      }
    } finally {
      S.loading = null;
    }
    closeDrawer(false);
  }

  function newConversation(quiet) {
    clearTimeout(S.pollTimer);
    clearThread();
    S.current = emptySession();
    remember(null);
    renderThread();
    renderList();
    CJ.composer.onSessionChanged();
    closeDrawer(false);
    if (!quiet) $("task").focus();
  }

  async function start() {
    try {
      const data = await api("/api/sessions");
      state.sessionsApi = true;
      S.list = Array.isArray(data.sessions) ? data.sessions : [];
    } catch (_) {
      // An older server: no history, and the page keeps the conversation in memory.
      state.sessionsApi = false;
      S.list = [];
    }
    document.body.classList.toggle("no-sessions", !available());
    renderList();
    CJ.composer.onAvailability();
    const id = available() ? hashId() || storageGet(LAST_KEY) : null;
    if (id && /^[0-9a-f]{32}$/.test(id)) await openSession(id);
    else newConversation(true);
    window.addEventListener("hashchange", () => {
      const next = hashId();
      if (next && next !== S.current.id) openSession(next);
    });
  }

  // ------------------------------------------------------------------ history list

  async function refreshList() {
    if (!available()) return;
    try {
      const data = await api("/api/sessions");
      S.list = Array.isArray(data.sessions) ? data.sessions : [];
      renderList();
    } catch (_) { /* keep the list on screen */ }
  }

  let listSoon = null;
  function refreshListSoon() {
    clearTimeout(listSoon);
    listSoon = setTimeout(refreshList, 300);
  }

  function upsertListRow(session) {
    if (S.list.some((r) => r.id === session.id)) return;
    S.list.unshift({ id: session.id, title: session.title || "", created_at: session.created_at,
      updated_at: session.updated_at, turns: (session.turns || []).length, pending: false });
  }

  function markListPending() {
    const row = S.list.find((r) => r.id === S.current.id);
    if (row && !row.pending) {
      row.pending = true;
      row.updated_at = new Date().toISOString();
      renderList();
    }
    refreshListSoon();
  }

  function scheduleListPoll() {
    clearTimeout(S.listTimer);
    if (available() && S.list.some((r) => r.pending)) S.listTimer = setTimeout(refreshList, LIST_POLL_MS);
  }

  function titleOf(row) {
    return row.title && row.title.trim() ? row.title : "New conversation";
  }

  function renderList() {
    const list = $("history");
    const note = $("history-note");
    list.replaceChildren();
    if (!available()) {
      note.textContent = state.sessionsApi === false
        ? "This server does not keep conversations. Answers stay on this page until you leave it." : "";
      note.hidden = state.sessionsApi !== false;
      return;
    }
    const rows = S.list.slice();
    if (!rows.length) {
      note.textContent = "No conversations yet. Your questions and answers are saved on this computer.";
      note.hidden = false;
    } else {
      note.hidden = true;
    }
    for (const row of rows) list.append(historyRow(row));
    scheduleListPoll();
  }

  function historyRow(row) {
    const current = row.id === S.current.id;
    const title = titleOf(row);
    const link = el("a", { class: "hist-link", href: "#s=" + row.id, "aria-current": current ? "page" : null,
      onclick: (e) => {
        if (e.ctrlKey || e.metaKey || e.shiftKey || e.button === 1) return;
        e.preventDefault();
        if (row.id !== S.current.id) openSession(row.id);
        else closeDrawer(false);
      } }, [
      el("span", { class: "hist-title", text: title }),
      el("span", { class: "hist-meta" }, [
        row.pending ? el("span", { class: "hist-dot", title: "Running" }, el("span", { class: "visually-hidden", text: "Running. " })) : null,
        el("time", { datetime: row.updated_at, text: relativeTime(row.updated_at) }),
      ]),
    ]);
    const item = el("li", { class: "hist-item" + (current ? " is-current" : ""), "data-id": row.id }, [link]);
    item.append(el("div", { class: "hist-tools" }, [
      iconButton("edit", "Rename " + title, () => startRename(item, row)),
      iconButton("trash", "Delete " + title, () => deleteSession(row)),
    ]));
    return item;
  }

  function startRename(item, row) {
    const input = el("input", { type: "text", class: "hist-input", value: row.title || "", maxlength: "200",
      "aria-label": "New name for " + titleOf(row), placeholder: titleOf(row) });
    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      const title = input.value.trim();
      if (save && title !== (row.title || "")) {
        try {
          const summary = await api("/api/sessions/" + encodeURIComponent(row.id) + "/title", { title: title });
          Object.assign(row, summary);
          if (row.id === S.current.id) S.current.title = summary.title;
          renderThreadHead();
        } catch (error) {
          toast("Rename failed: " + error.message);
        }
      }
      renderList();
      const again = $("history").querySelector("[data-id='" + row.id + "'] .hist-link");
      if (again) again.focus();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(false); }
    });
    input.addEventListener("blur", () => finish(true));
    item.replaceChildren(input);
    input.focus();
    input.select();
  }

  async function deleteSession(row) {
    const ok = await askDialog({
      title: "Delete this conversation?",
      body: ["\u201c" + titleOf(row) + "\u201d", "Its questions, answers and synthesis results are removed from this computer. This cannot be undone."],
      confirm: "Delete",
      danger: true,
    });
    if (!ok) return;
    try {
      await api("/api/sessions/" + encodeURIComponent(row.id), undefined, "DELETE");
    } catch (error) {
      if (error.status !== 404) { toast("Delete failed: " + error.message); return; }
    }
    S.list = S.list.filter((r) => r.id !== row.id);
    if (row.id === S.current.id) newConversation(true);
    else renderList();
    if (storageGet(LAST_KEY) === row.id) storageSet(LAST_KEY, null);
    toast("Conversation deleted");
  }

  // ------------------------------------------------------------------ sidebar shell

  function narrow() {
    return window.matchMedia && window.matchMedia(NARROW).matches;
  }

  function syncToggle() {
    const open = narrow() ? document.body.classList.contains("drawer-open")
      : !document.body.classList.contains("sidebar-collapsed");
    const btn = $("sidebar-toggle");
    btn.setAttribute("aria-expanded", String(open));
    const label = open ? "Hide conversations" : "Show conversations";
    btn.setAttribute("aria-label", label);
    btn.title = label;
    $("sidebar").inert = narrow() && !open;
  }

  function openDrawer() {
    document.body.classList.add("drawer-open");
    $("sidebar-overlay").hidden = false;
    syncToggle();
    const first = $("new-chat");
    if (first) first.focus();
  }

  function closeDrawer(focusToggle) {
    if (!document.body.classList.contains("drawer-open")) return;
    document.body.classList.remove("drawer-open");
    $("sidebar-overlay").hidden = true;
    syncToggle();
    if (focusToggle) $("sidebar-toggle").focus();
  }

  function initSidebar() {
    if (storageGet(SIDEBAR_KEY) === "collapsed") document.body.classList.add("sidebar-collapsed");
    $("sidebar-toggle").addEventListener("click", () => {
      if (narrow()) {
        if (document.body.classList.contains("drawer-open")) closeDrawer(true);
        else openDrawer();
        return;
      }
      const collapsed = document.body.classList.toggle("sidebar-collapsed");
      storageSet(SIDEBAR_KEY, collapsed ? "collapsed" : null);
      syncToggle();
    });
    $("sidebar-overlay").addEventListener("click", () => closeDrawer(true));
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && document.body.classList.contains("drawer-open")) closeDrawer(true);
    });
    if (window.matchMedia) {
      const mq = window.matchMedia(NARROW);
      const onChange = () => { if (!mq.matches) closeDrawer(false); syncToggle(); };
      if (mq.addEventListener) mq.addEventListener("change", onChange);
    }
    $("new-chat").addEventListener("click", () => newConversation(false));
    $("export-btn").addEventListener("click", exportConversation);
    syncToggle();
    setInterval(() => {
      for (const node of document.querySelectorAll(".hist-meta time")) node.textContent = relativeTime(node.getAttribute("datetime"));
    }, 60000);
  }

  // ------------------------------------------------------------------ thread

  function createView(turn) {
    return turn.kind === "chat" ? CJ.chat.create(turn) : CJ.jury.create(turn);
  }

  function renderThread() {
    const box = $("thread");
    const seen = new Set();
    let prev = null;
    for (const turn of S.current.turns) {
      seen.add(turn.id);
      const json = JSON.stringify(turn);
      let view = S.views.get(turn.id);
      if (!view) {
        view = createView(turn);
        view.json = json;
        S.views.set(turn.id, view);
      } else if (view.json !== json) {
        view.json = json;
        view.update(turn);
      }
      const want = prev ? prev.nextSibling : box.firstChild;
      if (view.node !== want) box.insertBefore(view.node, want);
      prev = view.node;
    }
    for (const [id, view] of S.views) {
      if (seen.has(id)) continue;
      view.node.remove();
      if (view.destroy) view.destroy();
      S.views.delete(id);
    }
    const empty = S.current.turns.length === 0;
    $("answers-empty").hidden = !empty;
    document.body.classList.toggle("thread-empty", empty);
    renderThreadHead();
  }

  function renderThreadHead() {
    const head = $("thread-head");
    const empty = S.current.turns.length === 0;
    head.hidden = empty;
    if (empty) return;
    const row = S.list.find((r) => r.id === S.current.id);
    let title = S.current.title || (row && row.title) || "";
    if (!title) {
      const first = S.current.turns[0];
      title = first.kind === "jury" ? first.task : first.message;
    }
    $("thread-title").textContent = String(title || "Conversation").replace(/\s+/g, " ").trim();
  }

  // ------------------------------------------------------------------ export

  function download(lines, prefix) {
    const blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const stamp = new Date().toISOString().replace(/[-:]/g, "").replace("T", "-").slice(0, 13);
    const link = el("a", { href: url, download: (prefix || "crossjury") + "-" + stamp + ".md", class: "visually-hidden" });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("Markdown file downloaded");
  }

  function exportConversation() {
    if (!S.current.turns.length) return;
    const out = ["# " + ($("thread-title").textContent || "llm-crossjury conversation"), "",
      "_Exported " + new Date().toLocaleString() + "_", ""];
    for (const turn of S.current.turns) {
      const view = S.views.get(turn.id);
      const lines = view && view.exportLines ? view.exportLines(2) : [];
      out.push(...lines, "---", "");
    }
    download(out, "crossjury-conversation");
  }

  CJ.sessions = {
    available: available,
    params: params,
    ensureSession: ensureSession,
    current: () => S.current,
    turnById: turnById,
    addTurn: addTurn,
    replaceTurn: replaceTurn,
    removeTurn: removeTurn,
    setBusy: setBusy,
    hasTurns: hasTurns,
    schedulePoll: schedulePoll,
    watchTurn: watchTurn,
    refresh: refresh,
    refreshList: refreshListSoon,
    start: start,
    initSidebar: initSidebar,
    newConversation: newConversation,
    download: download,
    icon: icon,
  };
})();
