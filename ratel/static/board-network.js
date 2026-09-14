async function refreshPins() {
  const generation = S.generation, request = ++S.pinGeneration;
  try {
    const d = await fetchJSON("/api/channels/" + encodeURIComponent(S.channel) + "/pins", S.controller.signal);
    if (generation !== S.generation || request !== S.pinGeneration) return;
    S.newestProposal[S.channel] = d.heads.proposed;
    S.approvedProposal[S.channel] = d.heads.approved;
    renderPins(d.pins); refreshClanHeads();
  } catch (error) {
    if (generation === S.generation && request === S.pinGeneration && error.name !== "AbortError")
      connectionStatus("Pinned messages unavailable.", refreshPins);
  }
}

// Per-role context meters from the clan endpoint. One track per role; the
// fill is tokens over checkpoint_at, red (--status-closed) past the line.
// A channel without a clan returns {"roles": []}: the block stays hidden.
let clanFetchedAt = 0;
async function refreshClan(force) {
  // throttle: one measurement per 10 s at most — every SSE tick is too often
  const now = Date.now();
  if (!force && now - clanFetchedAt < 10000) return;
  clanFetchedAt = now;
  if (!S.channel) return;
  const generation = S.generation, request = ++S.clanGeneration;
  try {
    const d = await fetchJSON("/api/channels/" + encodeURIComponent(S.channel) + "/clan", S.controller.signal);
    if (generation !== S.generation || request !== S.clanGeneration) return;
    paintMap(d);                    // one fetch feeds both the meter and the map
    const roles = d.roles || [];
    elClanMeter.hidden = !roles.length;
    elClanMeter.replaceChildren(...roles.map(r => {
      const over = r.context_tokens != null && r.context_tokens > r.checkpoint_at;
      const pct = r.context_tokens == null || !r.checkpoint_at ? 0
        : Math.min(100, r.context_tokens / r.checkpoint_at * 100);
      const div = document.createElement("div");
      div.className = "meter" + (over ? " over" : "");
      div.title = r.context_tokens == null ? r.role + ": ? tokens"
        : r.role + ": " + r.context_tokens.toLocaleString() + " / "
          + r.checkpoint_at.toLocaleString() + " tokens";
      const track = document.createElement("span");
      track.className = "track";
      const fill = document.createElement("span");
      fill.style.width = pct + "%";
      track.appendChild(fill);
      const nm = document.createElement("span");
      nm.className = "nm";
      nm.textContent = r.role;
      div.append(track, nm);
      return div;
    }));
  } catch { if (generation === S.generation && request === S.clanGeneration) elClanMeter.hidden = true; }
}

async function selectChannel(ch, route={}) {
  if (!/^[A-Za-z0-9_.-]+$/.test(ch) || ch === "." || ch === "..") return;
  if (S.controller) S.controller.abort();
  const controller = S.controller = new AbortController(), generation = ++S.generation;
  if (S.es) { S.es.close(); S.es = null; }
  if (clanRefreshTimer) { clearTimeout(clanRefreshTimer); clanRefreshTimer = 0; }
  clanFetchedAt = 0;
  closeThread(false);
  Object.assign(S, { channel: ch, seen: new Set(), last: "", byId: new Map(), kids: new Map(), agents: new Set(), lastDay: "",
                    nextBefore:null, paging:false, filters:{query:route.query || "", mention:route.mention || "", operator:!!route.operator} });
  closePreview();
  paintMap(null);
  elChanname.textContent = "#" + ch;
  elMessages.innerHTML = ""; elPins.innerHTML = ""; elPresence.innerHTML = "";
  elProgress.hidden = true; elClanMeter.hidden = true;
  elFilter.innerHTML = '<option value="">everyone</option>';
  if (S.filters.mention) { S.agents.add(S.filters.mention); elFilter.add(new Option("@" + S.filters.mention, S.filters.mention)); }
  elFilter.value = S.filters.mention;
  elSearch.value = S.filters.query; elOperator.checked = S.filters.operator;
  elOlder.hidden = true; $("#historyStatus").textContent = "";
  saveRoute(route.thread);
  renderChannels();
  connectionStatus("Loading messages…");
  const current = () => generation === S.generation;
  try {
    const d = await fetchJSON(historyURL(), controller.signal);
    if (!current()) return;
    S.newestProposal[ch] = d.heads.proposed; S.approvedProposal[ch] = d.heads.approved;
    for (const m of d.messages) ingest(m, false, true);
    S.last = d.tip || ""; S.nextBefore = d.next_before;
    elOlder.hidden = !S.nextBefore; elOlder.disabled = false;
    renderChannels();
    refreshPins(); refreshClan(true);
    if (!d.messages.length) {
      const empty = document.createElement("div"); empty.className = "empty-row";
      empty.textContent = "No matching messages."; elMessages.appendChild(empty);
    }
    if (route.thread) openThread(route.thread);
    connectStream(generation);
  } catch (error) {
    if (current() && error.name !== "AbortError")
      connectionStatus("Could not load messages.", () => selectChannel(ch, route));
  }
}

function connectStream(generation) {
  if (generation !== S.generation) return;
  if (S.es) S.es.close();
  const es = S.es = new EventSource("/api/channels/" + encodeURIComponent(S.channel) + "/events?since=" + encodeURIComponent(S.last));
  const current = () => generation === S.generation && S.es === es;
  connectionStatus("Connecting…");
  es.onopen = () => { if (current()) connectionStatus("Live"); };
  es.onerror = () => { if (current()) connectionStatus("Connection lost. Reconnecting…", () => connectStream(generation)); };
  for (const event of ["hello", "presence"]) es.addEventListener(event, e => {
    if (current()) renderPresence(JSON.parse(e.data).presence);
  });
  es.addEventListener("clan", () => { if (current()) scheduleClanRefresh(); });
  es.addEventListener("message", e => {
    if (!current()) return;
    const m = JSON.parse(e.data);
    S.last = m.id;
    if (!ingest(m, true)) return;
    if (m.pin || "unpin" in m || (m.attachments || []).some(a => inferType(a) === "clan")) refreshPins();
    refreshClan();
  });
}

async function loadOlder() {
  if (S.paging || !S.nextBefore) return;
  const generation = S.generation;
  S.paging = true; elOlder.disabled = true; $("#historyStatus").textContent = "Loading…";
  const anchor = elMessages.querySelector(".msg:not([hidden])");
  const top = anchor && anchor.getBoundingClientRect().top;
  try {
    const d = await fetchJSON(historyURL(S.nextBefore), S.controller.signal);
    if (generation !== S.generation) return;
    // Render the older page separately, then prepend it without moving existing nodes.
    const existing = [...elMessages.childNodes];
    existing.forEach(node => node.remove());
    const lastDay = S.lastDay;
    S.lastDay = "";
    for (const m of d.messages) ingest(m, false, true);
    elMessages.append(...existing); S.lastDay = lastDay;
    S.nextBefore = d.next_before; elOlder.hidden = !S.nextBefore;
    applyFilter();
    if (anchor) window.scrollBy(0, anchor.getBoundingClientRect().top - top);
    $("#historyStatus").textContent = S.nextBefore ? "" : "Beginning of matching history";
  } catch (error) {
    if (generation === S.generation && error.name !== "AbortError")
      $("#historyStatus").textContent = "Could not load older messages. Try again.";
  } finally {
    if (generation === S.generation) { S.paging = false; elOlder.disabled = false; }
  }
}
elOlder.addEventListener("click", loadOlder);

let bootGeneration = 0;
async function boot() {
  const request = ++bootGeneration;
  const q = new URLSearchParams(location.search);
  if (q.get("token")) {
    storeToken(q.get("token"));
    q.delete("token");                     // the token must not sit in the address bar
    history.replaceState(null, "", location.pathname +
      (q.toString() ? "?" + q : "") + location.hash);
  } else {
    try { S.token = localStorage.getItem(TOKEN_KEY); } catch (e) {}
  }
  let channels;
  connectionStatus("Loading channels…");
  try { ({channels} = await fetchJSON("/api/channels")); }
  catch { if (request === bootGeneration) connectionStatus("Could not load channels.", boot); return; }
  if (request !== bootGeneration) return;
  if (!channels.length) {
    connectionStatus("No channels yet");
    elMessages.innerHTML = '<p style="color:var(--text-muted)">No channels yet. Start an agent and post to one.</p>';
    return;
  }
  S.channels = channels;
  renderChannels();                        // the server already ordered them; sort again anyway
  const route = readRoute();
  selectChannel(route.channel || channels[0].name, route);
}
boot();
