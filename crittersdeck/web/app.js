// crittersdeck board: live view of Claude Code sessions, drawn as critters.

const $ = (sel) => document.querySelector(sel);
const STATUS_TEXT = {
  working: "Working",
  needs_you: "Needs you",
  done_unseen: "Done, not seen",
  idle: "Idle",
  ended: "Ended",
};
const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto", style: "narrow" });

let csrf = "";
let board = { now: 0, sources: [], sessions: [], rooms: [] };
let events = null;
let openDetailKey = null;
let linkDown = false;
const openEnded = new Set(); // "Ended" lists the viewer opened, kept open across re-renders
const drafts = new Map(); // answers being picked for questions, by request id: kept across re-renders
let composing = false;
let renderLater = false;
document.addEventListener("compositionstart", () => { composing = true; });
document.addEventListener("compositionend", () => {
  composing = false;
  if (renderLater) { renderLater = false; render(); }
});

// ---- helpers -------------------------------------------------------------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v == null) continue;
    if (k === "class") node.className = v;
    // CSSOM, not a style attribute: the board's CSP blocks inline style attributes.
    else if (k === "style") for (const [prop, val] of Object.entries(v)) node.style.setProperty(prop, val);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  node.append(...children.filter((c) => c != null && c !== false));
  return node;
}

function ago(ts) {
  const secs = Math.round(ts - Date.now() / 1000);
  const abs = Math.abs(secs);
  if (abs < 60) return rtf.format(secs, "second");
  if (abs < 3600) return rtf.format(Math.round(secs / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(secs / 3600), "hour");
  return rtf.format(Math.round(secs / 86400), "day");
}

function lineCount(text) {
  const n = text.split("\n").length;
  return n > 1 ? `${n} lines` : null;
}

// The command as text nodes with break opportunities after "/", "=", "&&" and before " -",
// so long paths wrap at a sensible place (built with DOM nodes, never HTML).
function withBreaks(text) {
  const out = [];
  for (const part of text.split(/(?<=[/=]|&&)|(?= -)/)) out.push(part, document.createElement("wbr"));
  return out;
}

// Status text with the time kept on one line.
function tileStatus(s) {
  const line = statusLine(s);
  const at = line.lastIndexOf(" · ");
  if (!["idle", "ended", "done_unseen"].includes(s.status) || at < 0) return [line];
  return [line.slice(0, at + 3), el("span", { class: "nowrap" }, line.slice(at + 3))];
}

function keyOf(s) {
  return `${s.source_id}/${s.session_id}`;
}

function placeOf(s) {
  return `${s.folder || "session"} · ${s.session_id.slice(0, 4)}`;
}

// The session's title (its first prompt, or a name given on the board).
function nameOf(s) {
  return s.title || "Untitled session";
}

let toastTimer;
function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 4000);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "content-type": "application/json", "x-csrf-token": csrf, ...(options.headers || {}) },
  });
  if (res.status === 401) {
    showLogin();
    throw new Error("signed out");
  }
  const body = res.headers.get("content-type")?.includes("json") ? await res.json() : {};
  if (!res.ok) {
    const err = new Error(body.detail || body.error || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return body;
}

// ---- critters (SVG) --------------------------------------------------------

const SVG_NS = "http://www.w3.org/2000/svg";
function svg(tag, attrs) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
}

// ---- critters: own pixel art -------------------------------------------------
// Grid: the critter's body spans x 0..18, y 2..14. A hat sits above it (y -6..2) and a
// little sign floats at its top right (x 19..24, y -7..-2).

const INK = "#141210";

// Bitmaps: "#" main colour, "o" second colour, "w" white, " " empty.
function pix(rows, ox, oy, colors, cls) {
  const g = svg("g", cls ? { class: cls } : {});
  rows.forEach((row, y) => {
    [...row].forEach((ch, x) => {
      if (colors[ch]) g.append(svg("rect", { x: ox + x, y: oy + y, width: 1, height: 1, fill: colors[ch] }));
    });
  });
  return g;
}

// One hat per working folder, so sessions are told apart at a glance.
const HATS = [
  { name: "beanie", rows: ["   ww   ", "  ####  ", " ###### ", "oooooooo"], x: 5, y: -2, c: { "#": "#5b7fd6", o: "#3d5fae", w: "#f4f1ea" } },
  { name: "straw hat", rows: ["    ######    ", "    oooooo    ", "##############"], x: 2, y: -1, c: { "#": "#e2b955", o: "#8a5a24" } },
  { name: "crown", rows: ["#  ##  #", "########", "##o##o##"], x: 5, y: -1, c: { "#": "#f4f1ea", o: "#d94b4b" } },
  { name: "cap", rows: ["   ww   ", "  ######", " #######", "oooooooo"], x: 3, y: -2, c: { "#": "#e05a5a", o: "#a53a3a", w: "#f4f1ea" } },
  { name: "top hat", rows: ["  ######  ", "  ######  ", "  ######  ", "  oooooo  ", "##########"], x: 4, y: -4, c: { "#": "#8a857b", o: "#c4473a" } },
  { name: "party hat", rows: ["   ww   ", "   ##   ", "  oooo  ", " ###### ", "oooooooo"], x: 5, y: -4, c: { "#": "#e85d9a", o: "#f2c94c", w: "#f4f1ea" } },
];

function hatFor(folder) {
  let h = 0;
  for (const ch of folder || "") h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return HATS[h % HATS.length];
}

// What the critter is doing, as a little sign: [kind, bitmap, colours].
function signFor(status, tool) {
  if (status === "needs_you") return ["ask", [" ### ", "#   #", "   # ", "  #  ", "     ", "  #  "], { "#": "currentColor" }];
  if (status === "done_unseen") return ["cookie", [" ### ", "#o###", "####.", "##o  ", " ##  "], { "#": "#c98b4a", o: "#4b2a12" }];
  if (status === "idle" || status === "ended") return ["sleep", ["####", "  # ", " #  ", "####"], { "#": "var(--muted)" }];
  if (status !== "working") return null;
  if (/^(Bash|PowerShell)/.test(tool)) return ["command", ["#    ", " #   ", "  #  ", " #   ", "#  ##"], { "#": "currentColor" }];
  if (/^(Edit|Write|MultiEdit|NotebookEdit)$/.test(tool)) return ["edit", ["   oo", "  ##o", " ### ", "###  ", "#    "], { "#": "#f2c94c", o: "#e85d9a" }];
  if (/^(Read|Grep|Glob|LS)$/.test(tool)) return ["read", [" ##  ", "#  # ", "#  # ", " ##  ", "   ##"], { "#": "currentColor" }];
  if (/^Web/.test(tool)) return ["web", [" ### ", "# # #", "#####", "# # #", " ### "], { "#": "#5b9bd6" }];
  return ["think", ["     ", "     ", "     ", "     ", "# # #"], { "#": "currentColor" }];
}

const ACTIVITY = { command: "running a command", edit: "editing", read: "reading", web: "on the web", think: "thinking" };

function critter(status, color, tool = "", folder = "", solid = true) {
  const asleep = status === "idle" || status === "ended";
  // On a solid alert tile the asking critter is a dark silhouette; elsewhere it keeps its colour.
  const asking = status === "needs_you" && solid;
  const root = svg("svg", { viewBox: "-6 -8 30 23", class: "critter-svg", "aria-hidden": "true", "shape-rendering": "crispEdges" });
  root.style.color = asleep ? "var(--critter-asleep)" : asking ? INK : color;
  const armY = status === "needs_you" ? 3 : 5;
  const body = svg("g", { class: "body", fill: "currentColor" });
  body.append(
    svg("rect", { x: 3, y: 2, width: 12, height: 9 }),
    svg("rect", { x: 0, y: armY, width: 3, height: 2, class: "arm" }),
    svg("rect", { x: 15, y: armY, width: 3, height: 2, class: "arm" }),
  );
  const legs = svg("g", { class: "legs", fill: "currentColor" });
  for (const [i, x] of [4, 6, 11, 13].entries()) {
    legs.append(svg("rect", { x, y: 11, width: 1, height: 3, class: i % 2 ? "leg leg-b" : "leg leg-a" }));
  }
  // Eyes: dark on a coloured body; on the dark silhouette of an asking critter, the alert colour.
  const eye = asking ? "var(--ask)" : INK;
  const face = svg("g", { class: "face", fill: eye });
  if (asleep) {
    face.append(svg("rect", { x: 5, y: 6, width: 3, height: 1 }), svg("rect", { x: 10, y: 6, width: 3, height: 1 }));
  } else if (status === "done_unseen") {
    face.append(pix(["#  ", " # ", "#  "], 5, 4, { "#": INK }), pix(["  #", " # ", "  #"], 10, 4, { "#": INK }),
      svg("rect", { x: 8, y: 8, width: 2, height: 1, class: "mouth" }));
  } else {
    face.append(
      svg("rect", { x: 6, y: 4, width: 1, height: status === "needs_you" ? 3 : 2, class: "eye" }),
      svg("rect", { x: 11, y: 4, width: 1, height: status === "needs_you" ? 3 : 2, class: "eye" }),
    );
  }
  const hat = hatFor(folder);
  const hatG = pix(hat.rows, hat.x, hat.y, hat.c, "hat");
  root.append(legs, body, face, hatG);
  const sign = signFor(status, tool);
  if (sign) {
    const [kind, rows, colors] = sign;
    root.append(pix(rows, 19, -7, colors, `sign sign-${kind}`));
  }
  return root;
}

// MCP tools are named mcp__<server>__<tool>: the tool part is what says what it does.
function toolName(tool) {
  return tool.startsWith("mcp__") ? tool.split("__").slice(2).join("__") || tool : tool;
}

function statusLine(s) {
  if (s.status === "working") return ACTIVITY[signFor("working", s.tool)[0]] + (s.tool ? ` · ${toolName(s.tool)}` : "");
  if (s.status === "needs_you") {
    if (!s.pending) return "has a question";
    return s.pending.questions ? "has a question" : `allow ${s.pending.tool || "a tool"}?`;
  }
  if (s.status === "done_unseen") return `done · ${ago(s.updated)}`; // the stripes say "not seen"
  if (s.status === "idle") return `idle · ${ago(s.updated)}`;
  if (s.status === "ended") return `ended · ${ago(s.updated)}`;
  return STATUS_TEXT[s.status];
}

// ---- rendering ----------------------------------------------------------------

function tile(s, src) {
  return el(
    "li",
    {},
    el(
      "button",
      {
        type: "button",
        class: `tile s-${s.status}${openDetailKey === keyOf(s) ? " open" : ""}`,
        style: { "--c": src.color },
        "data-focus": `tile:${keyOf(s)}`,
        onclick: () => openDetail(keyOf(s)),
        // a live critter can be dragged onto a room (Add to room in its panel does the same without a mouse)
        draggable: s.status === "ended" ? null : "true",
        ondragstart: (e) => startDrag(e, s),
        ondragend: endDrag,
      },
      el("span", { class: "corner", "aria-hidden": "true" }),
      s.unread ? el("span", { class: "mail", title: `${s.unread} message${s.unread > 1 ? "s" : ""} waiting` },
        "✉ ", String(s.unread), el("span", { class: "visually-hidden" }, ` message${s.unread > 1 ? "s" : ""} waiting,`)) : null,
      el("span", { class: "art" }, critter(s.status, src.color, s.tool, s.folder)),
      el("span", { class: "tile-name" }, nameOf(s), el("span", { class: "visually-hidden" }, `, ${src.name},`)),
      el("span", { class: "tile-place" }, placeOf(s)),
      el("span", { class: "tile-status" }, ...tileStatus(s)),
    ),
  );
}

function needCard(s, src) {
  const p = s.pending;
  if (p?.questions) return questionCard(s, src, p);
  return el(
    "li",
    { class: p ? "need" : "need compact", style: { "--c": src.color } },
    el("span", { class: "art small" }, critter("needs_you", src.color, s.tool, s.folder, false)),
    el(
      "div",
      { class: "need-text" },
      el("p", { class: "need-who" }, el("strong", {}, nameOf(s)), ` · ${placeOf(s)} · ${src.name}`),
      p
        ? el("p", { class: "need-what" }, `Wants to use ${p.tool || "a tool"}`, p.description ? el("span", { class: "need-why" }, ` — ${p.description}`) : null)
        : el("p", { class: "need-what" }, "Waiting for an answer in the session"),
      // Shown in full (no inner scroll), so nothing that Allow runs can hide below the fold.
      p && lineCount(p.input) ? el("p", { class: "small need-lines" }, lineCount(p.input)) : null,
      p ? el("pre", { class: "need-input", tabindex: "0", "data-focus": `pre:${p.id}`, "aria-label": "Exactly what it will run" }, ...withBreaks(p.input)) : null,
      p && !p.complete ? el("p", { class: "small" }, "Too long to show in full, so it can only be allowed in the session.") : null,
      p ? null : el("p", { class: "small" }, "Answer it in the session: it did not come through the board's hook."),
    ),
    p
      ? el(
          "div",
          { class: "need-actions" },
          p.complete
            ? el("button", { type: "button", class: "btn allow", "data-focus": `allow:${p.id}`, disabled: linkDown, onclick: () => answer(p.id, "allow") }, "Allow")
            : null,
          el("button", { type: "button", class: "btn deny", "data-focus": `deny:${p.id}`, disabled: linkDown, onclick: () => answer(p.id, "deny") }, "Deny"),
        )
      : null,
  );
}

// A question with options (AskUserQuestion), answered on the board. The session shows it too.
// "Other" is one more choice, with its text field: picking it never throws away other choices.
function questionCard(s, src, p) {
  if (!drafts.has(p.id)) drafts.set(p.id, p.questions.map(() => ({ options: new Set(), otherOn: false, other: "", error: "" })));
  const draft = drafts.get(p.id);
  const fieldsets = p.questions.map((q, qi) => {
    const d = draft[qi];
    const name = `q-${p.id}-${qi}`;
    const errorId = `${name}-error`;
    const choose = (on, apply) => {
      if (!q.multi && on) { d.options.clear(); d.otherOn = false; }
      apply();
      d.error = "";
      render();
    };
    return el(
      "fieldset",
      { class: "question", "aria-describedby": d.error ? errorId : null },
      el("legend", {}, q.header ? el("span", { class: "q-header" }, q.header) : null, el("span", { class: "q-text" }, q.question)),
      q.multi ? el("p", { class: "small q-hint" }, "Choose one or more.") : null,
      ...q.options.map((o, oi) =>
        el(
          "label",
          { class: "q-option" },
          el("input", {
            type: q.multi ? "checkbox" : "radio", name, value: String(oi), checked: d.options.has(oi),
            "data-focus": `q:${p.id}:${qi}:${oi}`,
            onchange: (e) => {
              const on = e.currentTarget.checked;
              choose(on, () => (on ? d.options.add(oi) : d.options.delete(oi)));
            },
          }),
          el("span", { class: "q-label" }, o.label, o.description ? el("span", { class: "q-desc" }, o.description) : null),
        ),
      ),
      el(
        "div",
        { class: d.otherOn ? "q-option q-other" : "q-option q-other off" },
        el("input", {
          type: q.multi ? "checkbox" : "radio", name, value: "other", id: `${name}-other`, checked: d.otherOn,
          "data-focus": `qr:${p.id}:${qi}`,
          onchange: (e) => {
            const on = e.currentTarget.checked;
            choose(on, () => { d.otherOn = on; });
          },
        }),
        el(
          "span",
          { class: "q-label" },
          el("label", { for: `${name}-other` }, "Other"),
          el("input", {
            type: "text", maxlength: "500", autocomplete: "off", value: d.other, placeholder: "Type your own answer",
            "aria-label": `Your own answer to: ${q.question}`, "data-focus": `qo:${p.id}:${qi}`,
            oninput: (e) => {
              d.other = e.currentTarget.value;
              if (d.other.trim() && (!d.otherOn || d.error)) choose(true, () => { d.otherOn = true; }); // typing chooses "Other"
            },
          }),
        ),
      ),
      // Only the first error is announced: focus goes there; the others are reachable by their question.
      el("p", { class: "error small", id: errorId, role: d.error && draft.findIndex((x) => x.error) === qi ? "alert" : null }, d.error),
    );
  });
  return el(
    "li",
    { class: "need question-need", style: { "--c": src.color } },
    el("span", { class: "art small" }, critter("needs_you", src.color, s.tool, s.folder, false)),
    el(
      "form",
      { class: "need-text", onsubmit: (e) => { e.preventDefault(); sendAnswers(p); } },
      el("p", { class: "need-who" }, el("strong", {}, nameOf(s)), ` · ${placeOf(s)} · ${src.name}`),
      el("p", { class: "need-what" }, p.questions.length > 1 ? "Has some questions for you" : "Has a question for you"),
      ...fieldsets,
      el(
        "div",
        { class: "need-actions" },
        el("button", { type: "submit", class: "btn allow", "data-focus": `allow:${p.id}`, disabled: linkDown }, "Send"),
        el("button", { type: "button", class: "btn", "data-focus": `deny:${p.id}`, disabled: linkDown, onclick: () => answer(p.id, "deny", true) }, "Skip"),
      ),
    ),
  );
}

async function sendAnswers(p) {
  const draft = drafts.get(p.id) || [];
  for (const d of draft) {
    const own = d.otherOn && d.other.trim();
    d.error = d.otherOn && !own ? "Write your answer, or untick Other." : !d.options.size && !own ? "Choose an answer." : "";
  }
  const missing = draft.findIndex((d) => d.error);
  if (missing >= 0) {
    render();
    const field = document.querySelector(`[data-focus="${CSS.escape(draft[missing].otherOn ? `qo:${p.id}:${missing}` : `q:${p.id}:${missing}:0`)}"]`);
    field?.focus();
    field?.closest("fieldset")?.scrollIntoView({ block: "nearest" });
    return;
  }
  const answers = draft.map((d) => ({ options: [...d.options], other: d.otherOn ? d.other : "" }));
  try {
    await api(`/api/approvals/${encodeURIComponent(p.id)}`, { method: "POST", body: JSON.stringify({ decision: "answer", answers }) });
    drafts.delete(p.id);
    toast("Answer sent. The agent carries on.");
  } catch (err) {
    toast(err.status === 409 ? "Already answered in the session." : `Could not send the answer: ${err.message}`);
  }
  const next = [...document.querySelectorAll("#needs-list .need-actions .btn")].find((b) => !b.dataset.focus.endsWith(`:${p.id}`));
  (next || $("#needs-title")).focus();
}

async function answer(id, decision, question = false) {
  try {
    await api(`/api/approvals/${encodeURIComponent(id)}`, { method: "POST", body: JSON.stringify({ decision }) });
    toast(question ? "Skipped. The agent was told you did not answer."
      : decision === "allow" ? "Allowed. The agent carries on." : "Denied. The agent was told no.");
  } catch (err) {
    toast(err.status === 409 ? "Already answered in the session." : `Could not send the answer: ${err.message}`);
  }
  const next = [...document.querySelectorAll("#needs-list .need-actions .btn")].find((b) => !b.dataset.focus.endsWith(`:${id}`));
  (next || $("#needs-title")).focus();
}

// ---- pages ------------------------------------------------------------------------

const PAGES = ["board", "rooms", "sources", "settings", "about"];
let me = null;

function currentPage() {
  const name = location.hash.replace(/^#\/?/, "").split("/")[0];
  return PAGES.includes(name) ? name : "board";
}

function showPage() {
  const page = currentPage();
  if (page !== "sources") {
    // "shown only now": the token leaves the page with you
    $("#source-created").hidden = true;
    for (const id of ["#install-snippet", "#source-snippet"]) $(id).textContent = "";
  }
  for (const section of document.querySelectorAll(".page")) section.hidden = section.dataset.page !== page;
  for (const link of document.querySelectorAll(".nav a")) {
    if (link.dataset.page === page) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  document.title = titleFor(page);
  if (page === "sources") renderSources();
  if (page === "settings") renderSettings();
  if (page === "about") renderAbout();
  if (page === "rooms") renderRooms(true);
}

function titleFor(page) {
  const asking = board.sessions.filter((s) => s.status === "needs_you").length;
  const room = page === "rooms" && roomById(currentRoomId());
  const name = page === "board" ? "crittersdeck" : `${room ? room.name : page[0].toUpperCase() + page.slice(1)} · crittersdeck`;
  return asking ? `(${asking}) ${name}` : name;
}

window.addEventListener("hashchange", () => {
  showPage();
  (currentRoomId() && roomById(currentRoomId()) ? $("#room-title") : $("#main")).focus({ preventScroll: true });
  window.scrollTo(0, 0);
});

// ---- board --------------------------------------------------------------------------

function render() {
  const sources = new Map(board.sources.map((src) => [src.id, src]));
  const visible = board.sessions.filter((s) => sources.has(s.source_id));

  // The strip: counts at a glance.
  const count = (status) => visible.filter((s) => s.status === status).length;
  const asking = visible
    .filter((s) => s.status === "needs_you")
    // Stable order, so cards do not jump under a finger: requests the board can answer, oldest
    // first, then questions that must be answered in the session.
    .sort((a, b) => (b.pending ? 1 : 0) - (a.pending ? 1 : 0) || (a.pending?.created ?? a.started) - (b.pending?.created ?? b.started));
  $("#stat-needs").textContent = asking.length;
  $(".stat-ask").classList.toggle("on", asking.length > 0);
  $("#stat-working").textContent = count("working");
  $("#stat-done").textContent = count("done_unseen");
  $("#stat-sources").textContent = board.sources.length;
  $("#nav-needs").textContent = asking.length;
  $("#nav-needs").hidden = !asking.length;
  document.title = titleFor(currentPage());

  const n = asking.length;
  $("#board-title").replaceChildren(
    n ? `${n === 1 ? "One critter needs" : `${n} critters need`} ` : "Nothing needs ", el("em", {}, "you."));
  $("#needs-count").textContent = linkDown ? "—" : asking.length;
  $("#needs-empty").hidden = asking.length > 0;
  $("#needs-empty").textContent = linkDown
    ? "Not connected, so this list may be out of date."
    : "Nothing needs you. When an agent asks for permission or a question, it shows up here.";
  if (composing) { renderLater = true; return; } // rebuilding the field would drop half-typed text
  const focused = document.activeElement?.dataset?.focus;
  const range = [document.activeElement?.selectionStart, document.activeElement?.selectionEnd];
  $("#needs-list").replaceChildren(...asking.map((s) => needCard(s, sources.get(s.source_id))));

  // One group per machine.
  $("#deck-empty").hidden = board.sources.length > 0;
  const groups = board.sources.map((src) => {
    const mine = visible.filter((s) => s.source_id === src.id && s.status !== "ended");
    const ended = visible.filter((s) => s.source_id === src.id && s.status === "ended");
    const last = Math.max(0, ...visible.filter((s) => s.source_id === src.id).map((s) => s.updated));
    const endedKey = `ended:${src.id}`;
    return el(
      "section",
      { class: "group", "aria-labelledby": `g-${src.id}` },
      el("h2", { id: `g-${src.id}`, class: "section" },
        el("span", { class: "dot", style: { "--c": src.color }, "aria-hidden": "true" }), src.name,
        el("span", { class: "count" }, String(mine.length)),
        last ? el("span", { class: "section-note" }, `last event ${ago(last)}`) : null),
      mine.length
        ? el("ul", { class: "grid", role: "list" }, ...mine.map((s) => tile(s, src)))
        : el("p", { class: "quiet" }, ended.length ? "No live sessions." : "No sessions yet. They appear when this source sends its first event."),
      ended.length
        ? el("details", { class: "ended", open: openEnded.has(endedKey), ontoggle: (e) => {
            if (e.currentTarget.open) openEnded.add(endedKey); else openEnded.delete(endedKey);
          } },
            el("summary", {}, `Ended (${ended.length})`),
            el("ul", { class: "grid", role: "list" }, ...ended.map((s) => tile(s, src))))
        : null,
    );
  });
  $("#groups").replaceChildren(...groups);
  renderStrip(visible);
  // Everything was rebuilt: put keyboard focus back where it was.
  if (focused) {
    const again = document.querySelector(`[data-focus="${CSS.escape(focused)}"]`);
    again?.focus({ preventScroll: true });
    if (again?.type === "text" && range[0] != null) again.setSelectionRange(...range);
  }
  for (const id of drafts.keys()) if (!asking.some((s) => s.pending?.id === id)) drafts.delete(id);

  if (openDetailKey) fillDetail();
  if (currentPage() === "sources") renderSources();
  if (currentPage() === "rooms") renderRooms(false);
  notify(visible, sources);
}

// ---- side panel: one session ------------------------------------------------------------

function openDetail(key) {
  openDetailKey = key;
  fillDetail();
  if (!$("#drawer").open) $("#drawer").showModal();
  render();
}

function tileOrder() {
  return [...document.querySelectorAll("#groups .tile")].map((t) => t.dataset.focus.slice(5));
}

const num = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(n >= 1e4 ? 0 : 1)}k` : String(n));

function usageBlock(info) {
  if (!info || !Object.keys(info).length) {
    return el("p", { class: "usage-none" }, "No usage from this machine. Update the crittersdeck plugin there, with Send titles and usage on, to see context, tokens and cost.");
  }
  const has = (k) => info[k] != null;
  const pct = has("context_used") && info.context_max ? Math.min(1, info.context_used / info.context_max) : null;
  const cells = Array.from({ length: 30 }, (_, i) => el("i", { class: pct != null && i < Math.round(pct * 30) ? "on" : null }));
  const figures = [
    ["Tokens in", has("tokens_in") ? num(info.tokens_in) : "—"],
    ["Tokens out", has("tokens_out") ? num(info.tokens_out) : "—"],
    ["Cache read", has("cache_read") ? num(info.cache_read) : "—"],
    ["Cache write", has("cache_write") ? num(info.cache_write) : "—"],
    ["Cost", has("cost_usd") ? el("span", {}, `$${info.cost_usd.toFixed(2)}`, el("small", {}, " est")) : "—"],
    ["Prompts", has("turns") ? String(info.turns) : "—"],
    ["Model", has("model") ? info.model.replace(/^claude-/, "") : "—"],
  ];
  return el(
    "div",
    { class: "usage" },
    el("div", { class: "usage-head" },
      el("span", { class: "label" }, "Context"),
      el("span", {}, has("context_used")
        ? `${num(info.context_used)}${info.context_max ? ` / ${num(info.context_max)} · ${Math.round(pct * 100)}%` : ""}`
        : "—")),
    pct != null ? el("div", { class: "cells", role: "img", "aria-label": `Context ${Math.round(pct * 100)}% used` }, ...cells) : null,
    el("dl", { class: "figures" }, ...figures.flatMap(([k, v]) => [el("div", {}, el("dt", {}, k), el("dd", {}, v))])),
  );
}

function fillDetail() {
  const s = board.sessions.find((x) => keyOf(x) === openDetailKey);
  const drawer = $("#drawer");
  if (!s) {
    if (drawer.open) drawer.close();
    return;
  }
  const src = board.sources.find((x) => x.id === s.source_id);
  $("#drawer-art").replaceChildren(critter(s.status, src?.color ?? "#888888", s.tool, s.folder, false));
  $("#drawer-eyebrow").textContent = `${src?.name ?? "?"} · ${s.folder || "session"}`;
  $("#drawer-title").textContent = nameOf(s);
  const badge = s.status === "working" ? `working${s.tool ? ` · ${toolName(s.tool)}` : ""}` : `${STATUS_TEXT[s.status]} · ${ago(s.updated)}`;
  $("#drawer-badge").textContent = badge;
  const facts = [
    ["Doing", s.detail || "nothing reported"],
    ["Started", ago(s.started)],
    ["Session", s.session_id],
  ];
  $("#drawer-facts").replaceChildren(...facts.flatMap(([k, v]) => [el("div", {}, el("dt", {}, k), el("dd", {}, v))]));
  $("#drawer-ask").hidden = s.status !== "needs_you";
  $("#drawer-ask-text").textContent = s.pending
    ? (s.pending.questions ? `${s.pending.questions.length > 1 ? "Questions" : "A question"} with options` : `Allow ${s.pending.tool || "a tool"}?`)
    : "It asked something in the session.";
  $("#drawer-ask-go").hidden = !s.pending;
  $("#drawer-usage").replaceChildren(usageBlock(s.info));
  if (document.activeElement !== $("#rename-input")) $("#rename-input").value = s.title || "";
  $("#messages-title").textContent = s.unread ? `Messages · ${s.unread} waiting` : "Messages";
  $("#drawer-seen").hidden = s.status !== "done_unseen";
  const joinable = openRooms().filter((r) => !r.members.some((m) => memberKey(m) === openDetailKey));
  $("#drawer-room-form").hidden = s.status === "ended" || !joinable.length;
  fillSelect($("#drawer-room-select"), joinable.map((r) => [r.id, r.name]));
  loadThread(openDetailKey);
}

// The session's messages: fetched when the panel opens and when the board changes.
let threadTimer;
function loadThread(key) {
  clearTimeout(threadTimer);
  threadTimer = setTimeout(async () => {
    const [src, sid] = key.split("/");
    let rows = [];
    try {
      rows = await api(`/api/sessions/${src}/${encodeURIComponent(sid)}/messages`);
    } catch {
      return;
    }
    if (openDetailKey !== key) return;
    $("#messages-list").replaceChildren(...(rows.length ? rows.map((m) => el("li", { class: m.outgoing ? "out" : "in" },
      el("p", { class: "msg-who" }, m.outgoing ? `To ${m.to_kind === "session" ? "a session" : m.to_kind === "all" ? "everyone" : "a machine"}` : `From ${m.from}`,
        ` · ${ago(m.created)}`, !m.outgoing && !m.delivered ? " · not delivered yet" : ""),
      el("p", { class: "msg-text" }, m.text),
      m.ref ? el("p", { class: "msg-ref" }, m.ref) : null))
      : [el("li", { class: "quiet-ink" }, "No messages. Agents write with deck_send; you can write below.")]));
  }, 250);
}

$("#message-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const [src, sid] = openDetailKey.split("/");
  try {
    await api(`/api/sessions/${src}/${encodeURIComponent(sid)}/messages`, { method: "POST", body: JSON.stringify({ text: $("#message-input").value }) });
    $("#message-input").value = "";
    toast("Sent. It reaches the session with its next prompt or reply.");
    loadThread(openDetailKey);
  } catch (err) {
    toast(`Could not send it: ${err.message}`);
  }
});

$("#drawer-room-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const room = roomById($("#drawer-room-select").value);
  if (room) addToRoom(room, openDetailKey);
});

let focusAfterClose = null; // a selector the next close should focus instead of the tile
$("#drawer").addEventListener("close", () => {
  // The tile that opened the panel may have been rebuilt since: focus its current copy.
  const key = openDetailKey;
  openDetailKey = null;
  render();
  const target = focusAfterClose ? document.querySelector(focusAfterClose) : document.querySelector(`[data-focus="${CSS.escape(`tile:${key}`)}"]`);
  focusAfterClose = null;
  target?.scrollIntoView({ block: "center" });
  target?.focus({ preventScroll: true });
});

$("#drawer").addEventListener("keydown", (e) => {
  if (!["ArrowLeft", "ArrowRight"].includes(e.key) || e.target.closest("input")) return;
  const order = tileOrder();
  const at = order.indexOf(openDetailKey);
  if (at < 0 || order.length < 2) return;
  e.preventDefault();
  openDetail(order[(at + (e.key === "ArrowRight" ? 1 : order.length - 1)) % order.length]);
});

$("#drawer-ask-go").addEventListener("click", () => {
  const s = board.sessions.find((x) => keyOf(x) === openDetailKey);
  if (currentPage() !== "board") history.replaceState(null, "", "#/"), showPage();
  // the card's first control: Allow (or the command) for a permission, the first option for a question
  focusAfterClose = s?.pending
    ? `#needs-list [data-focus$="${CSS.escape(`:${s.pending.id}`)}"], #needs-list [data-focus^="${CSS.escape(`q:${s.pending.id}:`)}"]`
    : "#needs-title";
  $("#drawer").close();
});

$("#rename-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const [src, sid] = openDetailKey.split("/");
  try {
    await api(`/api/sessions/${src}/${encodeURIComponent(sid)}`, { method: "PATCH", body: JSON.stringify({ title: $("#rename-input").value }) });
    toast("Renamed.");
  } catch (err) {
    toast(`Could not rename it: ${err.message}`);
  }
});

$("#drawer-seen").addEventListener("click", async () => {
  const [src, sid] = openDetailKey.split("/");
  try {
    await api(`/api/sessions/${src}/${encodeURIComponent(sid)}/seen`, { method: "POST" });
    $("#drawer").close();
  } catch (err) {
    toast(`Could not mark it: ${err.message}`);
  }
});

$("#drawer-forget").addEventListener("click", async () => {
  const key = openDetailKey;
  const ok = await confirmDialog("Forget this session?", "It leaves the board until it sends a new event.", "Forget");
  if (!ok) return;
  const [src, sid] = key.split("/");
  try {
    await api(`/api/sessions/${src}/${encodeURIComponent(sid)}`, { method: "DELETE" });
    $("#drawer").close();
    toast("Session forgotten.");
  } catch (err) {
    toast(`Could not forget it: ${err.message}`);
  }
});

function confirmDialog(title, text, action) {
  const d = $("#confirm-dialog");
  $("#confirm-title").textContent = title;
  $("#confirm-text").textContent = text;
  $("#confirm-ok").textContent = action;
  d.returnValue = "";
  d.showModal();
  return new Promise((resolve) => d.addEventListener("close", () => resolve(d.returnValue === "confirm"), { once: true }));
}

// ---- rooms ----------------------------------------------------------------------------

const memberKey = (m) => `${m.source_id}/${m.session_id}`;
const roomById = (id) => board.rooms.find((r) => r.id === id);
const openRooms = () => board.rooms.filter((r) => r.closed == null);
function currentRoomId() {
  const [page, id] = location.hash.replace(/^#\/?/, "").split("/");
  return page === "rooms" && id ? decodeURIComponent(id) : null;
}

// Options for a <select>, keeping the choice when the list is rebuilt.
function fillSelect(select, options) {
  const keep = select.value;
  select.replaceChildren(...options.map(([value, text]) => el("option", { value }, text)));
  if (options.some(([value]) => value === keep)) select.value = keep;
}

function sessionLabel(key) {
  const s = board.sessions.find((x) => keyOf(x) === key);
  const src = s && board.sources.find((x) => x.id === s.source_id);
  return s ? `${nameOf(s)} · ${src?.name ?? "?"}` : "a session that left the board";
}

async function addToRoom(room, key) {
  if (room.members.some((m) => memberKey(m) === key)) return toast(`Already in ${room.name}.`);
  const [src, sid] = key.split("/");
  try {
    await api(`/api/rooms/${room.id}/members/${src}/${encodeURIComponent(sid)}`, { method: "PUT" });
    toast(`${sessionLabel(key)} added to ${room.name}. It gets a notice with the other members.`);
  } catch (err) {
    toast(`Could not add it: ${err.message}`);
  }
}

// Drag and drop: a critter onto a room chip on the Board.
let dragKey = null;
function startDrag(e, s) {
  dragKey = keyOf(s);
  e.dataTransfer.setData("text/plain", nameOf(s));
  e.dataTransfer.effectAllowed = "copy";
  e.currentTarget.classList.add("drag-source");
  document.documentElement.classList.add("dragging");
  markMembers();
}
// Rooms the dragged critter is already in take no drop (and look it).
function markMembers() {
  for (const t of document.querySelectorAll("a.room-tile")) {
    const room = roomById(t.dataset.focus.slice(5));
    t.classList.toggle("drop-member", Boolean(dragKey && room?.members.some((m) => memberKey(m) === dragKey)));
  }
}
function endDrag() {
  dragKey = null;
  document.documentElement.classList.remove("dragging");
  for (const t of document.querySelectorAll(".drop-over, .drop-member, .drag-source")) t.classList.remove("drop-over", "drop-member", "drag-source");
}
function dropTarget(node, roomId) {
  node.addEventListener("dragover", (e) => {
    if (!dragKey || node.classList.contains("drop-member")) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    node.classList.add("drop-over");
  });
  node.addEventListener("dragleave", () => node.classList.remove("drop-over"));
  node.addEventListener("drop", (e) => {
    e.preventDefault();
    const key = dragKey;
    endDrag();
    const room = roomById(roomId);
    if (key && room && room.closed == null) addToRoom(room, key);
  });
}

// The Rooms zone of the Board: a tile per open room, with its members as little critters in their
// machine's colour, and a tile to make a new one.
function memberCritters(members, max) {
  const shown = members.slice(0, max).map((m) => {
    const s = board.sessions.find((x) => keyOf(x) === memberKey(m));
    const src = board.sources.find((x) => x.id === m.source_id);
    return el("span", { class: `mini${!s || s.status === "ended" ? " gone" : ""}`, title: s ? `${nameOf(s)} · ${src?.name ?? "?"}` : "" },
      critter(s?.status ?? "ended", src?.color ?? "#888888", "", s?.folder ?? "", false));
  });
  return members.length > max ? [...shown, el("span", { class: "mini-more" }, `+${members.length - max}`)] : shown;
}

let newRoomDraft = "";
function renderStrip(visible) {
  const rooms = openRooms();
  $("#nav-rooms").textContent = rooms.length;
  $("#nav-rooms").hidden = !rooms.length;
  $("#rooms-zone").hidden = !board.sources.length;
  $("#zone-count").textContent = rooms.length;
  const focused = document.activeElement?.id === "new-room-name";
  const tiles = rooms.map((r) => {
    const n = r.members.length;
    const names = r.members.map((m) => sessionLabel(memberKey(m))).join("; ");
    const tileLink = el("a", { class: "room-tile", href: `#/rooms/${r.id}`, "data-focus": `room:${r.id}` },
      el("span", { class: "room-tile-head" }, el("span", { class: "room-hash", "aria-hidden": "true" }, "#"),
        el("span", { class: "room-tile-name" }, r.name)),
      el("span", { class: "room-tile-crowd", "aria-hidden": "true" }, ...(n ? memberCritters(r.members, 8)
        : [el("span", { class: "room-tile-empty" }, el("span", { class: "fine-only" }, "Empty: drop critters here"),
            el("span", { class: "coarse-only" }, "Empty: open a critter → Add to room"))])),
      el("span", { class: "room-tile-meta" }, `${n} member${n === 1 ? "" : "s"}`,
        r.last ? ` · last message ${ago(r.last)}` : " · no messages yet",
        el("span", { class: "visually-hidden" }, `${n ? `. Members: ${names}` : ""}. Drop a critter here to add it to the room.`)));
    dropTarget(tileLink, r.id);
    return el("li", {}, tileLink);
  });
  const input = el("input", { id: "new-room-name", maxlength: "40", required: true, placeholder: "Room name", autocomplete: "off",
    value: newRoomDraft, oninput: (e) => { newRoomDraft = e.target.value; } });
  const form = el("form", { class: "room-tile new-room", onsubmit: createRoom },
    el("label", { class: "label", for: "new-room-name" }, "New room"),
    el("span", { class: "rename-row" }, input, el("button", { type: "submit", class: "btn primary" }, "Create")));
  $("#room-tiles").replaceChildren(...tiles, el("li", {}, form));
  if (dragKey) markMembers();
  if (focused) { input.focus({ preventScroll: true }); input.setSelectionRange(input.value.length, input.value.length); }
}

async function createRoom(e) {
  e.preventDefault();
  try {
    const room = await api("/api/rooms", { method: "POST", body: JSON.stringify({ name: newRoomDraft }) });
    newRoomDraft = "";
    e.target.reset();
    toast(`Room ${room.name} created. Add critters to it.`);
    render();
  } catch (err) {
    toast(`Could not create it: ${err.message}`);
  }
}

function roomRow(r) {
  return el("li", { class: "row" },
    el("span", { class: "room-hash", "aria-hidden": "true" }, "#"),
    el("div", { class: "row-text" },
      el("a", { class: "row-title", href: `#/rooms/${r.id}` }, r.name),
      el("p", { class: "row-sub" }, `${r.members.length} member${r.members.length === 1 ? "" : "s"} · ${r.messages} message${r.messages === 1 ? "" : "s"}`,
        r.last ? ` · last ${ago(r.last)}` : "", r.closed != null ? ` · closed ${ago(r.closed)}` : "")),
    el("span", { class: "room-tile-crowd", "aria-hidden": "true" }, ...memberCritters(r.members, 8)));
}

function renderRooms(arrived) {
  const id = currentRoomId();
  const room = id && roomById(id);
  $("#rooms-index").hidden = Boolean(room);
  $("#room-view").hidden = !room;
  if (!room) {
    const open = openRooms();
    const closed = board.rooms.filter((r) => r.closed != null);
    $("#rooms-list").replaceChildren(...(open.length ? open.map(roomRow)
      : [el("li", { class: "row" }, el("p", { class: "quiet" }, "No rooms yet. Create one, then drag critters into it."))]));
    $("#rooms-closed").hidden = !closed.length;
    $("#rooms-closed-summary").textContent = `Closed (${closed.length})`;
    $("#rooms-closed-list").replaceChildren(...closed.map(roomRow));
    if (id && arrived && snapshots) toast("That room is gone.");
    return;
  }
  const closed = room.closed != null;
  $("#room-eyebrow").textContent = closed ? `Room · closed ${ago(room.closed)}` : "Room";
  $("#room-title").textContent = room.name;
  $("#room-closed-note").hidden = !closed;
  $("#room-paused-note").hidden = closed || !room.paused;
  for (const f of ["#room-post-form", "#room-add-form", "#room-rename-form", "#room-close"]) $(f).hidden = closed;
  if (document.activeElement !== $("#room-rename-input")) $("#room-rename-input").value = room.name;
  $("#room-member-count").textContent = room.members.length;
  const focused = document.activeElement?.dataset?.focus;
  $("#room-members").replaceChildren(...(room.members.length ? room.members.map((m) => {
    const key = memberKey(m);
    const s = board.sessions.find((x) => keyOf(x) === key);
    const src = board.sources.find((x) => x.id === m.source_id);
    return el("li", { class: `member${!s || s.status === "ended" ? " gone" : ""}` },
      el("span", { class: "art small" }, critter(s?.status ?? "ended", src?.color ?? "#888888", s?.tool ?? "", s?.folder ?? "", false)),
      el("span", { class: "member-text" },
        el("span", { class: "member-name" }, s ? nameOf(s) : "Left the board"),
        el("span", { class: "member-sub" }, `${src?.name ?? "?"} · ${s ? statusLine(s) : "gone"}`)),
      closed ? null : el("button", { type: "button", class: "icon-btn small", "data-focus": `rm:${key}`,
        "aria-label": `Remove ${s ? nameOf(s) : "this session"} from ${room.name}`, onclick: () => removeMember(room, m) }, "✕"));
  }) : [el("li", { class: "quiet drop-hint" }, closed ? "No members." : "No members yet. Drag critters onto this room on the Board, or add one below.")]));
  if (focused) document.querySelector(`[data-focus="${CSS.escape(focused)}"]`)?.focus({ preventScroll: true });
  const candidates = board.sessions.filter((s) => s.status !== "ended" && !room.members.some((m) => memberKey(m) === keyOf(s))
    && board.sources.some((x) => x.id === s.source_id));
  fillSelect($("#room-add-select"), candidates.map((s) => [keyOf(s), sessionLabel(keyOf(s))]));
  $("#room-add-form").querySelector("button").disabled = !candidates.length;
  if (!candidates.length) fillSelect($("#room-add-select"), [["", "No other live sessions"]]);
  if (roomShown.id !== room.id) {  // never show the previous room's chat while this one loads
    roomShown = { id: room.id, last: -1 };
    $("#room-messages").replaceChildren(el("li", { class: "quiet" }, "Loading…"));
  }
  // refetch only when the room has a new message (or on arrival): not on every board update
  const mark = `${room.messages}/${room.last}/${room.delivered}`;
  if (arrived || roomMark.id !== room.id || roomMark.mark !== mark) {
    roomMark = { id: room.id, mark };
    loadRoom(room.id, arrived);
  }
}
let roomMark = { id: null, mark: "" };

// The chat: fetched when the room opens and when the board changes.
let roomTimer;
let roomShown = { id: null, last: 0 };
function loadRoom(id, arrived) {
  clearTimeout(roomTimer);
  roomTimer = setTimeout(async () => {
    let rows;
    try {
      rows = await api(`/api/rooms/${id}/messages`);
    } catch {
      return;
    }
    const room = roomById(id);
    if (currentRoomId() !== id || !room) return;
    const lastId = rows.at(-1)?.id ?? 0;
    if (roomShown.id === id && roomShown.last === lastId && !arrived) return; // nothing new: keep the scroll
    const first = roomShown.last === -1;
    const fresh = !first && roomShown.id === id ? rows.filter((m) => m.id > roomShown.last && !m.from_board) : [];
    if (fresh.length) toast(fresh.length === 1 ? `New message in ${room.name}: ${fresh[0].from}` : `${fresh.length} new messages in ${room.name}`);
    const list = $("#room-messages");
    const atEnd = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
    roomShown = { id, last: lastId };
    $("#room-msg-count").textContent = rows.length;
    list.replaceChildren(...(rows.length ? rows.map((m) => {
      const [srcId, sid] = (m.from_id || ":").split(":");
      const s = board.sessions.find((x) => x.source_id === srcId && x.session_id === sid);
      const src = board.sources.find((x) => x.id === srcId);
      const who = m.from_board ? "You" : s ? nameOf(s) : m.from;
      const reach = m.recipients && (m.from_board || m.delivered < m.recipients)
        ? el("p", { class: "msg-reach", title: "A message reaches a session on its next turn, or when its wait wakes it." },
          `Delivered to ${m.delivered} of ${m.recipients}`) : null;
      return el("li", { class: m.from_board ? "mine" : "theirs", style: src ? { "--c": src.color } : null },
        el("span", { class: "chat-avatar", "aria-hidden": "true" },
          critter("awake", m.from_board ? "#ece8e0" : src?.color ?? "#888888", "", m.from_board ? "" : s?.folder ?? "", false)),
        el("div", { class: "chat-bubble" },
          el("p", { class: "msg-who" }, el("strong", {}, who), src ? el("span", { class: "chat-machine" }, src.name) : null,
            el("time", { datetime: new Date(m.created * 1000).toISOString(), title: new Date(m.created * 1000).toLocaleString() },
              clock(m.created))),
          el("p", { class: "msg-text" }, m.text),
          m.ref ? el("p", { class: "msg-ref" }, m.ref) : null,
          reach));
    }) : [el("li", { class: "quiet" }, "No messages yet. Members get what is posted from now on.")]));
    if (atEnd || arrived || first) list.scrollTop = list.scrollHeight;
    // on a phone the page scrolls, not the list: show the newest message above the composer
    if (first && matchMedia("(width < 820px)").matches) list.lastElementChild?.scrollIntoView({ block: "center" });
  }, 200);
}

// A message's time: the hour today, the day and hour before.
function clock(ts) {
  const d = new Date(ts * 1000);
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === new Date().toDateString() ? hm : `${d.toLocaleDateString([], { weekday: "short", day: "numeric" })} ${hm}`;
}

async function removeMember(room, m) {
  try {
    await api(`/api/rooms/${room.id}/members/${m.source_id}/${encodeURIComponent(m.session_id)}`, { method: "DELETE" });
    toast(`Removed from ${room.name}.`);
    $("#room-add-select").focus();
  } catch (err) {
    toast(`Could not remove it: ${err.message}`);
  }
}

$("#room-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#room-error").textContent = "";
  try {
    const room = await api("/api/rooms", { method: "POST", body: JSON.stringify({ name: $("#room-name").value }) });
    $("#room-name").value = "";
    location.hash = `#/rooms/${room.id}`;
  } catch (err) {
    $("#room-error").textContent = err.message;
  }
});

$("#room-add-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const room = roomById(currentRoomId());
  if (room && $("#room-add-select").value) addToRoom(room, $("#room-add-select").value);
});

$("#room-rename-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api(`/api/rooms/${currentRoomId()}`, { method: "PATCH", body: JSON.stringify({ name: $("#room-rename-input").value }) });
    toast("Renamed.");
  } catch (err) {
    toast(`Could not rename it: ${err.message}`);
  }
});

// The whole chat as a Markdown file: author, machine and time of each message, oldest first.
$("#room-export").addEventListener("click", async () => {
  const room = roomById(currentRoomId());
  if (!room) return;
  let rows = [];
  try {
    for (let before = ""; ; ) {
      const page = await api(`/api/rooms/${room.id}/messages${before}`);
      rows = [...page, ...rows];
      if (page.length < 50) break;
      before = `?before=${page[0].id}`;
    }
  } catch (err) {
    toast(`Could not export it: ${err.message}`);
    return;
  }
  const lines = [`# ${room.name}`, "", `Exported ${new Date().toLocaleString()} · ${rows.length} messages`, ""];
  for (const m of rows) {
    const [srcId, sid] = (m.from_id || ":").split(":");
    const s = board.sessions.find((x) => x.source_id === srcId && x.session_id === sid);
    const machine = board.sources.find((x) => x.id === srcId)?.name;
    const who = m.from_board ? "You (board)" : s ? nameOf(s) : m.from;
    lines.push(`## ${who}${machine ? ` · ${machine}` : ""} · ${new Date(m.created * 1000).toLocaleString()}`, "", m.text, "");
    if (m.ref) lines.push(`Ref: ${m.ref}`, "");
  }
  const link = el("a", { href: URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/markdown" })),
    download: `${room.name.replace(/[^\p{L}\p{N}]+/gu, "-").replace(/^-|-$/g, "") || "room"}.md` });
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
});

$("#room-close").addEventListener("click", async () => {
  const room = roomById(currentRoomId());
  if (!room || !(await confirmDialog(`Close ${room.name}?`, "Nobody can post in it any more. It stays readable here for a week, then it is deleted.", "Close room"))) return;
  try {
    await api(`/api/rooms/${room.id}`, { method: "DELETE" });
    toast("Room closed.");
  } catch (err) {
    toast(`Could not close it: ${err.message}`);
  }
});

$("#room-post-input").addEventListener("keydown", (e) => {
  // Enter sends with a keyboard; on a touch keyboard (no Shift) Enter is a new line and Send is the button
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && matchMedia("(pointer: fine)").matches) {
    e.preventDefault();
    $("#room-post-form").requestSubmit();
  }
});

$("#room-post-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#room-post-input");
  if (!input.value.trim()) return;
  try {
    await api(`/api/rooms/${currentRoomId()}/messages`, { method: "POST", body: JSON.stringify({ text: input.value }) });
    input.value = "";
    loadRoom(currentRoomId(), true);
  } catch (err) {
    toast(`Could not send it: ${err.message}`);
  }
});

// ---- sources -------------------------------------------------------------------------

function renderSources() {
  const focused = document.activeElement?.closest?.("#sources-list") ? document.activeElement.dataset.focus : null;
  const list = board.sources.map((src) => {
    const mine = board.sessions.filter((s) => s.source_id === src.id);
    const live = mine.filter((s) => s.status !== "ended").length;
    const last = Math.max(0, ...mine.map((s) => s.updated));
    return el(
      "li",
      { class: "row" },
      el("span", { class: "icon-box", "aria-hidden": "true" }, el("span", { class: "swatch", style: { "--c": src.color } })),
      el("div", { class: "row-text" },
        el("p", { class: "row-title" }, src.name),
        el("p", { class: "row-sub" }, `${live} live · ${mine.length - live} ended · ${last ? `last event ${ago(last)}` : "no events yet"}`)),
      el("button", {
        type: "button",
        class: "btn",
        "data-focus": `del:${src.id}`,
        "aria-label": `Delete source ${src.name}`,
        onclick: async () => {
          const ok = await confirmDialog(`Delete “${src.name}”?`, "Its token stops working and its sessions leave the board.", "Delete");
          if (!ok) return;
          try {
            await api(`/api/sources/${src.id}`, { method: "DELETE" });
            board.sources = board.sources.filter((x) => x.id !== src.id); // the stream catches up just after
            renderSources();
            $("#sources-title").focus();
            toast("Source deleted.");
          } catch (err) {
            toast(`Could not delete it: ${err.message}`);
          }
        },
      }, "Delete"),
    );
  });
  $("#sources-list").replaceChildren(...(list.length ? list : [el("li", { class: "row quiet" }, "No sources yet. Add the first one below.")]));
  if (focused) document.querySelector(`[data-focus="${CSS.escape(focused)}"]`)?.focus();
  // a new source gets a colour no other source has, by default
  const input = $("#source-form [name=color]");
  if (!colorTouched) { // never overwrite a colour the person picked
    const used = new Set(board.sources.map((x) => x.color.toLowerCase()));
    input.value = PALETTE.find((c) => !used.has(c)) ?? input.value;
  }
}

let colorTouched = false;
$("#source-form [name=color]").addEventListener("input", () => { colorTouched = true; });

// Source colours: distinct from each other and from the alert orange.
const PALETTE = ["#4f8cff", "#3fbf7f", "#c77dff", "#f2c94c", "#2ec4c4", "#ff7eb6", "#8bd450", "#b0a99a"];

$("#source-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  try {
    const created = await api("/api/sources", { method: "POST", body: JSON.stringify(data) });
    if (!board.sources.some((x) => x.id === created.source.id)) board.sources = [...board.sources, created.source]; // the stream may be first
    renderSources();
    $("#created-name").textContent = created.source.name;
    $("#source-snippet").textContent = JSON.stringify(created.settings, null, 2);
    $("#install-snippet").textContent = created.install.join("\n");
    $("#install-block").hidden = !created.install.length;
    // Without a plugin marketplace configured, the manual block is the only way: show it open.
    $("#manual-block").open = !created.install.length;
    $("#source-insecure").hidden = !created.insecure;
    $("#source-no-public-url").hidden = !created.no_public_url;
    $("#source-created").hidden = false;
    $("#source-error").textContent = "";
    form.reset();
    colorTouched = false;
    renderSources();
    $("#created-title").focus();
  } catch (err) {
    $("#source-error").textContent = err.message;
  }
});

for (const [button, block] of [["#copy-snippet", "#source-snippet"], ["#copy-install", "#install-snippet"]]) {
  $(button).addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($(block).textContent);
      toast("Copied.");
    } catch {
      toast("Copy failed: select the text and copy it by hand.");
    }
  });
}

// ---- settings: kept in this browser ------------------------------------------------------

const prefs = { notify: "never", sound: false, motion: true };
try {
  const saved = JSON.parse(localStorage.getItem("crittersdeck-prefs") || "{}");
  for (const k of Object.keys(prefs)) if (typeof saved?.[k] === typeof prefs[k]) prefs[k] = saved[k];
  if (typeof saved?.motion !== "boolean" && localStorage.getItem("crittersdeck-motion") === "paused") prefs.motion = false; // from 0.1
} catch {
  /* storage unavailable: defaults for this page */
}
function savePrefs() {
  try {
    localStorage.setItem("crittersdeck-prefs", JSON.stringify(prefs));
  } catch {
    /* the choice lasts for this page only */
  }
}

function renderSettings() {
  for (const b of document.querySelectorAll("[data-notify]")) {
    b.setAttribute("aria-checked", String(b.dataset.notify === prefs.notify));
    b.tabIndex = b.dataset.notify === prefs.notify ? 0 : -1;
  }
  $("#sound-switch").setAttribute("aria-checked", String(prefs.sound));
  $("#motion-switch").setAttribute("aria-checked", String(prefs.motion));
  const permission = "Notification" in window ? Notification.permission : "unsupported";
  $("#notify-sub").textContent = permission === "unsupported"
    ? "This browser cannot show notifications."
    : permission === "denied"
      ? "Notifications are blocked for this site: allow them in the browser's site settings."
      : "A desktop notification from the browser, even when this tab is in the background.";
  $("#idle-value").textContent = me ? `${me.idle_minutes} min` : "—";
  $("#account-title").textContent = me ? `Signed in as ${me.user}` : "Signed in";
  $("#sign-out").hidden = !me?.password_login;
}

async function setNotify(value) {
  if (value !== "never" && "Notification" in window && Notification.permission === "default") {
    await Notification.requestPermission();
  }
  prefs.notify = value;
  savePrefs();
  renderSettings();
}

$(".segmented").addEventListener("click", (e) => {
  const b = e.target.closest("[data-notify]");
  if (b) setNotify(b.dataset.notify);
});
$(".segmented").addEventListener("keydown", (e) => {
  if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)) return;
  e.preventDefault();
  const options = [...document.querySelectorAll("[data-notify]")];
  const at = options.findIndex((b) => b.dataset.notify === prefs.notify);
  const next = options[(at + (e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : options.length - 1)) % options.length];
  setNotify(next.dataset.notify).then(() => next.focus());
});
$("#sound-switch").addEventListener("click", () => {
  prefs.sound = !prefs.sound;
  savePrefs();
  renderSettings();
  if (prefs.sound) chime();
});
$("#motion-switch").addEventListener("click", () => {
  prefs.motion = !prefs.motion;
  savePrefs();
  applyMotion();
  renderSettings();
});
$("#notify-test").addEventListener("click", () => {
  if (!("Notification" in window) || Notification.permission !== "granted") {
    toast("Allow notifications first: pick “When one needs me”.");
    return;
  }
  showNotification("crittersdeck", "This is how a session that needs you will look.", "test");
});

function applyMotion() {
  document.documentElement.classList.toggle("motion-paused", !prefs.motion);
}

// Notifications: one per request or finished session, never repeated.
const told = new Set();
let firstSnapshot = true;
let snapshots = 0; // what was already there when the page loaded is not news
function notify(visible, sources) {
  if (!snapshots) return;
  const fresh = [];
  const now = new Set();
  for (const s of visible) {
    // one per request, or one per stretch of asking or of being done: not one per event
    const key = s.status === "needs_you" ? `ask:${keyOf(s)}:${s.pending?.id ?? "session"}`
      : s.status === "done_unseen" ? `done:${keyOf(s)}` : null;
    if (!key) continue;
    now.add(key);
    if (told.has(key)) continue;
    told.add(key);
    if (!firstSnapshot) fresh.push([s, key]);
  }
  for (const key of told) if (!now.has(key)) told.delete(key); // left that state: may tell again next time
  firstSnapshot = false;
  if (prefs.notify === "never" || !("Notification" in window) || Notification.permission !== "granted") return;
  for (const [s, key] of fresh) {
    if (s.status === "done_unseen" && prefs.notify !== "done") continue;
    const src = sources.get(s.source_id)?.name ?? "";
    const what = s.status === "done_unseen" ? "finished"
      : s.pending?.questions ? "has a question" : s.pending ? `wants to use ${s.pending.tool || "a tool"}` : "needs you";
    showNotification(`${nameOf(s)} · ${src}`, `It ${what}.`, key, keyOf(s));
  }
}

function showNotification(title, body, tag, key) {
  try {
    const n = new Notification(title, { body, tag, icon: "favicon.svg" });
    n.onclick = () => {
      window.focus();
      if (key) {
        location.hash = "#/";
        openDetail(key);
      }
      n.close();
    };
    if (prefs.sound) chime();
  } catch {
    /* some browsers only allow notifications from a service worker */
  }
}

let audio;
function chime() {
  try {
    audio ??= new AudioContext();
    const now = audio.currentTime;
    for (const [i, freq] of [880, 1320].entries()) {
      const osc = audio.createOscillator();
      const gain = audio.createGain();
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, now + i * 0.12);
      gain.gain.exponentialRampToValueAtTime(0.15, now + i * 0.12 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + i * 0.12 + 0.25);
      osc.connect(gain).connect(audio.destination);
      osc.start(now + i * 0.12);
      osc.stop(now + i * 0.12 + 0.3);
    }
  } catch {
    /* no audio */
  }
}

// ---- about ---------------------------------------------------------------------------

function renderAbout() {
  $("#about-version").textContent = me ? `v${me.version}` : "";
  const facts = [
    ["Version", me?.version ?? "—"],
    ["Ingest address", me?.public_url || "this board's own address"],
    ["Plugin marketplace", me?.plugin_marketplace || "not set: machines are connected by hand"],
    ["Sources", String(board.sources.length)],
    ["Sessions", String(board.sessions.length)],
  ];
  $("#about-facts").replaceChildren(...facts.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));
}

// ---- live updates, sign-in -----------------------------------------------------------------

function connect() {
  events?.close();
  events = new EventSource("/api/events");
  events.onopen = () => setLink("");
  setLink("connecting…");
  events.onmessage = (msg) => {
    board = JSON.parse(msg.data);
    snapshots += 1;
    render();
    if (currentPage() === "about") renderAbout();
  };
  events.onerror = async () => {
    setLink("reconnecting…");
    const closed = events.readyState === EventSource.CLOSED;
    try {
      const res = await fetch("/api/me");
      if (res.status === 401) return showLogin();
    } catch {
      /* server unreachable */
    }
    // EventSource gives up after an HTTP error (a proxy's 502 while the board restarts): retry by hand.
    if (closed) setTimeout(() => events?.readyState === EventSource.CLOSED && connect(), 5000);
  };
}

function setLink(text) {
  $("#link-state").textContent = text || "live";
  $("#link-state").className = `link ${text ? "down" : "ok"}`;
  // While the live stream is down the board may be stale: dim it and stop answers.
  linkDown = Boolean(text);
  document.documentElement.classList.toggle("link-down", linkDown);
  render();
}

function showLogin() {
  events?.close();
  events = null;
  if ($("#drawer").open) $("#drawer").close();
  $("#app").hidden = true;
  $("#login-view").hidden = false;
  $("#password").focus();
}

function showBoard(who) {
  me = who;
  csrf = who.csrf;
  $("#login-view").hidden = true;
  $("#app").hidden = false;
  showPage();
  connect();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const button = e.currentTarget.querySelector("button[type=submit]");
  const input = $("#password");
  $("#login-error").textContent = "";
  input.removeAttribute("aria-invalid");
  button.disabled = true;
  button.textContent = "Signing in…";
  let res;
  try {
    res = await fetch("/api/login", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ password: input.value }) });
  } catch {
    res = null;
  } finally {
    button.disabled = false;
    button.textContent = "Sign in";
  }
  if (!res?.ok) {
    $("#login-error").textContent = res?.status === 401 ? "Wrong password." : "Sign-in failed. Is the board running?";
    input.setAttribute("aria-invalid", "true");
    input.select();
    return;
  }
  input.value = "";
  const who = await res.json();
  // /api/me has the board's settings too.
  const full = await fetch("/api/me").then((r) => (r.ok ? r.json() : who)).catch(() => who);
  showBoard(full);
});

$("#sign-out").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  showLogin();
});

document.addEventListener("visibilitychange", () => document.documentElement.classList.toggle("page-hidden", document.hidden));

// Relative times age without events.
setInterval(() => board.sessions.length && render(), 30_000);

(async function start() {
  applyMotion();
  // The board may boot before the server (a wall display): keep retrying until it answers.
  for (let wait = 2000; ; wait = Math.min(wait * 2, 30_000)) {
    try {
      const res = await fetch("/api/me");
      return res.ok ? showBoard(await res.json()) : showLogin();
    } catch {
      $("#login-view").hidden = false;
      $("#login-error").textContent = "Can't reach the board. Retrying…";
      await new Promise((resolve) => setTimeout(resolve, wait));
    }
  }
})();
