const $ = s => document.querySelector(s);
const elSidebar = $("#sidebar");
const elChannels = $("#channels"), elPresence = $("#presence"), elFilter = $("#filter");
const elPins = $("#pins"), elMessages = $("#messages"), elThread = $("#thread");
const elChanname = $("#channame"), elProgress = $("#progress");
const elThreadResize = $("#threadResize"), elPreview = $("#preview");
const elClanMeter = $("#clanMeter");

const S = { channel: null, es: null, seen: new Set(), last: "", lastDay: "",
            byId: new Map(), kids: new Map(), agents: new Set(), openThread: null,
            token: null, catalog: null, newestProposal: {}, approvedProposal: {},
            channels: [], clan: null, generation: 0, controller: null, threadController: null,
            threadGeneration: 0, nextBefore: null, paging: false, filters: {}, pinGeneration: 0,
            clanGeneration: 0 };

  const elSearch = $("#search"), elOperator = $("#operatorOnly"), elOlder = $("#olderMessages");
function routeHash(channel, thread=null) {
  const q = new URLSearchParams();
  if (thread) q.set("thread", thread);
  if (S.filters.query) q.set("q", S.filters.query);
  if (S.filters.mention) q.set("mention", S.filters.mention);
  if (S.filters.operator) q.set("operator", "1");
  return "#" + encodeURIComponent(channel) + (q.size ? "?" + q : "");
}
function saveRoute(thread=null) {
  if (!S.channel) return;
  const hash = routeHash(S.channel, thread);
  if (location.hash !== hash) history.pushState(null, "", hash);
  $("#channelLink").href = routeHash(S.channel);
}
function readRoute() {
  const [channel, query=""] = location.hash.slice(1).split("?");
  let name;
  try { name = decodeURIComponent(channel); } catch { name = ""; }
  const q = new URLSearchParams(query);
  return {channel:name, thread:q.get("thread"), query:q.get("q") || "",
          mention:q.get("mention") || "", operator:q.get("operator") === "1"};
}
async function fetchJSON(path, signal) {
  const response = await fetch(path, {signal});
  if (!response.ok) throw new Error("HTTP " + response.status);
  return response.json();
}
function connectionStatus(text, retry) {
  const box = $("#connection");
  box.textContent = text;
  if (retry) {
    const button = document.createElement("button");
    button.className = "retry-action"; button.textContent = "Retry";
    button.onclick = retry; box.appendChild(button);
  }
}
function matchesView(m) {
  const f = S.filters;
  return (!f.query || m.text.toLocaleLowerCase().includes(f.query.toLocaleLowerCase())) &&
    (!f.mention || (m.mentions || []).includes(f.mention)) &&
    (!f.operator || m.from === "stakeholder" || (m.mentions || []).includes("stakeholder"));
}
function historyURL(before) {
  const q = new URLSearchParams({limit:"100", q:S.filters.query || "", mention:S.filters.mention || "",
                                operator:S.filters.operator ? "1" : "0"});
  if (before) q.set("before", before);
  return "/api/channels/" + encodeURIComponent(S.channel) + "/history?" + q;
}
function submitFilters(e) {
  if (e) e.preventDefault();
  selectChannel(S.channel, {query:elSearch.value.trim(), mention:elFilter.value, operator:elOperator.checked});
}
$("#browse").addEventListener("submit", submitFilters);
elOperator.addEventListener("change", submitFilters);
window.addEventListener("hashchange", () => {
  const r = readRoute();
  if (r.channel) selectChannel(r.channel, r);
});
