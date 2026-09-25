const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
const asList = (value) => Array.isArray(value) ? value : [];
const isPublished = (lead) => asList(lead.emails).length > 0 && lead.email_origin === "scraped";
const isInferred = (lead) => asList(lead.emails).length > 0 && lead.email_origin !== "scraped";
const safeUrl = (value) => { try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) ? url.href : ""; } catch { return ""; } };
const safeEmail = (value) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(value)) ? String(value) : "";
const timeText = (seconds) => { const n = Math.max(0, Math.floor(Number(seconds) || 0)); return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, "0")}`; };
const titleCase = (value) => value === "saas" ? "SaaS" : String(value || "").replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase());
const dateText = (value) => { const date = new Date(value || ""); return Number.isNaN(date.getTime()) ? "Date unknown" : date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }); };

const state = { mode: "search", leads: [], status: null, visibleLimit: 20, loading: false, refreshing: false, hosted: false, hostedTest: false, lastLeads: "", lastLog: "" };
let toastTimer;
let csrfToken = "";

function notice(message, success = false) {
  const node = $("formNotice");
  node.textContent = message;
  node.classList.toggle("success", success);
  node.classList.remove("hidden");
}
function clearNotice() { $("formNotice").classList.add("hidden"); }
function toast(message) {
  const node = $("toast"); node.textContent = message; node.classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => node.classList.remove("show"), 2600);
}
function setMode(mode) {
  state.mode = mode;
  for (const [id, value] of [["modeSearch", "search"], ["modeSeeds", "seeds"]]) {
    const selected = mode === value;
    $(id).classList.toggle("selected", selected);
    $(id).setAttribute("aria-pressed", String(selected));
  }
  $("seedField").classList.toggle("hidden", mode !== "seeds");
  $("sourceHelp").textContent = mode === "seeds"
    ? "Your list skips search engines and goes straight to the listed websites."
    : "Free search providers may limit requests. Seed lists are useful when discovery is blocked.";
  clearNotice();
}
$("modeSearch").addEventListener("click", () => setMode("search"));
$("modeSeeds").addEventListener("click", () => setMode("seeds"));

const nicheBox = $("r_niches");
function selectedNiches() { return [...nicheBox.querySelectorAll(".niche-choice.selected")].map((button) => button.dataset.niche); }
function updateNicheCount() { const n = selectedNiches().length; $("nicheCount").textContent = `${n} selected`; }
nicheBox.addEventListener("click", (event) => {
  const button = event.target.closest(".niche-choice");
  if (!button || !nicheBox.contains(button)) return;
  if (button.classList.contains("selected") && selectedNiches().length === 1) { notice("Choose at least one industry."); return; }
  button.classList.toggle("selected");
  button.setAttribute("aria-pressed", String(button.classList.contains("selected")));
  updateNicheCount(); clearNotice();
});
for (const id of ["r_max", "r_out", "r_seeds"]) $(id).addEventListener("input", clearNotice);

async function submitRun() {
  if (state.loading || state.status?.running) return;
  const niches = selectedNiches();
  const max = Number($("r_max").value);
  const out = $("r_out").value.trim();
  const seeds = $("r_seeds").value.trim();
  if (!niches.length) return notice("Choose at least one industry.");
  if (!Number.isInteger(max) || max < 1 || max > 500) return notice("Enter a lead target between 1 and 500.");
  if (!/^(data|output)\/(?!.*\.\.)[^\\]+\.(csv|json)$/i.test(out)) return notice("Save results to a CSV or JSON file under data/ or output/.");
  if (state.mode === "seeds" && !/^data\/(?!.*\.\.)[^\\]+$/.test(seeds)) return notice("Enter an existing seed file under data/.");
  const payload = {
    niche: niches, max, out, seeds: state.mode === "seeds" ? seeds : "",
    min_quality: Number($("r_quality").value) || 0,
    emails_only: $("r_email").checked,
    no_enrich: $("r_noenrich").checked,
    fresh: $("r_fresh").checked,
  };
  state.loading = true;
  $("btnRun").disabled = true;
  $("btnRun").innerHTML = "Starting…";
  clearNotice();
  try {
    const response = await fetch("/api/run", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken }, body: JSON.stringify(payload) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    if (result.queued) {
      notice(result.message || "Scrape started. Leads will appear automatically as they are found.", true);
      state.lastLog = "";
    } else {
      notice("Scrape started. Live activity and results will update below.", true);
    }
    $("activity").scrollIntoView({ behavior: "smooth", block: "center" });
    await refresh();
  } catch (error) {
    notice(`Could not start the scrape: ${error.message}`);
  } finally {
    state.loading = false;
    $("btnRun").innerHTML = "Start scraping <span aria-hidden=\"true\">→</span>";
    $("btnRun").disabled = !!state.status?.running;
  }
}
$("btnRun").addEventListener("click", submitRun);
$("btnStop").addEventListener("click", async () => {
  $("btnStop").disabled = true;
  try {
    const response = await fetch("/api/stop", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken }, body: "{}" });
    const result = await response.json();
    if (!response.ok || result.ok === false) throw new Error(result.error || `HTTP ${response.status}`);
    toast("Stop requested");
    await refresh();
  } catch (error) { notice(`Could not stop the run: ${error.message}`); }
});

function renderStatus(status) {
  state.status = status;
  const running = !!status.running;
  const hosted = !!status.hosted;
  const hostedTest = !!status.hosted_test_mode;
  const jobStatus = String(status.job_status || "idle");
  const starting = hosted && jobStatus === "queued";
  const scraping = hosted && jobStatus === "running";
  state.hosted = hosted;
  state.hostedTest = hostedTest;
  const failed = !running && status.exit_code !== null && status.exit_code !== 0;
  const finished = !running && status.exit_code === 0;
  $("connection").className = `connection ${running ? "running" : failed ? "error" : ""}`;
  $("headerStatus").textContent = hosted ? (running ? (starting ? "Starting scraper" : "Scraping now") : "Scraper ready") : running ? "Scraping now" : failed ? "Run needs attention" : "Local & ready";
  $("hostedNotice").classList.toggle("hidden", !hostedTest);
  $("runFootnote").textContent = hostedTest ? "Scraping continues in the background" : "Runs locally on your machine";
  $("r_out").closest(".field-group").classList.toggle("hidden", hosted);
  $("runBadge").className = `run-state ${running ? "running" : failed ? "error" : "idle"}`;
  $("runBadge").lastElementChild.textContent = running ? "Running" : failed ? "Failed" : finished ? "Complete" : "Idle";
  $("activity").classList.toggle("running", running);
  const mode = status.seeds ? "seed list" : "web search";
  $("runTitle").textContent = hosted ? (starting ? "Starting your scrape" : scraping ? "Your scrape is underway" : failed ? "The last run did not finish" : finished ? "Leads are ready" : "Ready when you are") : running ? "Your scrape is underway" : failed ? "The last run did not finish" : finished ? "Last run complete" : "Ready when you are";
  const errorLine = (status.log_tail || []).slice().reverse().find((line) => /\| ERROR\s+\|/.test(line));
  const found = Number(status.leads_collected_log) || 0;
  $("runSummary").textContent = hosted
    ? (starting ? "The scraper is starting now. You can leave this page open and leads will appear automatically." : scraping ? "Websites are being searched and checked now. Completed leads will appear below." : failed ? (status.error || "The scraper could not complete this run.") : finished ? `Saved ${found} real lead${found === 1 ? "" : "s"}. Review them below.` : "Choose your options and press Start scraping. Real leads will appear below automatically.")
    : running
    ? `Working through ${status.niches?.length || 1} industry selection${status.niches?.length === 1 ? "" : "s"} using ${mode}.`
    : failed ? (errorLine ? errorLine.split(" | ").slice(-1)[0] : `Exit code ${status.exit_code}. Open the run log for details.`)
    : finished ? (found ? `Collected ${found} lead${found === 1 ? "" : "s"} and saved results to ${status.out_file || "the selected file"}.` : "The run finished without new leads. Your saved library is still available below.")
    : "Set up a scrape to see discovery, progress, and saved leads here.";
  const max = Math.max(1, (Number(status.max) || 0) * Math.max(1, status.niches?.length || 1));
  const pct = running || finished ? Math.min(100, Math.round(found / max * 100)) : 0;
  $("runProgress").style.width = `${pct}%`;
  $("runProgressTrack").setAttribute("aria-valuenow", String(pct));
  $("progressLabel").textContent = running ? "Leads collected so far" : finished ? "Leads collected in last run" : failed ? "Run stopped with an error" : "No run in progress";
  $("progressAmount").textContent = running || finished ? `${found} / ${max}` : "—";
  $("metricProbed").textContent = status.candidates_probed || 0;
  $("metricSearches").textContent = status.searches || 0;
  $("metricFound").textContent = found;
  $("runCurrent").textContent = hosted ? (starting ? "Starting the scraper…" : scraping ? "Searching websites and collecting contacts…" : failed ? "Open the run log to see what happened." : finished ? "Ready for another run." : "Waiting for a run") : running ? status.current_url || status.current_query || "Preparing candidates…" : failed ? "Open the run log to see what happened." : finished ? "Ready for another run." : "Waiting for a run";
  $("runTime").textContent = `${running ? "Elapsed" : "Last duration"} ${status.elapsed_s ? timeText(status.elapsed_s) : "—"}`;
  $("btnRun").disabled = running || state.loading;
  $("btnRun").innerHTML = "Start scraping <span aria-hidden=\"true\">→</span>";
  $("btnStop").disabled = !running;
  const lines = status.log_tail || [];
  const logText = lines.length ? lines.join("\n") : "No run output yet.";
  if (state.lastLog !== logText) {
    const log = $("log"); const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
    log.textContent = logText; if (atBottom) log.scrollTop = log.scrollHeight;
    state.lastLog = logText;
  }
  $("logLine").textContent = lines.length ? lines[lines.length - 1].slice(0, 95) : "Latest output appears here";
}

function updateNicheFilter(leads) {
  const select = $("f_niche");
  const previous = select.value;
  const values = [...new Set(leads.map((lead) => lead.niche).filter(Boolean))].sort();
  select.replaceChildren(new Option("All industries", ""), ...values.map((value) => new Option(titleCase(value), value)));
  if (values.includes(previous)) select.value = previous;
}
function leadCard(lead, index) {
  const site = safeUrl(lead.website);
  const host = site ? new URL(site).hostname.replace(/^www\./, "") : "Website unavailable";
  const email = safeEmail(asList(lead.emails)[0]);
  const trust = isPublished(lead) ? "published" : isInferred(lead) ? "inferred" : "none";
  const primary = trust === "published" && email
    ? `<small>Published email</small><a href="mailto:${esc(email)}">${esc(email)}</a>`
    : trust === "inferred" ? `<small>Unverified email guess</small><strong>${esc(email || "See details")}</strong>`
    : `<small>Contact options</small><strong>${asList(lead.whatsapp_numbers).length ? "WhatsApp available" : asList(lead.phones).length ? "Phone available" : "No email found"}</strong>`;
  const channels = [["Email", asList(lead.emails).length], ["WhatsApp", asList(lead.whatsapp_numbers).length], ["Instagram", asList(lead.instagram_handles).length], ["LinkedIn", asList(lead.linkedin_urls).length], ["Phone", asList(lead.phones).length]]
    .filter((entry) => entry[1]).map(([label, count]) => `<span class="channel">${esc(label)} ${count}</span>`).join("");
  const name = String(lead.business_name || host);
  const quality = ["high", "medium", "low"].includes(lead.quality_label) ? lead.quality_label : "low";
  return `<article class="lead-card"><div class="lead-top"><div class="lead-avatar" aria-hidden="true">${esc(name.trim()[0]?.toUpperCase() || "?")}</div><div class="lead-title"><h3 title="${esc(name)}">${esc(name)}</h3>${site ? `<a href="${esc(site)}" target="_blank" rel="noopener noreferrer">${esc(host)} ↗</a>` : `<span>${esc(host)}</span>`}</div><span class="quality-tag ${quality}">${quality}</span></div><div class="lead-primary ${trust}">${primary}</div><div class="lead-meta"><span class="tag">${esc(titleCase(lead.niche))}</span><span class="lead-channels">${channels}</span></div><div class="lead-bottom"><span>${esc(dateText(lead.scraped_at))} · score ${Number(lead.quality_score) || 0}</span><button type="button" class="details-button" data-lead-index="${index}">View details →</button></div></article>`;
}

function renderLibrary() {
  const leads = state.leads;
  const published = leads.filter(isPublished).length;
  const inferred = leads.filter(isInferred).length;
  const other = leads.length - published - inferred;
  $("heroTotal").textContent = leads.length;
  $("heroPublished").textContent = published;
  $("heroInferred").textContent = inferred;
  $("resultTotal").textContent = leads.length;
  $("publishedCount").textContent = published;
  $("inferredCount").textContent = inferred;
  $("otherCount").textContent = other;
  const query = $("q").value.trim().toLowerCase();
  const niche = $("f_niche").value;
  const origin = $("f_origin").value;
  const quality = $("f_q").value;
  const filtered = leads.map((lead, index) => ({ lead, index })).filter(({ lead }) => {
    if (niche && lead.niche !== niche) return false;
    if (origin === "published" && !isPublished(lead)) return false;
    if (origin === "inferred" && !isInferred(lead)) return false;
    if (origin === "none" && asList(lead.emails).length) return false;
    if (quality && lead.quality_label !== quality) return false;
    if (query && ![lead.business_name, lead.website, lead.niche, lead.source_query, ...asList(lead.emails), ...asList(lead.phones)].join(" ").toLowerCase().includes(query)) return false;
    return true;
  });
  const sort = $("f_sort").value;
  filtered.sort((a, b) => sort === "name" ? String(a.lead.business_name).localeCompare(String(b.lead.business_name))
    : sort === "recent" ? String(b.lead.scraped_at).localeCompare(String(a.lead.scraped_at))
    : (Number(b.lead.quality_score) || 0) - (Number(a.lead.quality_score) || 0));
  const visible = filtered.slice(0, state.visibleLimit);
  $("leadGrid").innerHTML = visible.map(({ lead, index }) => leadCard(lead, index)).join("");
  $("resultCount").textContent = `Showing ${visible.length} of ${filtered.length} matching leads`;
  $("emptyState").classList.toggle("hidden", filtered.length > 0);
  $("showMore").classList.toggle("hidden", filtered.length <= state.visibleLimit);
  $("emptyText").textContent = !leads.length ? "Start a scrape to build your lead library."
    : origin === "published" && !published ? "No published emails saved yet. View all leads to inspect other contact channels and unverified guesses."
    : "Try another search or filter to see more leads.";
  $("clearFilters").textContent = !leads.length ? "Set up a scrape" : "Show all leads";
  $("dataSource").textContent = state.status?.source_file ? `Latest file: ${state.status.source_file}` : "";
}
for (const id of ["q", "f_niche", "f_origin", "f_q", "f_sort"]) $(id).addEventListener(id === "q" ? "input" : "change", () => { state.visibleLimit = 20; renderLibrary(); });
$("showMore").addEventListener("click", () => { state.visibleLimit += 20; renderLibrary(); });
$("clearFilters").addEventListener("click", () => {
  if (!state.leads.length) return $("workspace").scrollIntoView({ behavior: "smooth" });
  $("q").value = ""; $("f_niche").value = ""; $("f_origin").value = "all"; $("f_q").value = "";
  state.visibleLimit = 20; renderLibrary();
});

function detailSection(title, values, format) {
  if (!values.length) return "";
  return `<section class="detail-section"><h3>${esc(title)}</h3><div class="detail-items">${values.map(format).join("")}</div></section>`;
}
function showDetails(index) {
  const lead = state.leads[index]; if (!lead) return;
  $("dialogTitle").textContent = lead.business_name || "Business details";
  const site = safeUrl(lead.website);
  const trust = isPublished(lead) ? "published" : isInferred(lead) ? "inferred" : "none";
  const emailItems = asList(lead.emails).map((value) => {
    const email = safeEmail(value);
    return trust === "published" && email
      ? `<span class="detail-item"><a class="detail-site" href="mailto:${esc(email)}">${esc(email)}</a><button class="detail-copy" data-copy="${esc(email)}" type="button">Copy</button></span>`
      : `<span class="detail-item">${esc(value)}</span>`;
  });
  const social = asList(lead.instagram_handles).map((handle) => {
    const clean = String(handle).replace(/^@/, "").replace(/[^\w.]/g, "");
    return clean ? `<a class="detail-item" href="https://instagram.com/${encodeURIComponent(clean)}" target="_blank" rel="noopener noreferrer">Instagram · @${esc(clean)} ↗</a>` : "";
  }).concat(asList(lead.linkedin_urls).map((value) => {
    const url = safeUrl(value); return url ? `<a class="detail-item" href="${esc(url)}" target="_blank" rel="noopener noreferrer">LinkedIn · ${esc(new URL(url).pathname.replace(/^\//, ""))} ↗</a>` : "";
  })).filter(Boolean);
  const wa = asList(lead.whatsapp_numbers).map((value) => {
    const digits = String(value).replace(/\D/g, ""); return digits ? `<a class="detail-item" href="https://wa.me/${digits}" target="_blank" rel="noopener noreferrer">WhatsApp · ${esc(value)} ↗</a>` : "";
  }).filter(Boolean);
  const phones = asList(lead.phones).map((value) => `<span class="detail-item">${esc(value)}</span>`);
  $("dialogBody").innerHTML = `${site ? `<a class="detail-site" href="${esc(site)}" target="_blank" rel="noopener noreferrer">${esc(site)} ↗</a>` : ""}<div class="detail-flags"><span class="detail-flag">${esc(titleCase(lead.niche))}</span><span class="detail-flag ${trust}">${trust === "published" ? "Published email" : trust === "inferred" ? "Inferred email" : "No email"}</span><span class="detail-flag">Quality ${Number(lead.quality_score) || 0} · ${esc(lead.quality_label || "low")}</span></div>${trust === "inferred" ? `<p class="detail-warning">These addresses were inferred from a mail-capable domain. Their individual mailboxes have not been verified, so review them before any outreach.</p>` : ""}${detailSection("Email addresses", emailItems, (item) => item)}${detailSection("Social profiles", social, (item) => item)}${detailSection("WhatsApp", wa, (item) => item)}${detailSection("Phone numbers", phones, (item) => item)}<section class="detail-section"><h3>Discovery</h3><div class="detail-muted">${lead.source_query === "seed" ? "Seed list" : esc(lead.source_query || "Search result")} · collected ${esc(dateText(lead.scraped_at))}</div></section>`;
  $("leadDialog").showModal();
}
$("leadGrid").addEventListener("click", (event) => { const button = event.target.closest("[data-lead-index]"); if (button) showDetails(Number(button.dataset.leadIndex)); });
$("dialogClose").addEventListener("click", () => $("leadDialog").close());
$("leadDialog").addEventListener("click", (event) => { if (event.target === $("leadDialog")) $("leadDialog").close(); });
$("dialogBody").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy]"); if (!button) return;
  try { await navigator.clipboard.writeText(button.dataset.copy); toast("Email copied"); }
  catch { toast("Could not copy. Select the address instead."); }
});

async function refresh() {
  if (state.refreshing) return;
  state.refreshing = true;
  try {
    const [statusResponse, leadsResponse] = await Promise.all([fetch("/api/status"), fetch("/api/leads")]);
    if (statusResponse.status === 401 || leadsResponse.status === 401) { window.location.replace("/login"); return; }
    if (!statusResponse.ok || !leadsResponse.ok) throw new Error("Dashboard unavailable");
    const [status, data] = await Promise.all([statusResponse.json(), leadsResponse.json()]);
    renderStatus(status);
    const leads = asList(data.leads);
    const digest = JSON.stringify(leads);
    if (digest !== state.lastLeads) { state.leads = leads; state.lastLeads = digest; updateNicheFilter(leads); renderLibrary(); }
    $("lastUpdated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch {
    $("connection").className = "connection error";
    $("headerStatus").textContent = "Dashboard offline";
    $("lastUpdated").textContent = "Connection lost · retrying";
  } finally { state.refreshing = false; }
}
document.getElementById("signOut").addEventListener("click", async () => {
  try { await fetch("/api/logout", { method: "POST", headers: { "X-CSRF-Token": csrfToken } }); }
  finally { window.location.replace("/login"); }
});
async function startDashboard() {
  try {
    const response = await fetch("/api/session");
    if (!response.ok) { window.location.replace("/login"); return; }
    const session = await response.json();
    csrfToken = session.csrf;
    document.getElementById("userName").textContent = session.username;
    refresh();
    setInterval(refresh, 2000);
  } catch { window.location.replace("/login"); }
}
startDashboard();
