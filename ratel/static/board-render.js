/* ---- clan card ------------------------------------------------------ */
const TOKEN_KEY = "ratel.board.token";
const CLAN_LIMITS = { roles: 8, why: 160, skills: 8, skill: 40, model: 80, preset: 40, bytes: 2600 };
let clanRadioSeq = 0;   // radio groups are document-wide: every rendered card gets its own

function authHeaders() {
  return S.token ? { "Authorization": "Bearer " + S.token } : {};
}

function clanCatalog() {
  // lazy, once per page load: the same object `ratel clan catalog` prints.
  // A non-200 (an operator's half-saved models/presets/roles.toml) rejects,
  // so every consumer's null/catch path runs, and the next call retries.
  if (!S.catalog) S.catalog = fetch("/api/clan/catalog").then(r => {
    if (!r.ok) throw new Error("catalog unreadable");
    return r.json();
  }).catch(e => { S.catalog = null; throw e; });
  return S.catalog;
}

function storeToken(t) {
  S.token = t;
  try { if (t) localStorage.setItem(TOKEN_KEY, t); else localStorage.removeItem(TOKEN_KEY); } catch (e) {}
  refreshClanHeads();
}

// mirror of validate_clan_attachment: same rules, same wording; returns the
// first failing message or null
function clanValidate(att, catalog) {
  const roles = att.roles || [];
  if (!roles.length || roles.length > CLAN_LIMITS.roles)
    return "roles: must be a non-empty list of at most " + CLAN_LIMITS.roles + " roles";
  const seen = new Set();
  let writers = 0;
  const presets = ((catalog && catalog.presets) || []).map(p => p.id);
  for (const r of roles) {
    if (!r.name || seen.has(r.name))
      return "name: " + (seen.has(r.name) ? "duplicate role " + r.name : "every role needs a name");
    seen.add(r.name);
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(r.name))
      return "name: " + JSON.stringify(r.name) + " is not mentionable on the channel";
    if (r.name === "orchestrator")
      return "name: orchestrator is fixed at kickoff and never proposed";
    if (!r.preset || r.preset.length > CLAN_LIMITS.preset ||
        !/^[a-z0-9][a-z0-9-]*$/.test(r.preset))
      return "preset: " + JSON.stringify(r.preset) + " for " + r.name;
    if (catalog && !presets.includes(r.preset))
      return "preset: unknown preset " + JSON.stringify(r.preset) + " for " + r.name +
             " (known: " + presets.slice().sort().join(", ") + ")";
    if (r.writer) writers++;
    const skills = r.skills || [];
    if (skills.length > CLAN_LIMITS.skills || skills.some(s => !s || s.length > CLAN_LIMITS.skill))
      return "skills: " + JSON.stringify(skills) + " for " + r.name;
    if (r.why && r.why.length > CLAN_LIMITS.why)
      return "why: " + JSON.stringify(r.why) + " for " + r.name;
  }
  if (writers !== 1)
    return "writer: a clan needs exactly one writer, got " + writers;
  // exact mirror of the server: json.dumps(att, separators=(",", ":")) with
  // ensure_ascii — non-ASCII becomes \uXXXX (6 bytes), separators are compact
  const ascii = JSON.stringify(att).replace(/[^\x00-\x7F]/g, "\\u0000");
  if (new TextEncoder().encode(ascii).length > CLAN_LIMITS.bytes)
    return "bytes: attachment exceeds " + CLAN_LIMITS.bytes + " bytes";
  return null;
}

// ONE source of truth for whether a proposed card may be confirmed: it must
// be the channel's newest proposal, unapproved, with a token stored
function clanIsNewest(card) {
  return card.dataset.msg === S.newestProposal[S.channel] &&
         card.dataset.msg !== S.approvedProposal[S.channel];
}
function clanEditable(card) {
  return clanIsNewest(card) && !!S.token;
}

// the newest proposed clan message per channel drives every card's head:
// two open tabs converge over SSE. Head/hint tell the truth about bus state
// even without a token; only inputs and Confirm fold the token in.
function clanHeadText(card, status) {
  const trusted = status === "approved" ? "stakeholder" : "orchestrator";
  if ((card.dataset.author || "") !== trusted)
    return status + " \u00b7 ignored (not from " + trusted + ")";
  if (status !== "proposed") return "\u2713 " + status;
  if (card.dataset.msg === S.approvedProposal[S.channel]) return "✓ approved";
  return clanIsNewest(card) ? "proposed" : "superseded";
}

function refreshClanHeads() {
  document.querySelectorAll('.clancard[data-status="proposed"]').forEach(card => {
    const ignored = (card.dataset.author || "") !== "orchestrator";
    const isNewest = clanIsNewest(card);
    const editable = isNewest && !ignored && !!S.token;
    card.querySelector(".clanhead").textContent = clanHeadText(card, "proposed");
    card.querySelectorAll(".clanin").forEach(i => { i.disabled = !editable; });
    const btn = card.querySelector(".confirm");
    if (btn) btn.disabled = !editable || !!card._confirming;
    if (card._syncAdd) card._syncAdd();   // the blanket .clanin toggle ignores the roles cap
    // ...and it knows nothing about the edits either: re-enabling Confirm on
    // every arriving message would undo a live validation failure (drop the
    // writer's row and the next SSE frame would re-arm Confirm)
    if (btn && card._att) clanRevalidate(card);
    const hint = card.querySelector(".clanhint");
    if (hint) hint.textContent = isNewest && !editable && !ignored ?
      "open the board with the token URL to confirm" : "";
  });
}

/* ---- text ---------------------------------------------------------- */
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ESC[c]);

// Agent palette: 8 slots in fixed order, assigned per channel in first-seen
// order and persisted so a ninth agent can never re-map agents 1-8. Slots 9+
// wrap the palette; their left rules and dots draw dotted (class "alt").
const PALETTE = 8;
const slotCache = {};
function slotsFor(ch) {
  const key = "board.slotMap." + ch;
  if (!slotCache[ch]) {
    let m = {};
    try { m = JSON.parse(localStorage.getItem(key)) || {}; } catch (e) { m = {}; }
    slotCache[ch] = m;
  }
  return slotCache[ch];
}
function agentSlot(name) {
  const m = slotsFor(S.channel);
  if (!(name in m)) {
    const used = Object.values(m);
    let n = 1;
    while (used.includes(n)) n++;
    m[name] = n;
    try { localStorage.setItem("board.slotMap." + S.channel, JSON.stringify(m)); } catch (e) {}
  }
  return m[name];
}
function colour(name) {
  return "var(--a" + (((agentSlot(name) - 1) % PALETTE) + 1) + ")";
}
function chipBg(name) {
  return "var(--c" + (((agentSlot(name) - 1) % PALETTE) + 1) + ")";
}
function isAltCycle(name) {
  return Math.ceil(agentSlot(name) / PALETTE) > 1;
}

// Rendered fragments are parked behind <<B0>> / <<I0>> sentinels so later passes
// (bold, mentions, lists) never rewrite the inside of a code block or a link.
// The text is escaped first, so a literal "<" can never come from a message.
// Every block-level tag md() can emit. Newlines next to one are stripped, or a
// stray <br> lands between blocks; a new tag that is not in here gets a phantom
// blank line above and below it.
const MD_BLOCK = "ul|ol|li|h[1-6]|table|thead|tbody|tr|th|td|blockquote|div";
const MD_NL_BEFORE = new RegExp("\\n(</?(?:" + MD_BLOCK + ")\\b)", "g");
const MD_NL_AFTER = new RegExp("(</(?:" + MD_BLOCK + ")>)\\n", "g");
// ...and just inside an opening one: a blockquote's lines are bare text, so
// without this the quote opens with a stray <br>
const MD_NL_INSIDE = new RegExp("(<(?:" + MD_BLOCK + ")\\b[^>]*>)\\n", "g");
// a fixed set, chosen by the delimiter row's shape — never captured text, the
// same rule as the fence info string: this lands in an attribute
const MD_ALIGN = { ":-:": ' style="text-align:center"', "-:": ' style="text-align:right"',
                   ":-": ' style="text-align:left"' };
// the delimiter cell collapsed to its shape, then looked up; anything else — a
// plain `---`, or a row with more cells than the header — aligns by default
const mdAlign = c => MD_ALIGN[String(c).replace(/-+/, "-")] || "";

// the emphasis markers md() renders, removed for a plain-text summary. Same
// word boundaries as the italics pass, so a snake_case_name survives.
const plainish = s => String(s ?? "")
  .replace(/\*\*([^*]+)\*\*/g, "$1")
  .replace(/~~([^~]+)~~/g, "$1")
  .replace(/\*([^*\n]+)\*/g, "$1")
  .replace(/(^|[^A-Za-z0-9_])_([^_\n]+)_(?![A-Za-z0-9_])/g, "$1$2");

// `opts.blocks` turns on headings, tables, ordered lists, blockquotes and
// horizontal rules. It is OFF for message text on purpose and that is not a
// style choice: agents write `---` separators and `# ` shell comments and quote
// CLI output with `>` constantly, and the bus is append-only, so enabling them
// there would retroactively restyle every message ever posted. Inline emphasis
// is always on — it is low-noise and already common in what agents write.
// Nested lists are NOT supported; a nested item renders as a flat one.
function md(text, opts) {
  const blocks = !!(opts && opts.blocks);
  // ON unless a caller says otherwise, so message rendering is untouched. A
  // caller rendering a FILE turns it off: the mention pass calls colour() ->
  // agentSlot(), which WRITES board.slotMap.<channel> — a file that mentions
  // @someone would permanently allocate a palette slot to a non-agent and
  // corrupt that channel's colour map for every later render.
  const mentions = !opts || opts.mentions !== false;
  const stash = [];
  const park = (kind, html) => "<<" + kind + (stash.push(html) - 1) + ">>";
  let s = esc(text);
  // The fence's info string reaches an ATTRIBUTE — the first captured group in
  // this function that does. It is constrained AT THE REGEX to [A-Za-z0-9_+-],
  // which cannot carry a quote, a space or a `<`, and that is what makes the
  // interpolation below safe. Widening that charset breaks the guarantee — it
  // is load-bearing, not decorative.
  // The rest of the info LINE is swallowed and dropped, so ```js title=x still
  // parses; that group only applies when a newline follows, which leaves the
  // one-line ```x y``` form rendering exactly as it did before.
  s = s.replace(/```([A-Za-z0-9_+-]*)(?:[^\n]*\n)?([\s\S]*?)```/g, (_, info, b) => {
    const src = b.replace(/\n$/, "");
    const code = "<pre><code" + (info ? ' class="lang-' + info + '"' : "") + ">" +
                 src + "</code></pre>";
    // A mermaid fence keeps its source INSIDE the block. The renderer replaces
    // it only on success, so a parse error can add a line but can never blank
    // what was readable. `src` is already esc()'d, which is what makes it safe
    // in an attribute — and reading it back through dataset is what decodes
    // `--&gt;` to the `-->` mermaid expects.
    return park("B", info.toLowerCase() === "mermaid"
      ? '<div class="mermaid-block" data-src="' + src + '">' + code + "</div>"
      : code);
  });
  s = s.replace(/`([^`\n]+)`/g, (_, c) => park("I", "<code>" + c + "</code>"));
  // Links are parked BEFORE emphasis and the label is emphasised inside the
  // callback. The other order mangles a URL that contains a marker —
  // https://x/a_b_c_d became https://x/a<i>b</i>c_d.
  const emph = t => t
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/~~([^~]+)~~/g, "<s>$1</s>")
    .replace(/\*([^*\n]+)\*/g, "<i>$1</i>")
    // a word boundary on BOTH sides, or every snake_case_name italicises
    .replace(/(^|[^A-Za-z0-9_])_([^_\n]+)_(?![A-Za-z0-9_])/g, "$1<i>$2</i>");
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    (_, t, u) => park("I", '<a href="' + u + '" target="_blank" rel="noopener">' + emph(t) + "</a>"));
  s = s.replace(/(^|\s)(https?:\/\/[^\s<]+)/g,
    (_, p, u) => p + park("I", '<a href="' + u + '" target="_blank" rel="noopener">' + u + "</a>"));
  s = emph(s);
  if (mentions) s = s.replace(/(^|[^\w@.])@([A-Za-z0-9][\w-]*)/g,
    (_, p, n) => p + '<span class="mention" style="color:' + colour(n) + ";background:" + chipBg(n) + '">@' + n + "</span>");
  const out = [];
  const lines = s.split("\n");
  // indexed, not for..of: a block construct has to be able to look AHEAD at
  // lines[i + 1] and to consume the lines it swallows by advancing i
  const WRAP = { checks: ['<ul class="checks">', "</ul>"], plain: ["<ul>", "</ul>"],
                 ol: ["<ol>", "</ol>"], quote: ["<blockquote>", "</blockquote>"] };
  let open = null;                 // the block kind currently open, or null
  const openBlock = kind => {      // one place that opens and closes a block
    if (kind === open) return;
    if (open) out.push(WRAP[open][1]);
    if (kind) out.push(WRAP[kind][0]);
    open = kind;
  };
  const cells = row => row.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map(c => c.trim());
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (blocks) {
      const head = line.match(/^\s*(#{1,6})\s+(.*)$/);
      if (head) {
        openBlock(null);
        out.push("<h" + head[1].length + ">" + head[2] + "</h" + head[1].length + ">");
        continue;
      }
      // A row is a table only if the NEXT line is its delimiter row, and this
      // runs BEFORE the horizontal rule or `| --- |` is eaten as an <hr>.
      const delim = lines[i + 1];
      if (line.includes("|") && delim !== undefined && delim.includes("|") &&
          /^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$/.test(delim)) {
        openBlock(null);
        const align = cells(delim).map(mdAlign);
        const th = cells(line).map((c, k) => "<th" + (align[k] || "") + ">" + c + "</th>").join("");
        const rows = [];
        let j = i + 2;
        for (; j < lines.length && lines[j].includes("|") && lines[j].trim(); j++)
          rows.push("<tr>" + cells(lines[j]).map((c, k) =>
            "<td" + (align[k] || "") + ">" + c + "</td>").join("") + "</tr>");
        out.push('<div class="scrollx"><table><thead><tr>' + th + "</tr></thead>" +
                 (rows.length ? "<tbody>" + rows.join("") + "</tbody>" : "") +
                 "</table></div>");
        i = j - 1;                 // consume the rows this table swallowed
        continue;
      }
      // `---` cannot collide with `- x`: a rule takes no space after the
      // dashes and a bullet requires one. Keep it that way.
      if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        openBlock(null);
        out.push("<hr>");
        continue;
      }
      // esc() rewrote `>` long before this ran: the quote marker is `&gt;`
      const quote = line.match(/^\s*&gt;\s?(.*)$/);
      if (quote) { openBlock("quote"); out.push(quote[1]); continue; }
      const num = line.match(/^\s*\d{1,9}[.)]\s+(.*)$/);
      if (num) { openBlock("ol"); out.push("<li>" + num[1] + "</li>"); continue; }
    }
    const task = line.match(/^\s*-\s\[([ xX])\]\s(.*)$/);
    const item = task ? null : line.match(/^\s*-\s(.*)$/);
    openBlock(task ? "checks" : item ? "plain" : null);
    if (task) out.push("<li><input type=\"checkbox\" disabled" + (task[1].toLowerCase() === "x" ? " checked" : "") + "><span>" + task[2] + "</span></li>");
    else if (item) out.push("<li>" + item[1] + "</li>");
    else out.push(line);
  }
  openBlock(null);
  s = out.join("\n")
    .replace(/\n*(<<B\d+>>)\n*/g, "$1")
    .replace(MD_NL_BEFORE, "$1")
    .replace(MD_NL_AFTER, "$1")
    .replace(MD_NL_INSIDE, "$1")
    .replace(/\n?(<hr>)\n?/g, "$1")      // void: it has no closing tag to match
    .replace(/\n/g, "<br>");
  return s.replace(/<<[BI](\d+)>>/g, (_, i) => stash[i]);
}

const fileUrl = (ch, ref) => "/files/" + encodeURIComponent(ch) + "/" +
  encodeURIComponent(String(ref || "").replace(/^files\//, ""));

function codeCard(label, lang, body, loader) {
  const card = document.createElement("div");
  card.className = "card";
  const lines = body === null ? 0 : body.split("\n").length;
  card.innerHTML =
    '<div class="chead"><span>' + esc(label) + "</span>" +
    (lang ? '<span class="sep">\u00b7</span><span>' + esc(lang) + "</span>" : "") +
    '<span class="grow"></span></div>' +
    '<div class="cbody"><pre><code>' + (body === null ? "" : esc(body)) + "</code></pre></div>";
  const head = card.querySelector(".chead");
  const code = card.querySelector("code");
  if (body === null) {
    card.querySelector(".cbody").hidden = true;   // no empty box before "show"
    const show = document.createElement("button");
    show.textContent = "show";
    show.addEventListener("click", async () => {
      show.textContent = "loading\u2026";
      const text = await loader().catch(() => null);
      code.textContent = text === null ? "could not load" : text;
      card.querySelector(".cbody").hidden = false;
      show.remove();
    });
    head.appendChild(show);
  } else if (lines > 12) {
    card.classList.add("clipped");
    const more = document.createElement("button");
    more.textContent = "show all (" + lines + " lines)";
    more.addEventListener("click", () => {
      const clipped = card.classList.toggle("clipped");
      more.textContent = clipped ? "show all (" + lines + " lines)" : "collapse";
    });
    head.appendChild(more);
  }
  return card;
}

// Only http(s) and our own root-relative paths may become an href. A link
// attachment's url is agent-supplied, and "javascript:" there would run on the
// board's origin, which can read every channel through the JSON API. esc()
// stops attribute break-out but does nothing about the scheme.
function safeUrl(url) {
  const raw = String(url == null ? "" : url).trim();
  if (!raw) return null;
  if (raw.startsWith("/")) return raw;
  try {
    const u = new URL(raw, location.origin);
    return (u.protocol === "http:" || u.protocol === "https:") ? u.href : null;
  } catch (e) {
    return null;
  }
}

function anchor(url, innerHtml, cls) {
  const href = safeUrl(url);
  if (href === null) {   // keep it visible, just not clickable
    return '<span class="unsafe" title="blocked: not an http(s) link">' + innerHtml + "</span>";
  }
  return '<a class="' + (cls || "") + '" href="' + esc(href) + '" target="_blank" rel="noopener">' + innerHtml + "</a>";
}

function linkCard(url, label) {
  const card = document.createElement("div");
  card.className = "card";
  card.dataset.url = url;
  card.innerHTML = '<div class="row">' + anchor(url, esc(label || url)) + "</div>";
  if (safeUrl(url) !== null) upgradeLink(card, url);   // renders now; the unfurl fills in behind it
  return card;
}

function upgradeLink(card, url) {
  if (!S.channel) return;
  fetch("/api/channels/" + encodeURIComponent(S.channel) + "/unfurl?url=" + encodeURIComponent(url))
    .then(r => r.ok ? r.json() : null)
    .then(u => { if (u) paintUnfurl(card, u); })
    .catch(() => {});   // a bare link is a fine end state
}

function paintUnfurl(card, u) {
  const link = (inner, cls) => anchor(u.url, inner, cls);
  if (u.kind === "github_pr" || u.kind === "github_issue") {
    const c = u.checks;
    const checks = c && (c.success || c.failure || c.pending)
      ? '<span class="checks">' +
        (c.success ? '<span class="ok">\u2713' + c.success + "</span> " : "") +
        (c.failure ? '<span class="bad">\u2717' + c.failure + "</span> " : "") +
        (c.pending ? '<span class="wait">\u25cb' + c.pending + "</span>" : "") + "</span>"
      : "";
    const diff = u.kind === "github_pr"
      ? '<span class="diff"><span class="add">+' + u.additions + '</span> <span class="del">\u2212' + u.deletions + "</span></span>"
      : "";
    card.innerHTML = '<div class="meta">' +
      '<span class="repo">' + esc(u.repo) + "#" + u.number + "</span>" +
      link('<span class="title">' + esc(u.title || u.url) + "</span>") +
      '<span class="badge ' + esc(u.state) + '">' + esc(u.state) + "</span>" +
      diff + checks + "</div>";
    return;
  }
  if (u.kind === "gdoc") {
    card.innerHTML = '<div class="meta"><span>\ud83d\udcc4</span>' +
      link('<span class="title">' + esc(u.title || u.url) + "</span>") +
      '<span class="gdoc-host">Google Docs</span>' + "</div>";
    return;
  }
  let host = u.url;
  try { host = new URL(u.url).hostname; } catch (e) { /* keep the raw url */ }
  card.innerHTML = '<div class="meta"><span class="repo">' + esc(host) + "</span>" +
    link(esc(u.title || u.url)) + "</div>";
}

// Agents drop `type` when they hand attach_file's object straight back to post().
// The bus repairs new messages; lines already on disk cannot be rewritten, so infer here too.
function inferType(att) {
  if (!att || typeof att !== "object") return null;
  if (att.type) return att.type;
  if (att.ref) return "file";
  if (att.url) return "link";
  if (att.body) return "code";
  if (att.items) return "tasks";
  return null;
}

// a preset's own label, for a read-only row; an id the catalog dropped shows raw
function clanPresetLabel(catalog, id) {
  const hit = ((catalog && catalog.presets) || []).find(p => p.id === id);
  return hit ? hit.label : (id || "");
}

// the clan the orchestrator proposed is a suggestion: any role may be dropped,
// and any catalogued or free-named role added. Both edit card._att.roles — the
// working copy Confirm posts — and rebuild the ROWS only: a whole-card render
// would move clanRadioSeq and orphan the checked writer radio mid-edit.
function clanRemoveRole(card, r) {
  const i = card._att.roles.indexOf(r);
  if (i >= 0) card._att.roles.splice(i, 1);
  card._rebuild();
}

function clanAddRole(card, catalog, name) {
  const roles = card._att.roles;
  if (!name || roles.length >= CLAN_LIMITS.roles) return;
  const presets = (catalog && catalog.presets) || [];
  const known = (catalog && catalog.roles && catalog.roles[name]) || null;
  roles.push({ name: name, preset: (known && known.preset) || (presets[0] || {}).id || "",
               writer: false, skills: [], why: "" });
  card._rebuild();
}

function clanRow(card, r, catalog, editable, radio) {
  // two rows per role: the controls, then `why` on its own full-width line.
  // `why` is the orchestrator's argument for the role existing — the thing the
  // human reads to approve a clan — so it never sits behind a sideways scroll.
  const frag = document.createDocumentFragment();
  const tr = document.createElement("tr");
  tr.className = "rolerow";
  const cell = (cls, ...kids) => {
    const td = document.createElement("td");
    td.className = cls;
    for (const k of kids) td.appendChild(k);
    tr.appendChild(td);
    return td;
  };
  const input = (tag, value, attrs) => {
    const el = document.createElement(tag);
    el.className = "clanin";
    if (tag === "input") { el.type = "text"; el.value = value; }
    else el.value = value;
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    if (!editable) el.disabled = true;
    return el;
  };
  const opt = (label, value, sel) => {
    const o = document.createElement("option");
    o.value = value; o.textContent = label;
    if (sel) o.selected = true;
    return o;
  };
  // line 1 on mobile: name (with its remove control) · setup
  const n1 = cell("l1 namecell");
  const rm = document.createElement("button");
  rm.type = "button";
  rm.className = "clanin clanx";
  rm.textContent = "\u00d7";
  rm.setAttribute("aria-label", "remove this role");
  rm.title = "remove this role";
  if (!editable) rm.disabled = true;
  rm.addEventListener("click", () => clanRemoveRole(card, r));
  n1.appendChild(rm);
  // the name is what extract_mentions gates, so it writes back and revalidates
  const nm = input("input", r.name, { "maxlength": "40" });
  nm.addEventListener("input", () => { r.name = nm.value; clanRevalidate(card); });
  n1.appendChild(nm);

  // ONE control per role: the preset carries harness, model and effort together
  if (editable && catalog) {
    const sel = input("select", r.preset);
    const presets = catalog.presets || [];
    // server order is the display order; number from the index, never from
    // p.order, which is only the sort key and may have gaps
    presets.forEach((pr, i) => sel.appendChild(
      opt((i + 1) + ") " + pr.label, pr.id, pr.id === r.preset)));
    if (!presets.some(pr => pr.id === r.preset))
      sel.appendChild(opt(r.preset, r.preset, true));   // a preset the catalog no longer ships
    sel.value = r.preset;
    sel.addEventListener("change", () => { r.preset = sel.value; clanRevalidate(card); });
    cell("l1 setupcell").appendChild(sel);
  } else cell("l1 setupcell", document.createTextNode(clanPresetLabel(catalog, r.preset)));

  // line 2 on mobile: writer · skills
  const w = document.createElement("input");
  w.type = "radio"; w.className = "clanin"; w.name = radio; w.checked = !!r.writer;
  if (!editable) w.disabled = true;
  w.addEventListener("change", () => {
    if (!w.checked) return;
    for (const o of card.querySelectorAll('input[type="radio"][name="' + radio + '"]'))
      if (o !== w) o.checked = false;
    for (const rr of card._att.roles) rr.writer = false;
    r.writer = true;
    clanRevalidate(card);
  });
  cell("l2").appendChild(w);
  const sk = input("input", (r.skills || []).join(", "), { "placeholder": "skills, comma-separated" });
  sk.addEventListener("input", () => {
    r.skills = sk.value.split(",").map(s => s.trim()).filter(Boolean);
    clanRevalidate(card);
  });
  cell("l2").appendChild(sk);

  const wtr = document.createElement("tr");
  wtr.className = "whyrow";
  const wtd = document.createElement("td");
  wtd.className = "why";
  wtd.colSpan = 4;                                     // name, setup, writer, skills
  wtd.textContent = r.why || "";                       // why is read-only
  wtr.appendChild(wtd);
  frag.append(tr, wtr);
  return frag;
}

function clanRevalidate(card) {
  clanCatalog().then(catalog => {
    const bad = clanValidate(card._att, catalog);
    card.querySelector(".clanerr").textContent = bad || "";
    const btn = card.querySelector(".confirm");
    if (btn) btn.disabled = !!(bad || !clanEditable(card) || card._confirming);
  }).catch(() => {
    card.querySelector(".clanerr").textContent =
      "catalog unreadable — check models.toml, presets.toml and roles.toml in RATEL_HOME";
    const btn = card.querySelector(".confirm");
    if (btn) btn.disabled = true;
  });
}

function clanCardInner(att, channel, m, card, author) {
  const root = document.createElement("div");
  const head = document.createElement("div");
  head.className = "chead clanhead";
  author = author || (m ? m.from : "");
  card.dataset.author = author;
  head.innerHTML = "<span>" + esc(clanHeadText(card, att.status)) + "</span>";
  root.appendChild(head);

  const table = document.createElement("table");
  const thead = document.createElement("thead");
  // four columns; `why` is the full-width second row under each of them
  thead.innerHTML = "<tr><th>name</th><th>setup</th><th>writer</th><th>skills</th></tr>";
  table.appendChild(thead);
  const tbody = document.createElement("tbody");
  const radio = "clan-writer-" + (++clanRadioSeq);
  const editable = att.status === "proposed" && clanEditable(card);

  // "+ add role": the catalogued roles not already on the card, or a free name.
  // Only an editable card gets one.
  const add = document.createElement("div");
  add.className = "clanadd";
  const addSel = document.createElement("select");
  addSel.className = "clanin";
  addSel.setAttribute("aria-label", "a catalogued role to add");
  const addName = document.createElement("input");
  addName.type = "text";
  addName.className = "clanin";
  addName.setAttribute("maxlength", "40");
  addName.setAttribute("placeholder", "or a new role name");
  const addBtn = document.createElement("button");
  addBtn.type = "button";
  addBtn.className = "clanin";
  addBtn.textContent = "+ add role";
  add.append(addSel, addName, addBtn);

  let cat = null;
  const fillAdd = catalog => {
    const on = new Set(card._att.roles.map(r => r.name));
    const names = Object.keys((catalog && catalog.roles) || {})
      // the orchestrator is fixed at kickoff and never proposed
      .filter(x => x !== "orchestrator" && !on.has(x)).sort();
    addSel.replaceChildren();
    for (const x of names) {
      const o = document.createElement("option");
      o.value = x; o.textContent = x;
      addSel.appendChild(o);
    }
    addSel.hidden = !names.length;
  };
  addBtn.addEventListener("click", () => {
    // a typed name wins over the dropdown; it still picks up a catalogued
    // role's own preset when it happens to name one
    clanAddRole(card, cat, addName.value.trim() || addSel.value);
    addName.value = "";
  });
  // the cap is enforced here, not left to the validator to reject afterwards
  card._syncAdd = () => {
    const room = card._att.roles.length < CLAN_LIMITS.roles;
    for (const el of [addSel, addName, addBtn]) el.disabled = !(clanEditable(card) && room);
    addBtn.title = room ? "" : "a clan is at most " + CLAN_LIMITS.roles + " roles";
  };

  const fill = catalog => {
    cat = catalog;
    tbody.replaceChildren();
    // card._att is the working copy Confirm posts: rows must bind to it
    for (const r of card._att.roles) tbody.appendChild(clanRow(card, r, catalog, editable, radio));
    fillAdd(catalog);
    refreshClanHeads();   // the async rebuild must not outlive a demotion
    card._syncAdd();      // ...and refreshClanHeads knows nothing about the cap
    clanRevalidate(card); // ...and the card must be validated at build time
  };
  // add/remove rebuild the rows through this, never the whole card
  card._rebuild = () => fill(cat);
  clanCatalog().then(fill).catch(() => fill(null));
  table.appendChild(tbody);
  const scroll = document.createElement("div");
  scroll.className = "scrollx";        // the scroll container is the wrapper
  scroll.appendChild(table);
  root.appendChild(scroll);
  if (editable) root.appendChild(add);

  const err = document.createElement("div");
  err.className = "clanerr";
  const hint = document.createElement("div");
  hint.className = "clanhint";
  if (att.status === "proposed" && clanEditable(card) === false && clanIsNewest(card) && !S.token)
    hint.textContent = "open the board with the token URL to confirm";
  root.appendChild(err);
  root.appendChild(hint);

  if (att.status === "approved") {
    const foot = document.createElement("div");
    foot.className = "cfoot";
    foot.textContent = "approved \u00b7 supersedes " + (att.supersedes || "");
    root.appendChild(foot);
  } else {
    const btn = document.createElement("button");
    btn.className = "confirm";
    btn.textContent = "Confirm";
    btn.disabled = !S.token;
    btn.addEventListener("click", async () => {
      if (card._confirming) return;
      card._confirming = true;
      const generation = S.generation;
      btn.disabled = true;
      try {
        const catalog = await clanCatalog().catch(() => null);
        if (generation !== S.generation || !card.isConnected) return;
        const approvedAtt = { type: "clan", status: "approved", issue: att.issue,
                              roles: card._att.roles, supersedes: m ? m.id : "" };
        const bad = clanValidate(approvedAtt, catalog);
        if (bad) { err.textContent = bad; return; }
        const r = await fetch("/api/channels/" + encodeURIComponent(channel) + "/post", {
          method: "POST",
          headers: Object.assign({ "Content-Type": "application/json" }, authHeaders()),
          body: JSON.stringify({ text: "@orchestrator clan approved",
                                 parent: m ? m.id : null, attachments: [approvedAtt] })
        });
        if (generation !== S.generation || !card.isConnected) return;
        if (r.status === 201) {
          card.dataset.status = "approved";
          card.replaceChildren(clanCardInner(approvedAtt, channel, m, card, "stakeholder"));
        } else if (r.status === 401) {
          storeToken(null);
          hint.textContent = "token cleared — open the board with the token URL to confirm";
        } else {
          const b = await r.json().catch(() => ({}));
          hint.textContent = b.error || "error " + r.status;
        }
      } catch {
        if (card.isConnected) hint.textContent = "Could not confirm. Check your connection and try again.";
      } finally {
        card._confirming = false;
        if (card.isConnected) clanRevalidate(card);
      }
    });
    root.appendChild(btn);
  }
  return root;
}

/* ---- mermaid ------------------------------------------------------- */
// pinned to the vendored blob; ratel/static/VENDOR.md records the hash
const MERMAID_VERSION = "10.9.1";
let mermaidReady = null;      // the single load, shared by every block
let mermaidSeq = 0;

// theme from the board's own tokens, so a diagram tracks dark/light with the
// rest of the page. A theme flip MID-SESSION leaves an already-rendered
// diagram in the old palette: accepted, not fixed — the modal it lives in is
// short-lived, and a re-render path costs more than the case is worth.
function mermaidConfig() {
  const css = getComputedStyle(document.documentElement);
  const v = n => css.getPropertyValue(n).trim();
  return {
    startOnLoad: false,                 // every render is explicit, per block
    // strict sets htmlLabels: false and disables `click` directives. Without
    // it mermaid renders arbitrary HTML inside a node label, which drives
    // straight through this file's escape-first model. Set, never inherited.
    securityLevel: "strict",
    suppressErrorRendering: true,       // a failure is ours to show, in place
    // `secure` is the list a %%{init}%% directive may NOT override. Mermaid's
    // default does not include `flowchart`, and
    // %%{init: {"flowchart":{"defaultRenderer":"elk"}}}%% would construct a
    // Worker — which this board's CSP has no worker-src for. Diagram source is
    // agent-authored text off the bus, so that directive is reachable by
    // anything that can post. Do not shorten this list.
    secure: ["secure", "securityLevel", "startOnLoad", "maxTextSize",
             "suppressErrorRendering", "flowchart", "layout", "elk"],
    theme: "base",
    fontFamily: v("--font-sans") || "system-ui",
    themeVariables: {
      background: v("--bg-app"), mainBkg: v("--bg-card"),
      primaryColor: v("--bg-card"), primaryTextColor: v("--text-primary"),
      primaryBorderColor: v("--border"), secondaryColor: v("--bg-hover"),
      tertiaryColor: v("--bg-field"), lineColor: v("--text-muted"),
      textColor: v("--text-primary"), nodeBorder: v("--border"),
      clusterBkg: v("--bg-field"), clusterBorder: v("--border"),
      titleColor: v("--text-primary"), edgeLabelBackground: v("--bg-app"),
    },
  };
}

// The ONE injection site. 990 KB over a tailnet, on a board that usually shows
// no diagram at all, is not a cost to pay on every page load — so this runs
// only when a block actually asks to be rendered, never from the markup.
function loadMermaid() {
  if (!mermaidReady) {
    mermaidReady = new Promise((resolve, reject) => {
      const el = document.createElement("script");
      el.src = "/static/mermaid.min.js?v=" + MERMAID_VERSION;
      el.onload = () => resolve(window.mermaid);
      el.onerror = () => reject(new Error("could not load the renderer"));
      document.head.appendChild(el);
    }).then(m => {
      m.initialize(mermaidConfig());
      return m;
    });
  }
  return mermaidReady;
}

async function renderMermaid(block, stale) {
  const gone = () => (stale && stale()) || !block.isConnected;
  try {
    const m = await loadMermaid();
    if (gone()) return;
    // mermaid mis-measures text inside a display:none subtree, so the block
    // must already be laid out — the modal is showModal()-ed before this runs
    const { svg } = await m.render("mmd-" + (++mermaidSeq), block.dataset.src || "");
    if (gone()) return;
    const holder = document.createElement("div");
    holder.className = "scrollx mermaid-svg";
    holder.innerHTML = svg;
    for (const a of holder.querySelectorAll("a")) {   // mermaid may emit links
      a.setAttribute("target", "_blank");
      a.setAttribute("rel", "noopener");
    }
    block.replaceChildren(holder);       // the source goes ONLY now, on success
  } catch (err) {
    if (gone() || block.querySelector(".mermaid-err")) return;
    const note = document.createElement("div");
    note.className = "mermaid-err";      // the fence source is still right above
    note.textContent = "Diagram did not render: " + ((err && err.message) || "error");
    block.appendChild(note);
  }
}

// auto: the preview modal renders straight away. A message does NOT — an agent
// posting a diagram must not pull 990 KB onto someone's phone without a tap.
function hydrateMermaid(root, auto) {
  for (const block of root.querySelectorAll(".mermaid-block:not([data-armed])")) {
    block.dataset.armed = "1";
    if (auto) {
      renderMermaid(block, root._stale);
      continue;
    }
    const go = document.createElement("button");
    go.className = "mermaid-go";
    go.textContent = "render diagram";
    go.addEventListener("click", () => { go.remove(); renderMermaid(block); });
    block.appendChild(go);
  }
}

/* ---- file preview modal -------------------------------------------- */
const PREVIEW_MAX = 512 * 1024;
let previewSeq = 0;        // a later click, or a close mid-fetch, orphans the render
let previewLock = null;    // the body overflow we replaced, when we replaced one

// Which attachments earn a modal. The mime decides, with the filename as the
// fallback — attach_file may omit the mime, which is why inferType exists.
// Everything else (images, PDFs, the rest) keeps opening in a new tab: /files/
// serves `default-src 'none'; sandbox` precisely so a PDF cannot be framed.
function previewKind(att, name) {
  const mime = String(att.mime || "");
  if (mime === "text/markdown" || /\.(?:md|markdown)$/i.test(name)) return "md";
  if (mime === "text/plain" || /\.(?:txt|log)$/i.test(name)) return "text";
  return null;
}

function closePreview() {
  if (elPreview && elPreview.open) elPreview.close();
}

function openPreview(url, name, kind) {
  const seq = ++previewSeq;
  const body = elPreview.querySelector(".pbody");
  elPreview.querySelector(".pname").textContent = name;
  elPreview.querySelector(".popen").innerHTML = anchor(url, "open in new tab");
  body.replaceChildren();
  body.textContent = "Loading\u2026";
  if (!elPreview.open) {
    elPreview.showModal();     // the top layer: no z-index rule to maintain
    if (matchMedia("(max-width: 700px)").matches) {
      // remember what was there: openThread sets this too on a phone, and
      // clearing it unconditionally would destroy the thread's lock underneath
      previewLock = { prev: document.body.style.overflow };
      document.body.style.overflow = "hidden";
    }
  }
  const stale = () => seq !== previewSeq || !elPreview.open;
  fetch(url).then(r => {
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.text();
  }).then(text => {
    if (stale()) return;
    const cut = text.length > PREVIEW_MAX;
    body.replaceChildren();
    if (kind === "md") {
      const wrap = document.createElement("div");
      wrap.className = "mdblocks";        // the block CSS is scoped to this
      wrap.innerHTML = md(text.slice(0, PREVIEW_MAX), { blocks: true, mentions: false });
      body.appendChild(wrap);
      wrap._stale = stale;              // reuse the counter, never a second one
      hydrateMermaid(wrap, true);       // laid out already: showModal() ran first
    } else {
      const pre = document.createElement("pre");
      pre.textContent = text.slice(0, PREVIEW_MAX);   // the mime says plain: a
      body.appendChild(pre);                          // `# ` log line is not a heading
    }
    if (cut) {
      const note = document.createElement("div");
      note.className = "pnote";
      note.textContent = "Showing the first " + Math.round(PREVIEW_MAX / 1024) +
        " KB \u2014 open in a new tab for the rest.";
      body.appendChild(note);
    }
  }).catch(err => {
    if (stale()) return;
    body.replaceChildren();
    const msg = document.createElement("div");
    msg.className = "pnote";
    msg.textContent = "Could not load this file (" + (err && err.message || "error") + ").";
    const again = document.createElement("button");
    again.className = "retry";
    again.textContent = "Retry";
    again.addEventListener("click", () => openPreview(url, name, kind));
    body.append(msg, again);
  });
}

if (elPreview) {
  elPreview.querySelector(".close").addEventListener("click", closePreview);
  // one place for every way it closes: the button, Escape, or a channel switch
  elPreview.addEventListener("close", () => {
    previewSeq++;                                  // orphan anything in flight
    elPreview.querySelector(".pbody").replaceChildren();
    if (previewLock) {                             // restore what WE replaced
      document.body.style.overflow = previewLock.prev;
      previewLock = null;
    }
  });
}

function renderAttachment(att, channel, m) {
  const type = inferType(att);
  if (type === "code") {
    return codeCard(att.file || "snippet", att.lang, att.body || "");
  }
  if (type === "file") {
    const url = fileUrl(channel, att.ref);
    const name = att.name || att.ref;
    if (att.lang) {   // spilled code: show the language, load the body on demand
      return codeCard(name, att.lang, null, () => fetch(url).then(r => r.text()));
    }
    if (String(att.mime || "").startsWith("image/")) {
      const a = document.createElement("a");
      a.href = url; a.target = "_blank"; a.rel = "noopener";
      a.innerHTML = '<img loading="lazy" alt="' + esc(name) + '" src="' + esc(url) + '">';
      return a;
    }
    const kind = previewKind(att, name);
    const pdf = att.mime === "application/pdf";
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = '<div class="row">' +
      (pdf ? "<span>\ud83d\udcc4</span>" : "") +
      "<span>" + esc(name) + '</span><span class="sep">\u00b7</span><span>' +
      esc(pdf && att.pages ? att.pages + (att.pages === 1 ? " page" : " pages") : att.mime || "file") +
      "</span>" +
      (kind ? '<button class="preview">preview</button>' : "") +
      anchor(url, kind ? "open in new tab" : "open") + "</div>";
    if (kind) card.querySelector("button.preview")
      .addEventListener("click", () => openPreview(url, name, kind));
    return card;
  }
  if (type === "tasks") {
    const items = att.items || [];
    const done = items.filter(i => i.done).length;
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = '<div class="chead"><span>' + esc(att.ref || "tasks") + "</span></div>" +
      '<ul class="checks">' + items.map(i =>
        "<li><input type=\"checkbox\" disabled" + (i.done ? " checked" : "") + "><span>" + esc(i.text) + "</span>" +
        (i.who ? '<span class="who" style="color:' + colour(i.who) + ";background:" + chipBg(i.who) + '">' + esc(i.who) + "</span>" : "") +
        "</li>").join("") + "</ul>" +
      '<div class="cfoot">' + done + "/" + items.length + " done</div>" +
      '<div class="progress" role="presentation"><span style="width:' +
      (items.length ? Math.round(done * 100 / items.length) : 0) + '%"></span></div>';
    return card;
  }
  if (type === "link") {
    return linkCard(att.url, null);
  }
  if (type === "clan") {
    const card = document.createElement("div");
    card.className = "card clancard";
    card.dataset.msg = m ? m.id : "";
    card.dataset.status = att.status;
    card._att = JSON.parse(JSON.stringify(att));   // the editable working copy
    card.appendChild(clanCardInner(att, channel, m, card));
    return card;
  }
  const unknown = document.createElement("div");   // never drop an attachment silently
  unknown.className = "card";
  unknown.innerHTML = '<div class="row"><code>' + esc(JSON.stringify(att)) + "</code></div>";
  return unknown;
}

const hhmm = ts => new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

function dateLabel(ts) {
  const d = new Date(ts);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const that = new Date(d); that.setHours(0, 0, 0, 0);
  const day = 86400000;
  if (that.getTime() === today.getTime()) return "Today";
  if (that.getTime() === today.getTime() - day) return "Yesterday";
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function dateSeparator(ts) {
  const div = document.createElement("div");
  div.className = "date-sep";
  div.textContent = dateLabel(ts);
  return div;
}

function hideEmptyRows() {
  for (const el of elMessages.querySelectorAll(".empty-row")) el.remove();
}

/* ---- rendering ----------------------------------------------------- */
function messageNode(m, interactive) {
  const el = document.createElement("article");
  el.className = "msg";
  el.dataset.id = m.id;
  el.style.borderLeftColor = colour(m.from);
  if (isAltCycle(m.from)) el.classList.add("alt");
  el.innerHTML =
    '<div class="head"><b class="from" style="color:' + colour(m.from) + '">' + esc(m.from) + "</b>" +
    '<time datetime="' + esc(m.ts) + '" title="' + esc(m.ts) + '">' + hhmm(m.ts) + "</time></div>" +
    '<div class="body">' + md(m.text) + '</div><div class="atts"></div><div class="foot"></div>';
  hydrateMermaid(el.querySelector(".body"), false);
  const atts = el.querySelector(".atts");
  for (const a of m.attachments || []) atts.appendChild(renderAttachment(a, S.channel, m));
  if (interactive) {
    const open = e => { if (!e.target.closest(".atts, a, button")) openThread(m.parent || m.id); };
    el.setAttribute("tabindex", "0");
    el.setAttribute("role", "button");
    el.setAttribute("aria-label", "Open thread from " + m.from);
    el.addEventListener("click", open);
    el.addEventListener("keydown", e => {
      if (e.target !== el) return;
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(e); }
    });
  }
  return el;
}

function paintFoot(id) {
  const el = elMessages.querySelector('.msg[data-id="' + id + '"]');
  if (!el) return;
  const m = S.byId.get(id);
  const foot = el.querySelector(".foot");
  foot.textContent = m && m.parent ? "↳ Reply · View thread" : "View thread";
  el.classList.add("has-replies");
}

// Header progress: the newest pin carrying a `tasks` attachment (issue #3). It is
// fed from the same pins list as the strip, so a pin/unpin/replace arriving over
// SSE repaints both in the same refreshPins() call. No such pin: stays hidden.
function tasksPin(pins) {
  for (let i = pins.length - 1; i >= 0; i--) {
    const att = (pins[i].attachments || []).find(a => inferType(a) === "tasks");
    if (att) return { pin: pins[i], att: att };
  }
  return null;
}

function renderProgress(pins) {
  const hit = tasksPin(pins);
  elProgress.hidden = !hit;
  if (!hit) { delete elProgress.dataset.id; return; }
  const items = hit.att.items || [];
  const done = items.filter(i => i.done).length;
  const pct = items.length ? Math.round(done * 100 / items.length) : 0;
  const title = plainish(hit.pin.text.split("\n")[0]).trim() || hit.att.ref || "tasks";
  elProgress.dataset.id = hit.pin.id;
  elProgress.title = title;
  elProgress.setAttribute("aria-label", done + " of " + items.length + " tasks done · " + title);
  elProgress.querySelector(".cnt").textContent = done + "/" + items.length;
  elProgress.querySelector(".progress > span").style.width = pct + "%";
}

elProgress.addEventListener("click", () => {
  const row = elMessages.querySelector('.msg[data-id="' + elProgress.dataset.id + '"]');
  if (row && !row.hidden) {
    row.scrollIntoView({ block: "center" });
    row.classList.add("flash");
    setTimeout(() => row.classList.remove("flash"), 1200);
    return;
  }
  // filtered out of the list: open the pinned card instead
  elPins.classList.add("open");
  elPins.scrollIntoView({ block: "start" });
});

function renderPins(pins) {
  renderProgress(pins);
  elPins.innerHTML = "";
  elPins.classList.remove("open");
  if (!pins.length) return;
  const older = pins.slice(0, -1), latest = pins[pins.length - 1];

  // Mobile collapsed summary row (normative); hidden on desktop by CSS.
  const summary = document.createElement("button");
  summary.className = "summary";
  summary.setAttribute("aria-label", "Toggle pinned messages");
  summary.innerHTML = '<span class="glyph">\ud83d\udccc</span>' +
    '<span class="cnt">' + pins.length + " pinned</span>" +
    '<span class="sum-title">' + esc(plainish(latest.text.replace(/\s+/g, " ")).slice(0, 120)) + "</span>" +
    '<span class="chev">&#9662;</span>';
  summary.addEventListener("click", () => elPins.classList.toggle("open"));

  const body = document.createElement("div");
  body.className = "pinbody";
  const label = document.createElement("div");
  label.className = "pinlabel";
  label.textContent = "Pinned \u00b7 " + pins.length;
  body.appendChild(label);

  let pending = null;
  if (older.length) {
    const toggle = document.createElement("button");
    toggle.className = "older";
    const label2 = n => "+" + n + " older pin" + (n === 1 ? "" : "s");
    toggle.textContent = label2(older.length);
    const list = document.createElement("div");
    list.hidden = true;
    for (const p of older) {
      const line = document.createElement("button");
      line.type = "button";
      line.addEventListener("click", () => openThread(p.parent || p.id));
      line.className = "pin collapsed";
      line.dataset.id = p.id;
      line.textContent = p.from + ": " + p.text.replace(/\s+/g, " ");
      line.style.borderLeft = "3px " + (isAltCycle(p.from) ? "dotted" : "solid") + " " + colour(p.from);
      list.appendChild(line);
    }
    toggle.addEventListener("click", () => {
      list.hidden = !list.hidden;
      toggle.textContent = list.hidden ? label2(older.length) : "hide older pins";
    });
    pending = [toggle, list];
  }
  const box = document.createElement("div");
  box.className = "pin expanded";
  const node = messageNode(latest, true);
  node.style.borderLeftColor = colour(latest.from);
  if (isAltCycle(latest.from)) node.classList.add("alt");
  box.appendChild(node);
  body.appendChild(box);
  if (pending) body.append(...pending);
  elPins.append(summary, body);
}

function renderPresence(list) {
  elPresence.innerHTML = list.map(p => {
    const state = p.online ? "online" : "offline";
    return '<div class="agent-row ' + state + '" data-agent="' + esc(p.agent) + '"' +
      ' title="' + esc(p.agent) + " \u00b7 last seen " + esc(p.last_seen) + '"' +
      ' aria-label="' + esc(p.agent) + ' \u00b7 ' + state + '">' +
      '<span class="dot' + (isAltCycle(p.agent) ? " alt" : "") + '" style="color:' + colour(p.agent) + '"></span>' +
      '<span class="nm">' + esc(p.agent) + "</span></div>";
  }).join("");
}

function noteAgents(m) {
  const before = S.agents.size;
  S.agents.add(m.from);
  for (const n of m.mentions || []) S.agents.add(n);
  if (S.agents.size === before) return;
  const keep = elFilter.value;
  elFilter.innerHTML = '<option value="">everyone</option>' +
    [...S.agents].sort().map(a => '<option value="' + esc(a) + '">@' + esc(a) + "</option>").join("");
  elFilter.value = keep;
}

function applyFilter() {
  const who = elFilter.value;
  let visible = 0;
  for (const el of elMessages.querySelectorAll(".msg")) {
    const m = S.byId.get(el.dataset.id);
    const show = Boolean(m && matchesView(m));
    el.hidden = !show;
    if (show) visible++;
  }
  // a separator whose group has no visible rows is noise — hide it too
  for (const sep of elMessages.querySelectorAll(".date-sep")) {
    let any = false;
    for (let n = sep.nextElementSibling; n; n = n.nextElementSibling) {
      if (n.classList.contains("date-sep")) break;
      if (n.classList.contains("msg") && !n.hidden) { any = true; break; }
    }
    sep.hidden = !any;
  }
  let empty = elMessages.querySelector(".empty-row.filter");
  if (!visible && (who || S.filters.query || S.filters.operator)) {
    if (!empty) {
      empty = document.createElement("div");
      empty.className = "empty-row filter";
      elMessages.appendChild(empty);
    }
    empty.textContent = "No matching messages. Try another search or filter.";
  } else if (empty) {
    empty.remove();
  }
}
elFilter.addEventListener("change", submitFilters);

/* ---- ingest -------------------------------------------------------- */
function ingest(m, live, historic=false) {
  if (S.seen.has(m.id)) return false;   // the stream replays from `since`; render each id once
  S.seen.add(m.id);
  S.byId.set(m.id, m);
  for (const a of historic ? [] : m.attachments || []) {          // clan state, incl. thread replies
    if (inferType(a) !== "clan") continue;
    if (a.status === "proposed" && m.from === "orchestrator")
      S.newestProposal[S.channel] = m.id;
    // only the token-gated route (or the operator) speaks as stakeholder: an
    // approval authored by a role never demotes the operator's Confirm
    if (a.status === "approved" && a.supersedes && m.from === "stakeholder")
      S.approvedProposal[S.channel] = a.supersedes;
    refreshClanHeads();
  }
  if (!historic) S.last = m.id;                         // append order, independent of ULID sorting
  noteAgents(m);
  if (live) bumpChannel(m);              // a live message is activity, reply or not
  if (m.parent) {
    if (!S.kids.has(m.parent)) S.kids.set(m.parent, []);
    S.kids.get(m.parent).push(m);
    paintFoot(m.parent);
    if (S.openThread === m.parent) {
      const box = elThread.querySelector(".replies");
      if (box && !box.querySelector('[data-id="' + m.id + '"]')) box.appendChild(messageNode(m, false));
    }
  }
  if (live && !matchesView(m)) return true;
  const nearBottom = window.innerHeight + window.scrollY >= document.body.offsetHeight - 80;
  const day = new Date(m.ts).toDateString();
  const el = messageNode(m, true);
  if (day !== S.lastDay) {
    S.lastDay = day;
    elMessages.appendChild(dateSeparator(m.ts));
  }
  elMessages.appendChild(el);
  paintFoot(m.id);
  hideEmptyRows();
  applyFilter();
  if (live) {
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 1200);
    markNew(el);
    if (nearBottom) window.scrollTo(0, document.body.scrollHeight);
  }
  return true;
}

// Persistent "NEW" divider above the first unseen live message; clears once
// the marked row has been >=90% in view for 2s (or a newer divider replaces it).
let newObserver = null;
function markNew(el) {
  const generation = S.generation;
  const prev = elMessages.querySelector(".new-divider");
  if (prev) prev.remove();
  if (newObserver) { newObserver.disconnect(); newObserver = null; }
  const div = document.createElement("div");
  div.className = "new-divider";
  div.textContent = "NEW";
  elMessages.insertBefore(div, el);
  let timer = null;
  newObserver = new IntersectionObserver(entries => {
    for (const en of entries) {
      if (en.intersectionRatio >= 0.9) {
        if (!timer) timer = setTimeout(() => {
          if (generation !== S.generation) return;
          div.remove();
          if (newObserver) { newObserver.disconnect(); newObserver = null; }
        }, 2000);
      } else if (timer) { clearTimeout(timer); timer = null; }
    }
  }, { threshold: [0.9] });
  newObserver.observe(el);
}

/* ---- thread panel width -------------------------------------------- */
const THREAD_WIDTH_KEY = "ratel.board.threadWidth";
const THREAD_MIN = 280, THREAD_MAX = 900, THREAD_DEFAULT = 400;
const SIDEBAR_W = 240, CONTENT_MIN = 360;   // the message column stays readable
// the PREFERENCE, never the clamped result: resizing once in a narrow window
// must not shrink what a wide window gets back
let threadWidthPref = THREAD_DEFAULT;
let threadRaf = 0;

function threadMax() {
  return Math.max(THREAD_MIN, Math.min(THREAD_MAX, window.innerWidth - SIDEBAR_W - CONTENT_MIN));
}
function threadClamp(px) {
  return Math.max(THREAD_MIN, Math.min(threadMax(), px));
}

function applyThreadWidth() {
  threadRaf = 0;
  const w = threadClamp(threadWidthPref);
  document.documentElement.style.setProperty("--thread-width", w + "px");
  if (!elThreadResize) return;
  elThreadResize.setAttribute("aria-valuemin", String(THREAD_MIN));
  elThreadResize.setAttribute("aria-valuemax", String(threadMax()));
  elThreadResize.setAttribute("aria-valuenow", String(Math.round(w)));
  elThreadResize.setAttribute("aria-valuetext", Math.round(w) + " pixels");
}
// one width write per frame: padding-right on #content reflows the whole
// message list, and a long channel is thousands of nodes
function scheduleThreadWidth() {
  if (!threadRaf) threadRaf = requestAnimationFrame(applyThreadWidth);
}
function setThreadWidth(px) { threadWidthPref = px; scheduleThreadWidth(); }
function saveThreadWidth() {                 // once per gesture, never per frame
  try { localStorage.setItem(THREAD_WIDTH_KEY, String(Math.round(threadWidthPref))); } catch (e) {}
}

try {
  const stored = parseInt(localStorage.getItem(THREAD_WIDTH_KEY), 10);
  if (Number.isFinite(stored)) threadWidthPref = stored;
} catch (e) {}
applyThreadWidth();
window.addEventListener("resize", scheduleThreadWidth);   // re-clamp; the preference stands

if (elThreadResize) {
  // pointer events with capture: mouse, trackpad, pen and touch are one code
  // path, and there are no document-level move/up listeners to clean up
  elThreadResize.addEventListener("pointerdown", e => {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    elThreadResize.setPointerCapture(e.pointerId);
    elThreadResize.classList.add("dragging");
    e.preventDefault();
  });
  elThreadResize.addEventListener("pointermove", e => {
    if (!elThreadResize.hasPointerCapture(e.pointerId)) return;
    setThreadWidth(threadClamp(window.innerWidth - e.clientX));
  });
  const endDrag = e => {
    if (!elThreadResize.hasPointerCapture(e.pointerId)) return;
    elThreadResize.releasePointerCapture(e.pointerId);
    elThreadResize.classList.remove("dragging");
    saveThreadWidth();
  };
  elThreadResize.addEventListener("pointerup", endDrag);
  elThreadResize.addEventListener("pointercancel", endDrag);
  elThreadResize.addEventListener("dblclick", () => {
    setThreadWidth(THREAD_DEFAULT);
    saveThreadWidth();
  });
  // the panel is anchored right, so LEFT widens it and Home is the wide end.
  // Escape is deliberately not handled: the document handler closes the
  // thread, and that stays true wherever focus is.
  elThreadResize.addEventListener("keydown", e => {
    const step = e.shiftKey ? 64 : 16;
    let w;
    if (e.key === "ArrowLeft") w = threadWidthPref + step;
    else if (e.key === "ArrowRight") w = threadWidthPref - step;
    else if (e.key === "Home") w = THREAD_MAX;
    else if (e.key === "End") w = THREAD_MIN;
    else if (e.key === "Enter") w = THREAD_DEFAULT;
    else return;
    e.preventDefault();
    setThreadWidth(threadClamp(w));
    saveThreadWidth();
  });
}

/* ---- thread panel -------------------------------------------------- */
async function openThread(id) {
  if (!/^[0-9A-HJKMNP-TV-Z]{26}$/.test(id)) return;
  if (S.threadController) S.threadController.abort();
  const controller = S.threadController = new AbortController();
  const generation = S.generation, request = ++S.threadGeneration, channel = S.channel;
  S.openThread = id;
  saveRoute(id);
  elThread.hidden = false;
  document.body.classList.add("thread-open");
  elThread.textContent = "Loading thread…";
  const cancel = document.createElement("button"); cancel.textContent = "Close thread";
  cancel.onclick = closeThread; elThread.appendChild(cancel);
  const current = () => generation === S.generation && request === S.threadGeneration;
  let t;
  try {
    t = await fetchJSON("/api/channels/" + encodeURIComponent(channel) + "/thread/" + id, controller.signal);
  } catch (error) {
    if (!current() || error.name === "AbortError") return;
    elThread.textContent = "Could not load this thread. ";
    const retry = document.createElement("button"); retry.textContent = "Retry";
    retry.onclick = () => openThread(id); elThread.append(retry, cancel); return;
  }
  if (!current()) return;
  const repliesById = new Map(t.replies.map(m => [m.id, m]));
  for (const m of S.kids.get(id) || []) repliesById.set(m.id, m);
  t.replies = [...repliesById.values()];
  S.openThread = id;
  elThread.innerHTML = "";
  const bar = document.createElement("div");
  bar.className = "thead";
  const back = document.createElement("button");
  back.className = "back";
  const n = t.replies.length;
  back.textContent = "\u2039 " + n + (n === 1 ? " reply" : " replies");
  back.setAttribute("aria-label", "Back to messages");
  back.addEventListener("click", closeThread);
  const title = document.createElement("span");
  title.className = "ttitle";
  title.textContent = "Thread";
  const close = document.createElement("button");
  close.className = "close";
  close.textContent = "\u00d7";
  close.setAttribute("aria-label", "Close thread");
  close.addEventListener("click", closeThread);
  const link = document.createElement("a"); link.textContent = "Thread link";
  link.className = "thread-link"; link.href = routeHash(channel, id);
  bar.append(back, title, link, close);

  const plabel = document.createElement("div");
  plabel.className = "parent-label";
  plabel.textContent = "Parent";
  const pcard = document.createElement("div");
  pcard.className = "parent-card";
  pcard.appendChild(messageNode(t.parent, false));
  const replies = document.createElement("div");
  replies.className = "replies";
  for (const r of t.replies) replies.appendChild(messageNode(r, false));
  elThread.append(bar, plabel, pcard, replies);
  elThread.hidden = false;
  document.body.classList.add("thread-open");
  refreshClanHeads();                    // the thread pane renders its own clan cards
  if (matchMedia("(max-width: 700px)").matches) {
    sheetLock = { y: window.scrollY, id: id };
    document.body.style.overflow = "hidden";
  }
  (matchMedia("(max-width: 700px)").matches ? back : close).focus();
}
let sheetLock = null;
function closeThread(updateRoute=true) {
  if (S.threadController) S.threadController.abort();
  S.threadGeneration++;
  if (updateRoute !== false) saveRoute();
  // the handle is display:none the moment .thread-open goes: focus would fall
  // to <body> and the tab order would restart at the top of the page
  if (elThreadResize && document.activeElement === elThreadResize) {
    const opener = elMessages.querySelector('.msg[data-id="' + S.openThread + '"]');
    if (opener) opener.focus();
  }
  elThread.hidden = true;
  S.openThread = null;
  document.body.classList.remove("thread-open");
  if (sheetLock) {
    document.body.style.overflow = "";
    window.scrollTo(0, sheetLock.y);
    const row = elMessages.querySelector('.msg[data-id="' + sheetLock.id + '"]');
    if (row) row.scrollIntoView({ block: "center" });
    sheetLock = null;
  }
}
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  // <dialog> closes itself on Escape and the event still bubbles to here, so
  // without this guard ONE press closes the preview AND the thread under it.
  if (elPreview && elPreview.open) return;
  if (elMapDialog && elMapDialog.open) return;
  if (elRoleDialog && elRoleDialog.open) return;
  closeThread();
});

/* ---- team map: pure model (no DOM) --------------------------------- */
// These three take strings/arrays and return plain objects so they can be
// sliced out and golden-tested under node. All DOM construction lives in the
// paint layer below. The state vocabulary is asserted equal to the Python one.
const MAP_STATES = ["gone", "stopped", "awaiting-operator", "stuck", "context-full", "busy", "queued",
                    "online", "idle", "offline"];

// Fixed radial layout: orchestrator at the hub, the rest on a circle in the
// clan.toml order the server sent. A node never moves because its state
// changed — the coordinates are a function of the names alone.
function mapLayout(names) {
  const cx = 100, cy = 100, r = 68;
  const hub = names.includes("orchestrator") ? "orchestrator" : null;
  const out = [];
  if (hub) out.push({ name: hub, hub: true, x: cx, y: cy });
  const ring = names.filter(n => n !== hub);
  const n = Math.max(ring.length, 1);
  ring.forEach((name, i) => {
    const a = -Math.PI / 2 + (2 * Math.PI * i) / n;
    out.push({ name: name, hub: false,
               x: Math.round(cx + r * Math.cos(a)),
               y: Math.round(cy + r * Math.sin(a)) });
  });
  return out;
}

// An edge means ADDRESSED, never private: w(a->b) = mentions(a->b) +
// replies(a->b) over a 30-minute window. Self-edges and endpoints that are not
// nodes are dropped. `waiting` is delivery state on a mention edge: the newest
// mentioner toward a role that still owes a reply (busy/queued/stuck).
function mapEdges(msgs, names, generated, states) {
  states = states || {};
  const nowMs = typeof generated === "number" ? generated : Date.parse(generated);
  const nodes = new Set(names);
  const byId = new Map(msgs.map(m => [m.id, m]));
  const win = 30 * 60 * 1000;
  const weights = new Map();
  const newestMentioner = new Map();
  const bump = (a, b) => {
    if (a === b || !nodes.has(a) || !nodes.has(b)) return;
    const key = a + "\u0000" + b;
    weights.set(key, (weights.get(key) || 0) + 1);
    return key;
  };
  for (const m of msgs) {
    const age = nowMs - Date.parse(m.ts);
    if (!(age >= 0 && age <= win)) continue;
    for (const to of m.mentions || []) {
      if (bump(m.from, to)) newestMentioner.set(to, m.from);
    }
    if (m.parent) {
      const p = byId.get(m.parent);
      if (p) bump(m.from, p.from);
    }
  }
  const owes = new Set(["busy", "queued", "stuck", "context-full", "awaiting-operator"]);
  return Array.from(weights, ([key, weight]) => {
    const i = key.indexOf("\u0000");
    const from = key.slice(0, i), to = key.slice(i + 1);
    return { from: from, to: to, weight: weight,
             waiting: owes.has(states[to]) && newestMentioner.get(to) === from };
  });
}

// The server model's state/confidence coerced into the map's vocabulary. An
// unknown state is never trusted into the page.
function mapState(state, confidence) {
  const known = MAP_STATES.includes(state) ? state : "offline";
  const conf = ["measured", "inferred", "weak"].includes(confidence)
    ? confidence : "weak";
  return { state: known, confidence: conf, weak: conf === "weak" };
}

// What the operator reads for a state. `idle` is only honest for a headless
// role, where round.pid genuinely proves nothing is running. Nothing on disk
// proves an interactive role's turn has ended, so it reads "no activity
// measured" — the reason string carries the full sentence.
function stateLabel(state, headless) {
  if (state === "awaiting-operator") return "awaiting operator";
  return state === "idle" && !headless ? "no activity measured" : state;
}

/* ---- map paint ---- */
const elMapPanel = $("#mapPanel"), elMapBody = $("#mapBody"), elMap = $("#map"),
      elMapBtn = $("#mapBtn"), elMapDialog = $("#mapDialog"),
      elMapDialogBody = $("#mapDialogBody");
const MAP_COLLAPSE_KEY = "ratel.board.mapCollapsed";
const SVG_NS = "http://www.w3.org/2000/svg";
const PHONE_MQ = matchMedia("(max-width: 700px)");

function svgEl(tag, attrs, text) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const k in (attrs || {})) el.setAttribute(k, attrs[k]);
  if (text != null) el.textContent = text;
  return el;
}

// Ring colour per state: busy is the accent, stuck is red, context-full amber,
// awaiting-operator violet (the one state that is the operator's to clear),
// online green, everything unknown/quiet neutral. A weak reading draws dashed.
const MAP_RING = {
  stopped: "var(--status-closed)", gone: "var(--text-muted)", stuck: "var(--status-closed)", "context-full": "var(--a7)",
  "awaiting-operator": "var(--a5)",
  busy: "var(--accent)", queued: "var(--text-muted)", online: "var(--status-open)",
  idle: "var(--outline)", offline: "var(--text-muted)",
};

function mountMap() {
  const target = PHONE_MQ.matches ? elMapDialogBody : elMapBody;
  if (elMap.parentNode !== target) target.appendChild(elMap);
}

// The whole paint path is createElementNS + textContent: no HTML string is
// built at all, which is stronger than escaping every field.
function paintMap(model) {
  S.clan = model;
  const rows = (model && model.roles) || [];
  const names = rows.map(r => r.role);
  const states = {};
  rows.forEach(r => { states[r.role] = r.state; });
  const layout = mapLayout(names);
  const pos = new Map(layout.map(p => [p.name, p]));
  const edges = mapEdges(Array.from(S.byId.values()), names,
                         model ? model.generated : 0, states);
  elMap.setAttribute("viewBox", "0 0 200 200");
  elMap.replaceChildren();
  const defs = svgEl("defs");
  const marker = svgEl("marker", {id: "mapArrow", viewBox: "0 0 6 6", refX: 6, refY: 3,
                                  markerWidth: 5, markerHeight: 5,
                                  orient: "auto-start-reverse"});
  marker.appendChild(svgEl("path", {d: "M0,0 L6,3 L0,6 z", fill: "var(--outline)"}));
  defs.appendChild(marker);
  elMap.appendChild(defs);
  for (const e of edges) {
    const a = pos.get(e.from), b = pos.get(e.to);
    if (!a || !b) continue;
    elMap.appendChild(svgEl("line", {x1: a.x, y1: a.y, x2: b.x, y2: b.y,
                                     class: "edge" + (e.waiting ? " waiting" : "")}));
  }
  for (const p of layout) {
    const r = rows.find(x => x.role === p.name);
    const st = mapState(r ? r.state : "gone", r ? r.confidence : "weak");
    const g = svgEl("g", {class: "node" + (st.weak ? " node-weak" : ""),
                          "data-role": p.name, role: "button", tabindex: "0"});
    g.appendChild(svgEl("circle", {class: "node-ring", cx: p.x, cy: p.y, r: 9,
                                   "stroke-width": 2.5,
                                   stroke: MAP_RING[st.state] || "var(--outline)"}));
    g.appendChild(svgEl("circle", {class: "node-dot", cx: p.x, cy: p.y, r: 6.5,
                                   fill: colour(p.name)}));
    if (r && r.context_tokens != null && r.checkpoint_at) {
      const frac = Math.max(0, Math.min(1, r.context_tokens / r.checkpoint_at));
      const c = 2 * Math.PI * 12;
      g.appendChild(svgEl("circle", {
        cx: p.x, cy: p.y, r: 12, fill: "none", "stroke-width": 2,
        stroke: frac >= 1 ? "var(--status-closed)" : "var(--accent)",
        "stroke-dasharray": (frac * c) + " " + c,
        transform: "rotate(-90 " + p.x + " " + p.y + ")"}));
    }
    g.appendChild(svgEl("text", {class: "node-label", x: p.x, y: p.y + 21}, p.name));
    g.appendChild(svgEl("title", {}, p.name + " \u00b7 " +
      stateLabel(st.state, r && r.headless) +
      (r && r.reasons && r.reasons.length ? ": " + r.reasons[0] : "")));
    g.addEventListener("click", () => openRole(p.name));
    g.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openRole(p.name); }
    });
    elMap.appendChild(g);
  }
  recolourPresence(model);
}

// On a phone the graph hides behind a button, so the presence strip carries the
// state colour instead — the same hue the node ring would draw.
function recolourPresence(model) {
  const states = {};
  for (const r of (model && model.roles) || []) states[r.role] = r.state;
  for (const row of elPresence.querySelectorAll(".agent-row")) {
    const st = states[row.dataset.agent];
    row.style.color = st ? (MAP_RING[st] || "") : "";
  }
}

function openMapDialog() {
  mountMap();
  if (!elMapDialog.open) {
    elMapDialog.showModal();
    if (PHONE_MQ.matches) document.body.style.overflow = "hidden";
  }
}
function closeMapDialog() {
  if (elMapDialog.open) elMapDialog.close();
}

if (elMapPanel) {
  let collapsed = false;
  try { collapsed = localStorage.getItem(MAP_COLLAPSE_KEY) === "1"; } catch (e) {}
  const head = elMapPanel.querySelector(".maphead");
  elMapPanel.classList.toggle("collapsed", collapsed);
  head.setAttribute("aria-expanded", String(!collapsed));
  head.addEventListener("click", () => {
    const next = !elMapPanel.classList.contains("collapsed");
    elMapPanel.classList.toggle("collapsed", next);
    head.setAttribute("aria-expanded", String(!next));
    try { localStorage.setItem(MAP_COLLAPSE_KEY, next ? "1" : "0"); } catch (e) {}
  });
}
if (elMapBtn) elMapBtn.addEventListener("click", openMapDialog);
if (elMapDialog) {
  elMapDialog.querySelector(".close").addEventListener("click", closeMapDialog);
  elMapDialog.addEventListener("close", () => {
    document.body.style.overflow = "";
    mountMap();                            // back into the sidebar for the next paint
  });
}
PHONE_MQ.addEventListener("change", mountMap);
mountMap();

/* ---- role detail dialog -------------------------------------------- */
const elRoleDialog = $("#roleDialog");
let roleDialogLock = null;

function roleAge(generated, since) {
  const g = Date.parse(generated), s = Date.parse(since);
  if (!isFinite(g) || !isFinite(s)) return "";
  const secs = Math.max(0, Math.round((g - s) / 1000));
  if (secs < 60) return secs + "s";
  if (secs < 3600) return Math.round(secs / 60) + "m";
  return Math.round(secs / 3600) + "h";
}

function newestMessage(msgs, pred) {
  let best = null;
  for (const m of msgs) if (pred(m) && (!best || m.id > best.id)) best = m;
  return best;
}

// Everything the operator needs before going to look at the tab. Both messages
// render through messageNode(), so they inherit md(), attachments and escaping.
function openRole(name) {
  const model = S.clan;
  const r = model && (model.roles || []).find(x => x.role === name);
  if (!r) return;
  elRoleDialog.querySelector(".rtitle").textContent = name;
  const body = elRoleDialog.querySelector(".rbody");
  body.replaceChildren();
  const dl = document.createElement("dl");
  dl.className = "rfacts";
  const fact = (k, v) => {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = v == null || v === "" ? "\u2014" : String(v);
    dl.append(dt, dd);
  };
  const age = roleAge(model.generated, r.since);
  fact("state", stateLabel(r.state, r.headless) + (age ? " for " + age : ""));
  if (r.awaiting) fact("waiting on", r.awaiting.text);   // textContent, like every fact
  fact("confidence", r.confidence);
  fact("terminal backend", model.terminal_backend || "zellij");
  if (r.terminal) {
    fact("terminal activity", r.terminal.state + (r.terminal.stale ? " (stale)" : ""));
    fact("terminal observed", r.terminal.at);
  }
  fact("harness", r.harness);
  fact("model", r.model);
  fact("writer", r.writer ? "yes" : "no");
  fact("branch", r.branch);
  fact("worktree", r.worktree);
  fact("tab / pane", [r.tab_id, r.pane_id].filter(x => x != null).join(" / "));
  body.appendChild(dl);
  if (r.reasons && r.reasons.length) {
    const ul = document.createElement("ul");
    ul.className = "rreasons";
    for (const reason of r.reasons) {
      const li = document.createElement("li");
      li.textContent = reason;                       // verbatim, from the closed vocabulary
      ul.appendChild(li);
    }
    body.appendChild(ul);
  }
  const ctx = document.createElement("div");
  ctx.className = "rctx";
  ctx.textContent = "context " + (r.context_tokens == null ? "?" : r.context_tokens.toLocaleString())
    + " / " + (r.checkpoint_at == null ? "?" : r.checkpoint_at.toLocaleString())
    + (r.last_checkpoint ? " \u00b7 last checkpoint " + r.last_checkpoint : "");
  body.appendChild(ctx);
  const msgs = Array.from(S.byId.values());
  const mention = newestMessage(msgs, m => m.from !== name && (m.mentions || []).includes(name));
  const own = newestMessage(msgs, m => m.from === name);
  const section = (label, m) => {
    const h = document.createElement("div");
    h.className = "rlabel";
    h.textContent = label;
    body.appendChild(h);
    if (m) {
      body.appendChild(messageNode(m, false));
    } else {
      const p = document.createElement("p");
      p.className = "rempty";
      p.textContent = "none on the bus yet";
      body.appendChild(p);
    }
  };
  section("newest mention of " + name, mention);
  section("newest post by " + name, own);
  if (!elRoleDialog.open) {
    elRoleDialog.showModal();
    if (PHONE_MQ.matches) {
      roleDialogLock = document.body.style.overflow;
      document.body.style.overflow = "hidden";
    }
  }
}

if (elRoleDialog) {
  elRoleDialog.querySelector(".close").addEventListener("click", () => elRoleDialog.close());
  elRoleDialog.addEventListener("close", () => {
    elRoleDialog.querySelector(".rbody").replaceChildren();
    if (roleDialogLock !== null) {
      document.body.style.overflow = roleDialogLock;
      roleDialogLock = null;
    }
  });
}
/* ---- end team map ---- */

/* ---- clan signature refresh ---------------------------------------- */
// The SSE `clan` event carries a signature, not the payload. Coalesce a burst
// into one refetch so the expensive model runs once per change, not per client
// per tick.
let clanRefreshTimer = 0;
function scheduleClanRefresh() {
  if (clanRefreshTimer) return;
  clanRefreshTimer = setTimeout(() => {
    clanRefreshTimer = 0;
    refreshClan(true);
  }, 250);
}

/* ---- channels ------------------------------------------------------ */
// Build one sidebar row. The server has already validated `repo`; the name,
// repo and checkout still go through textContent (never innerHTML), and the
// checkout is never an href.
function channelButton(c) {
  const b = document.createElement("button");
  b.dataset.channel = c.name;
  b.className = c.name === S.channel ? "active" : "";
  if (c.last_ts) b.title = c.name + " · last activity " + c.last_ts;
  const top = document.createElement("span");
  top.className = "top";
  const nm = document.createElement("span");
  nm.className = "nm";
  nm.textContent = c.name;
  const n = document.createElement("span");
  n.className = "n";
  n.textContent = c.count;
  top.append(nm, n);
  b.appendChild(top);
  const meta = channelMeta(c);
  if (meta) b.appendChild(meta);
  b.addEventListener("click", () => selectChannel(c.name));
  return b;
}

// The secondary line under the name: repo slug + shortened checkout. Null when
// the channel has no clan, so a plain channel keeps its one-line row.
function channelMeta(c) {
  const repo = c.repo, checkout = shortenPath(c.checkout);
  if (!repo && !checkout) return null;
  const meta = document.createElement("span");
  meta.className = "meta";
  if (repo) {
    const r = document.createElement("span");
    r.className = "repo";
    r.textContent = repo;
    meta.appendChild(r);
  }
  if (checkout) {
    const p = document.createElement("span");
    p.className = "co";
    p.textContent = checkout;
    meta.appendChild(p);
  }
  return meta;
}

// A checkout path shown `~`-relative and middle-ellipsised: on a worktree the
// tail (the repo directory) is the informative part, so a long path keeps it.
function shortenPath(p, max) {
  if (!p) return "";
  p = String(p).replace(/^\/(?:Users|home)\/[^/]+\//, "~/");
  max = max || 22;
  if (p.length <= max) return p;
  const head = Math.ceil((max - 1) / 2);
  return p.slice(0, head) + "\u2026" + p.slice(-(max - 1 - head));
}

// Re-render from S.channels, in activity order. Callable again: a live message
// re-sorts the list without a reload. The selected row's active state, focus
// and scroll position all survive the rebuild.
function renderChannels() {
  const focused = elChannels.contains(document.activeElement)
    ? document.activeElement.dataset.channel : null;
  const sidebarTop = elSidebar ? elSidebar.scrollTop : 0;
  const rowLeft = elChannels.scrollLeft;
  const sorted = S.channels.slice().sort(
    (a, b) => (b.last_id || "").localeCompare(a.last_id || ""));
  elChannels.replaceChildren(...sorted.map(channelButton));
  if (elSidebar) elSidebar.scrollTop = sidebarTop;
  elChannels.scrollLeft = rowLeft;
  if (focused) {
    // match by dataset, not a built selector: a channel name is not trusted to
    // be CSS-safe if a directory appeared outside `clan new`
    const b = [...elChannels.children].find(x => x.dataset.channel === focused);
    if (b) b.focus({ preventScroll: true });
  }
}

// A live message moved its channel to the top: raise its count and last id,
// then re-sort. The count mirrors the server's (all messages, replies too).
function bumpChannel(m) {
  const c = S.channels.find(x => x.name === S.channel);
  if (!c) return;
  c.count += 1;
  c.last_id = m.id;
  c.last_ts = m.ts;
  renderChannels();
}
