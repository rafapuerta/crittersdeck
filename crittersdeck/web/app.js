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
let board = { now: 0, sources: [], sessions: [] };
let events = null;
let openDetailKey = null;
let linkDown = false;
const openEnded = new Set(); // "Ended" lists the viewer opened, kept open across re-renders

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

function statusLine(s) {
  if (s.status === "working") return ACTIVITY[signFor("working", s.tool)[0]] + (s.tool ? ` · ${s.tool}` : "");
  if (s.status === "needs_you") return s.pending ? `allow ${s.pending.tool || "a tool"}?` : "has a question";
  if (s.status === "done_unseen") return `done · ${ago(s.updated)}`; // the stripes say "not seen"
  if (s.status === "idle") return `idle · ${ago(s.updated)}`;
  if (s.status === "ended") return `ended · ${ago(s.updated)}`;
  return STATUS_TEXT[s.status];
}

// ---- rendering ----------------------------------------------------------------

function render() {
  const sources = new Map(board.sources.map((src) => [src.id, src]));
  const visible = board.sessions.filter((s) => sources.has(s.source_id));

  // Needs you
  // Requests the board can answer first, then questions that must be answered in the session.
  // Stable order, so cards do not jump under a finger: requests the board can answer, oldest
  // first, then questions that must be answered in the session.
  const asking = visible
    .filter((s) => s.status === "needs_you")
    .sort((a, b) => (b.pending ? 1 : 0) - (a.pending ? 1 : 0) || (a.pending?.created ?? a.started) - (b.pending?.created ?? b.started));
  $("#needs-count").textContent = linkDown ? "—" : asking.length;
  $("#needs-empty").hidden = asking.length > 0;
  $("#needs-empty").textContent = linkDown
    ? "Not connected, so this list may be out of date."
    : "Nothing needs you. When an agent asks for permission or a question, it shows up here.";
  document.title = asking.length ? `(${asking.length}) crittersdeck` : "crittersdeck";
  const focused = document.activeElement?.dataset?.focus;
  $("#needs-list").replaceChildren(...asking.map((s) => needCard(s, sources.get(s.source_id))));

  // Deck, grouped by source
  $("#deck-empty").hidden = board.sources.length > 0;
  const groups = board.sources.map((src) => {
    const mine = visible.filter((s) => s.source_id === src.id && s.status !== "ended");
    const ended = visible.filter((s) => s.source_id === src.id && s.status === "ended");
    const endedKey = `ended:${src.id}`;
    return el(
      "section",
      { class: "group", "aria-labelledby": `g-${src.id}` },
      el("h3", { id: `g-${src.id}`, class: "group-title" },
        el("span", { class: "dot", style: { "--c": src.color }, "aria-hidden": "true" }), src.name,
        el("span", { class: "count" }, String(mine.length))),
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
  // Everything was rebuilt: put keyboard focus back where it was.
  if (focused) document.querySelector(`[data-focus="${CSS.escape(focused)}"]`)?.focus();

  if (openDetailKey) fillDetail();
}

function tile(s, src) {
  return el(
    "li",
    {},
    el(
      "button",
      {
        type: "button",
        class: `tile s-${s.status}`,
        style: { "--c": src.color },
        "data-focus": `tile:${keyOf(s)}`,
        onclick: () => openDetail(keyOf(s)),
      },
      el("span", { class: "corner", "aria-hidden": "true" }),
      el("span", { class: "art" }, critter(s.status, src.color, s.tool, s.folder)),
      el("span", { class: "tile-name" }, nameOf(s), el("span", { class: "visually-hidden" }, `, ${src.name},`)),
      el("span", { class: "tile-place" }, placeOf(s)),
      el("span", { class: "tile-status" }, ...tileStatus(s)),
    ),
  );
}

function needCard(s, src) {
  const p = s.pending;
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
      p ? null : el("p", { class: "small" }, "Answer it in the session; the board cannot answer questions."),
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

async function answer(id, decision) {
  try {
    await api(`/api/approvals/${encodeURIComponent(id)}`, { method: "POST", body: JSON.stringify({ decision }) });
    toast(decision === "allow" ? "Allowed. The agent carries on." : "Denied. The agent was told no.");
  } catch (err) {
    toast(err.status === 409 ? "Already answered in the session." : `Could not send the answer: ${err.message}`);
  }
  const next = [...document.querySelectorAll("#needs-list .need-actions .btn")].find((b) => !b.dataset.focus.endsWith(`:${id}`));
  (next || $("#needs-title")).focus();
}

// ---- detail dialog --------------------------------------------------------------

function openDetail(key) {
  openDetailKey = key;
  fillDetail();
  $("#detail-dialog").showModal();
}

function fillDetail() {
  const s = board.sessions.find((x) => keyOf(x) === openDetailKey);
  const dialog = $("#detail-dialog");
  if (!s) {
    if (dialog.open) dialog.close();
    return;
  }
  const src = board.sources.find((x) => x.id === s.source_id);
  $("#detail-title").textContent = nameOf(s);
  if (document.activeElement !== $("#rename-input")) $("#rename-input").value = s.title || "";
  const facts = [
    ["Status", statusLine(s)],
    ["Source", src?.name ?? "?"],
    ["Where", placeOf(s)],
    ["Last tool", s.tool || "none"],
    ["Doing", s.detail || "nothing reported"],
    ["Started", ago(s.started)],
    ["Last event", ago(s.updated)],
    ["Session id", s.session_id],
  ];
  $("#detail-body").replaceChildren(...facts.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));
  $("#detail-seen").hidden = s.status !== "done_unseen";
}

$("#detail-dialog").addEventListener("close", () => {
  // The tile that opened the dialog may have been rebuilt since: focus its current copy.
  const key = openDetailKey;
  openDetailKey = null;
  document.querySelector(`[data-focus="${CSS.escape(`tile:${key}`)}"]`)?.focus();
});

$("#rename-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const [src, sid] = openDetailKey.split("/");
  try {
    await api(`/api/sessions/${src}/${encodeURIComponent(sid)}`, {
      method: "PATCH",
      body: JSON.stringify({ title: $("#rename-input").value }),
    });
    toast("Renamed.");
  } catch (err) {
    toast(`Could not rename it: ${err.message}`);
  }
});

$("#detail-seen").addEventListener("click", async () => {
  const [src, sid] = openDetailKey.split("/");
  try {
    await api(`/api/sessions/${src}/${encodeURIComponent(sid)}/seen`, { method: "POST" });
    $("#detail-dialog").close();
  } catch (err) {
    toast(`Could not mark it: ${err.message}`);
  }
});

$("#detail-forget").addEventListener("click", async () => {
  const key = openDetailKey;
  const ok = await confirmDialog("Forget this session?", "It leaves the board until it sends a new event.", "Forget");
  if (!ok) return;
  const [src, sid] = key.split("/");
  try {
    await api(`/api/sessions/${src}/${encodeURIComponent(sid)}`, { method: "DELETE" });
    $("#detail-dialog").close();
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

// ---- sources dialog -----------------------------------------------------------------

function renderSources() {
  const list = board.sources.map((src) =>
    el(
      "li",
      { class: "source-row" },
      el("span", { class: "dot", style: { "--c": src.color }, "aria-hidden": "true" }),
      el("span", { class: "grow" }, src.name),
      el("button", {
        type: "button",
        class: "btn ghost",
        "aria-label": `Delete source ${src.name}`,
        onclick: async () => {
          const ok = await confirmDialog(`Delete “${src.name}”?`, "Its token stops working and its sessions leave the board.", "Delete");
          if (!ok) return;
          try {
            await api(`/api/sources/${src.id}`, { method: "DELETE" });
            toast("Source deleted.");
          } catch (err) {
            toast(`Could not delete it: ${err.message}`);
          }
        },
      }, "Delete"),
    ),
  );
  $("#sources-list").replaceChildren(...(list.length ? list : [el("li", { class: "quiet" }, "No sources yet.")]));
}

function openSources() {
  renderSources();
  $("#source-created").hidden = true;
  $("#source-error").textContent = "";
  $("#sources-dialog").showModal();
}
$("#open-sources").addEventListener("click", openSources);
$("#empty-add").addEventListener("click", openSources);

$("#source-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  try {
    const created = await api("/api/sources", { method: "POST", body: JSON.stringify(data) });
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

// ---- live updates, sign-in, motion ------------------------------------------------------

function connect() {
  events?.close();
  events = new EventSource("/api/events");
  events.onopen = () => setLink("");
  setLink("Connecting…");
  events.onmessage = (msg) => {
    board = JSON.parse(msg.data);
    render();
    if ($("#sources-dialog").open) renderSources();
  };
  events.onerror = async () => {
    setLink("Connection lost. Reconnecting…");
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
  $("#link-state").textContent = text;
  $("#link-state").hidden = !text;
  // While the live stream is down the board may be stale: dim it and stop answers.
  linkDown = Boolean(text);
  document.documentElement.classList.toggle("link-down", linkDown);
  render();
}

function showLogin() {
  events?.close();
  events = null;
  $("#board-view").hidden = true;
  $("#top-actions").hidden = true;
  $("#login-view").hidden = false;
  setLink("");
  $("#password").focus();
}

function showBoard(me) {
  csrf = me.csrf;
  $("#login-view").hidden = true;
  $("#board-view").hidden = false;
  $("#top-actions").hidden = false;
  $("#sign-out").hidden = !me.password_login;
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
    res = await fetch("/api/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ password: input.value }),
    });
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
  showBoard(await res.json());
});

$("#sign-out").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  showLogin();
});

function setMotion(paused) {
  document.documentElement.classList.toggle("motion-paused", paused);
  $("#motion-toggle").replaceChildren(paused ? "Play" : "Pause", el("span", { class: "wide-only" }, " motion"));
  try {
    localStorage.setItem("crittersdeck-motion", paused ? "paused" : "on");
  } catch {
    /* storage unavailable: the choice lasts for this page only */
  }
}
$("#motion-toggle").addEventListener("click", () =>
  setMotion(!document.documentElement.classList.contains("motion-paused")),
);
document.addEventListener("visibilitychange", () =>
  document.documentElement.classList.toggle("page-hidden", document.hidden),
);

// Relative times age without events.
setInterval(() => board.sessions.length && render(), 30_000);

(async function start() {
  let paused = false;
  try {
    paused = localStorage.getItem("crittersdeck-motion") === "paused";
  } catch {
    /* ignore */
  }
  setMotion(paused);
  // The board may boot before the server (a wall display): keep retrying until it answers.
  for (let wait = 2000; ; wait = Math.min(wait * 2, 30_000)) {
    try {
      const res = await fetch("/api/me");
      setLink("");
      return res.ok ? showBoard(await res.json()) : showLogin();
    } catch {
      setLink("Can't reach the board. Retrying…");
      await new Promise((resolve) => setTimeout(resolve, wait));
    }
  }
})();
