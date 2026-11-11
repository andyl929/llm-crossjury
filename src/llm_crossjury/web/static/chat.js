// llm-crossjury home page: a chat turn in the thread. The user's message on the
// right, the model's reply on the left with a model badge, Markdown, copy, and its
// cost line; a typing indicator while pending; the error with a retry when it failed.
"use strict";

(function () {
  const CJ = window.CJ;
  const { el, icon, iconButton, money, tokens, plural, planUsage, copyText, renderMarkdown, providerLabel, fixFor } = CJ;

  function modelName(turn) {
    return turn.resolved_model || (turn.model && turn.model.model) || "model";
  }

  function continuationText(turn) {
    const c = turn.continued_from;
    if (!c) return null;
    const source = CJ.sessions.turnById(c.turn_id);
    if (c.source === "synthesis") return "Continues from the final answer";
    if (source && source.kind === "jury" && source.step1 && c.member_index !== null && c.member_index !== undefined) {
      const m = source.step1.members[c.member_index];
      if (m) return "Continues from " + providerLabel(m.provider) + "'s answer";
    }
    return "Continues from a jury answer";
  }

  function costLine(turn) {
    const parts = [];
    const cli = turn.model && turn.model.backend === "cli";
    if (turn.cost !== null && turn.cost !== undefined) parts.push(money(turn.currency, turn.cost));
    else if (cli) parts.push(planUsage(1));
    const toks = [];
    if (turn.input_tokens !== null && turn.input_tokens !== undefined) toks.push(tokens(turn.input_tokens) + " in");
    if (turn.output_tokens !== null && turn.output_tokens !== undefined) toks.push(tokens(turn.output_tokens) + " out");
    if (toks.length) parts.push(toks.join(", "));
    if (turn.context_tokens) parts.push(tokens(turn.context_tokens) + " context");
    if (turn.latency_s !== null && turn.latency_s !== undefined) parts.push(Number(turn.latency_s).toFixed(1) + " s");
    return parts;
  }

  function create(initialTurn) {
    let turn = initialTurn;
    const node = el("section", { class: "turn turn-chat", id: "turn-" + turn.id, "aria-label": "Chat message" });

    function render() {
      const m = turn.model || {};
      const label = providerLabel(m.provider || "");
      const files = (turn.attachment_ids || []).length;
      const cont = continuationText(turn);
      const user = el("div", { class: "turn-user" }, el("div", { class: "bubble bubble-user" }, [
        cont ? el("div", { class: "bubble-label" }, [icon("chat"), cont]) : null,
        el("div", { class: "bubble-text", text: turn.message }),
        files ? el("div", { class: "bubble-files" }, [icon("clip"), plural(files, "attached file")]) : null,
      ]));

      const badge = el("div", { class: "reply-badge" }, [
        el("h3", { class: "reply-provider", text: label }),
        el("span", { class: "reply-model", text: modelName(turn) }),
        m.backend === "cli" ? el("span", { class: "tag", text: "Subscription" }) : null,
      ]);

      let body;
      let foot = null;
      const tools = [];
      if (turn.status === "pending") {
        body = el("div", { class: "reply-pending", "aria-live": "polite" }, [
          el("span", { class: "typing", "aria-hidden": "true" }, [el("i"), el("i"), el("i")]),
          el("span", { class: "elapsed", "data-since": turn.created_at || new Date().toISOString(),
            "data-prefix": label + " is writing", text: label + " is writing" }),
        ]);
      } else if (turn.status === "error" || turn.reply === null || turn.reply === undefined) {
        const text = turn.error || "No reply.";
        body = el("div", { class: "answer-error" }, [
          el("div", { class: "answer-error-title" }, [icon("alert"), "No reply"]),
          el("div", { text: text }),
          fixFor(text),
          el("div", { class: "actions" }, [
            el("button", { type: "button", class: "btn btn-small", onclick: () => CJ.composer.retryChat(turn) }, [icon("refresh"), "Retry"]),
            el("button", { type: "button", class: "btn btn-small btn-link", text: "Edit message", onclick: () => CJ.composer.prefill(turn.message, "chat", turn) }),
          ]),
        ]);
      } else {
        body = el("div", { class: "reply-body" }, renderMarkdown(turn.reply));
        tools.push(iconButton("copy", "Copy " + label + " reply", () => copyText(turn.reply, label + " reply")));
        const parts = costLine(turn);
        if (parts.length) {
          foot = el("dl", { class: "meta reply-meta" }, parts.map((p, i) =>
            el("div", {}, [el("dt", { class: i === 0 ? null : "visually-hidden", text: i === 0 ? "Cost" : "Detail" }), el("dd", { text: p })])));
        }
      }

      const reply = el("article", { class: "bubble bubble-model" + (turn.status === "error" ? " has-error" : ""),
        "aria-busy": turn.status === "pending" ? "true" : null }, [
        el("header", { class: "reply-head" }, [badge, tools.length ? el("div", { class: "answer-tools" }, tools) : null]),
        body,
        foot,
      ]);
      node.replaceChildren(user, el("div", { class: "turn-model" }, reply));
    }

    function update(next) {
      turn = next;
      render();
    }

    function exportLines(level) {
      const h = "#".repeat(level || 2);
      const label = providerLabel((turn.model || {}).provider || "");
      const out = [h + " You", ""];
      const cont = continuationText(turn);
      if (cont) out.push("_" + cont + "_", "");
      out.push(String(turn.message || "").trim(), "", h + " " + label + " (" + modelName(turn) + ")", "");
      const parts = turn.status === "done" ? costLine(turn) : [];
      if (parts.length) out.push("_" + parts.join("; ") + "_", "");
      if (turn.status === "pending") out.push("_Still running._", "");
      else if (turn.status === "error" || turn.reply === null || turn.reply === undefined) out.push("_No reply: " + (turn.error || "failed") + "_", "");
      else out.push(String(turn.reply).trim(), "");
      return out;
    }

    render();
    return { node: node, update: update, exportLines: exportLines };
  }

  CJ.chat = { create: create };
})();
