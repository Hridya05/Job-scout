"use strict";

(() => {
  // UI timing and page size live here; matching and source settings use the forms.
  const CONFIG = Object.freeze({
    pageSize: 50,
    scanningPollMs: 3000,
    idlePollMs: 15000,
    searchDelayMs: 300
  });
  const VIEWS = { matches: "matching jobs", all: "jobs", saved: "saved jobs", applied: "applications" };
  const STATUSES = {
    new: "New",
    saved: "Saved",
    applied: "Applied",
    interviewing: "Interviewing",
    offer: "Offer",
    rejected: "Rejected",
    dismissed: "Dismissed"
  };
  const APPLICATION_STATUSES = new Set(["applied", "interviewing", "offer", "rejected"]);
  const ADAPTERS = { google: "Google Careers", oracle: "Oracle Careers", jsonld: "JSON-LD", auto: "Auto-detect" };
  const $ = (id) => document.getElementById(id);
  const numberFormat = new Intl.NumberFormat();
  const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });
  const timeFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

  let state = null;
  let view = "matches";
  let query = "";
  let offset = 0;
  let total = 0;
  let jobsLoaded = false;
  let jobsBusy = false;
  let preferencesInitialized = false;
  let preferencesSaving = false;
  let preferencesDirty = false;
  let sourceSaving = false;
  let exporting = false;
  let sourceDirty = false;
  let editingSourceId = null;
  let scanStarting = false;
  let connectionFailed = false;
  let stateRequest = 0;
  let jobsRequest = 0;
  let stateController = null;
  let jobsController = null;
  let pollTimer = null;
  let searchTimer = null;
  let sourceSignature = "";
  let emptyAction = null;
  const pendingSources = new Set();
  const pendingJobs = new Map();
  const jobCards = new Map();

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function setText(node, value) {
    const text = String(value ?? "");
    if (node.textContent !== text) node.textContent = text;
  }

  function safeUrl(value) {
    if (typeof value !== "string") return null;
    try {
      const parsed = new URL(value);
      return ["http:", "https:"].includes(parsed.protocol) && !parsed.username && !parsed.password
        ? parsed.href : null;
    } catch {
      return null;
    }
  }

  function externalLink(label, url, className) {
    const href = safeUrl(url);
    if (!href) return null;
    const link = element("a", className, label);
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.setAttribute("aria-label", `${label} (opens in a new tab)`);
    return link;
  }

  function formatDate(value, withTime = false) {
    if (!value) return "Not provided";
    const date = new Date(typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T00:00:00` : value);
    return Number.isNaN(date.getTime()) ? String(value) : (withTime ? timeFormat : dateFormat).format(date);
  }

  function datedLabel(label, value, withTime = false) {
    const wrapper = element("span");
    wrapper.append(document.createTextNode(`${label} `));
    const time = element("time", "", formatDate(value, withTime));
    const parsed = value ? new Date(value) : null;
    if (parsed && !Number.isNaN(parsed.getTime())) time.dateTime = parsed.toISOString();
    wrapper.append(time);
    return wrapper;
  }

  function announce(message) {
    setText($("feedback"), message);
  }

  function showError(context, error) {
    setText($("error-message"), `${context}: ${error.message || String(error)}`);
    $("error-banner").hidden = false;
  }

  async function api(path, { method = "GET", data, signal, responseType = "json" } = {}) {
    const headers = { Accept: responseType === "blob" ? "text/csv" : "application/json" };
    const options = { method, headers, signal, credentials: "same-origin", cache: "no-store" };
    if (method !== "GET") {
      headers["Content-Type"] = "application/json";
      headers["X-Job-Scout"] = "1";
      options.body = JSON.stringify(data ?? {});
    }
    let response;
    try {
      response = await fetch(path, options);
    } catch (error) {
      if (signal && signal.aborted) throw error;
      throw new Error("Could not reach the local app. Check that Job Scout is still running.");
    }
    if (response.ok && responseType === "blob") return response.blob();
    const text = await response.text();
    let payload;
    try {
      payload = text ? JSON.parse(text) : {};
    } catch {
      throw new Error(`The server returned an unreadable response (HTTP ${response.status}).`);
    }
    if (!response.ok) {
      throw new Error(payload && typeof payload.error === "string" ? payload.error : `Request failed (HTTP ${response.status}).`);
    }
    return payload;
  }

  function invalidateState() {
    stateRequest += 1;
    if (stateController) stateController.abort();
    clearTimeout(pollTimer);
  }

  function invalidateJobs() {
    jobsRequest += 1;
    if (jobsController) jobsController.abort();
    jobsBusy = false;
    $("job-list").setAttribute("aria-busy", "false");
    $("results-loading").hidden = true;
    renderPagination();
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    const delay = state && state.scan.running ? CONFIG.scanningPollMs : CONFIG.idlePollMs;
    pollTimer = setTimeout(() => refreshState(), delay);
  }

  function renderScan() {
    const running = scanStarting || Boolean(state && state.scan.running);
    const enabled = Boolean(state && state.sources.some((source) => source.enabled));
    $("source-fields").disabled = !state || sourceSaving || running;
    $("source-scan-note").hidden = !running;
    $("scan-button").disabled = !state || running || !enabled;
    $("scan-button").classList.toggle("is-running", running);
    setText($("scan-button-label"), running ? "Scanning career pages…" : "Scan career pages");
    $("scan-button").title = state && !enabled ? "Enable or add a career source first." : "";
    const indicator = $("scan-indicator");
    indicator.className = "scan-indicator";
    if (connectionFailed) {
      indicator.classList.add("is-error");
      setText($("scan-status"), "Connection interrupted");
    } else if (running) {
      indicator.classList.add("is-running");
      setText($("scan-status"), "Looking for your next opportunity");
    } else if (state) {
      indicator.classList.add("is-ready");
      setText($("scan-status"), enabled ? "Ready when you are" : "Add or enable a source to begin");
    }
    if (!state) return;
    renderSources();
    const lastRun = state.scan.last_run;
    setText($("scan-last-run"), lastRun
      ? (lastRun.finished_at ? `Last scan: ${formatDate(lastRun.finished_at, true)}` : `Started: ${formatDate(lastRun.started_at, true)}`)
      : "No scans yet. Your first discovery starts here.");
    setText($("scan-next-run"), state.scan.next_run_at
      ? `Next scan: ${formatDate(state.scan.next_run_at, true)} · while the app is open`
      : state.preferences.scan_interval_minutes
        ? `Scheduled every ${state.preferences.scan_interval_minutes} minutes while the app is open.`
        : "Manual scans · scheduling is off.");
    const error = state.scan.last_error || (lastRun && lastRun.error);
    $("scan-error").hidden = !error;
    setText($("scan-error"), error ? `Last scan issue: ${error}` : "");
    if (emptyAction === "scan") $("empty-action").disabled = $("scan-button").disabled;
  }

  function renderCounts() {
    for (const key of ["total", "matches", "saved", "applied"]) {
      setText($(`count-${key}`), numberFormat.format(state.counts[key] || 0));
    }
    for (const key of Object.keys(VIEWS)) {
      setText($(`tab-${key}`), numberFormat.format(state.counts[key === "all" ? "total" : key] || 0));
    }
  }

  function fillPreferences(preferences) {
    $("keywords").value = preferences.keywords.join(", ");
    $("locations").value = preferences.locations.join(", ");
    $("exclude-keywords").value = preferences.exclude_keywords.join(", ");
    $("remote-only").checked = preferences.remote_only;
    $("scan-interval").value = preferences.scan_interval_minutes;
    $("max-pages").value = preferences.max_pages;
  }

  function renderSources() {
    const running = scanStarting || Boolean(state.scan.running);
    const signature = JSON.stringify([state.sources, [...pendingSources], running]);
    if (signature === sourceSignature) return;
    sourceSignature = signature;
    const list = $("source-list");
    const focusedId = list.contains(document.activeElement) ? document.activeElement.id : null;
    const fragment = document.createDocumentFragment();
    if (!state.sources.length) fragment.append(element("p", "small muted", "No sources yet. Add an official career page below, then run a scan."));
    for (const source of state.sources) {
      const busy = running || pendingSources.has(source.id);
      const card = element("article", "source-card");
      card.append(element("h3", "source-name", source.name));
      card.append(externalLink(source.url, source.url, "source-url") || element("span", "source-url", "Source URL unavailable"));
      card.append(element("span", "source-kind", ADAPTERS[source.kind] || source.kind));
      const controls = element("div", "source-controls");
      const toggle = element("button", "source-toggle", source.enabled ? "Enabled" : "Disabled");
      toggle.type = "button";
      toggle.id = `source-toggle-${source.id}`;
      toggle.disabled = busy;
      toggle.setAttribute("aria-pressed", String(Boolean(source.enabled)));
      toggle.setAttribute("aria-label", `${source.enabled ? "Disable" : "Enable"} ${source.name}`);
      toggle.addEventListener("click", () => changeSource(source, false));
      const secondary = element("div", "source-secondary");
      const edit = element("button", "text-button", "Edit");
      edit.type = "button";
      edit.id = `source-edit-${source.id}`;
      edit.disabled = busy;
      edit.setAttribute("aria-label", `Edit ${source.name}`);
      edit.addEventListener("click", () => editSource(source));
      const remove = element("button", "text-button text-button-danger", "Delete");
      remove.type = "button";
      remove.id = `source-delete-${source.id}`;
      remove.disabled = busy;
      remove.setAttribute("aria-label", `Delete ${source.name} and its jobs`);
      remove.addEventListener("click", () => changeSource(source, true));
      secondary.append(edit, remove);
      controls.append(toggle, secondary);
      card.append(controls);
      card.append(element("p", "source-meta", `${numberFormat.format(source.last_count || 0)} jobs in last scan · ${source.last_scan ? formatDate(source.last_scan, true) : "Not scanned yet"}`));
      if (source.last_error) card.append(element("p", "source-message source-error", `Error: ${source.last_error}`));
      if (source.last_warning) card.append(element("p", "source-message source-warning", `Warning: ${source.last_warning}`));
      fragment.append(card);
    }
    list.replaceChildren(fragment);
    list.setAttribute("aria-busy", "false");
    if (focusedId) {
      const replacement = $(focusedId);
      if (replacement && !replacement.disabled) replacement.focus({ preventScroll: true });
      else $("sources-title").focus({ preventScroll: true });
    }
  }

  async function refreshState({ refreshResults = false } = {}) {
    if (refreshResults) invalidateJobs();
    invalidateState();
    const request = stateRequest;
    const controller = new AbortController();
    stateController = controller;
    let refreshJobs = refreshResults;
    try {
      const next = await api("/api/state", { signal: controller.signal });
      if (request !== stateRequest) return;
      if (!next || !next.preferences || !next.scan || !next.counts || !Array.isArray(next.sources)) {
        throw new Error("The server returned an incomplete workspace state.");
      }
      const previous = state;
      const finished = Boolean(previous && (
        (previous.scan.running && !next.scan.running) ||
        (next.scan.last_run && next.scan.last_run.finished_at &&
          next.scan.last_run.finished_at !== (previous.scan.last_run && previous.scan.last_run.finished_at))
      ));
      state = next;
      connectionFailed = false;
      if (!preferencesInitialized) {
        fillPreferences(state.preferences);
        preferencesInitialized = true;
        $("preferences-fields").disabled = false;
        setText($("preferences-save-state"), "Preferences are up to date.");
      }
      renderScan();
      renderCounts();
      renderSources();
      if (finished) {
        announce(state.scan.last_error || state.sources.some((source) => source.last_error || source.last_warning)
          ? "Scan finished. Review any scan or source warnings below."
          : "Scan finished. Refreshing your job board.");
      }
      refreshJobs = refreshJobs || !jobsLoaded || finished;
      if (!refreshJobs && total === 0 && !jobsBusy) renderEmpty();
    } catch (error) {
      // Superseded reads are intentionally aborted, not hidden API failures.
      if (controller.signal.aborted) return;
      connectionFailed = true;
      renderScan();
      showError("Unable to refresh the workspace", error);
      if (!state) {
        $("source-list").setAttribute("aria-busy", "false");
        $("source-list").replaceChildren(element("p", "small muted", "Career sources could not be loaded."));
        setText($("preferences-save-state"), "Connect to the app to load your preferences.");
        $("results-loading").hidden = true;
        $("job-list").setAttribute("aria-busy", "false");
        setText($("results-summary"), "The workspace could not be loaded. Use Retry connection above.");
        setText($("results-total"), "Unavailable");
      }
    } finally {
      if (request === stateRequest) {
        if (refreshJobs) await loadJobs();
        if (request === stateRequest) schedulePoll();
      }
    }
  }

  function renderEmpty() {
    const neverScanned = Boolean(state && !state.scan.last_run && state.counts.total === 0 &&
      !state.sources.some((source) => source.last_scan));
    const running = scanStarting || Boolean(state && state.scan.running);
    let title;
    let description;
    emptyAction = null;
    if (query) {
      title = "No jobs found for that search";
      description = `Nothing in this view contains “${query}”. Try a different search or clear it to see this view’s jobs.`;
      emptyAction = "clear";
    } else if (neverScanned && running) {
      title = "Your first scan is underway";
      description = "We’re checking your enabled career sources. Your jobs will appear when the scan finishes.";
    } else if (neverScanned) {
      title = "Start with a fresh scan";
      description = "Choose your preferences and scan your enabled career sources to discover your first jobs.";
      emptyAction = "scan";
    } else if (view === "saved") {
      title = "A shortlist starts with one role";
      description = "Set a job’s status to Saved to keep it here. Your shortlist stays with you between scans.";
      emptyAction = "all";
    } else if (view === "applied") {
      title = "Your next chapter is still ahead";
      description = "Apply on the official posting, then choose Applied. Track interviews, offers, outcomes, and follow-up notes here.";
      emptyAction = "all";
    } else if (view === "matches") {
      title = "No matches just yet";
      description = "Try broader keywords or locations, or turn off remote-only filtering. Browse all discovered jobs and review any source warnings.";
      emptyAction = "all";
    } else {
      title = "No jobs discovered yet";
      description = "Check that your sources are enabled, review any errors or warnings, and try another scan.";
      emptyAction = "scan";
    }
    setText($("empty-title"), title);
    setText($("empty-description"), description);
    $("empty-action").hidden = !emptyAction;
    setText($("empty-action"), { clear: "Clear search", scan: neverScanned ? "Run your first scan" : "Scan career pages", all: "Browse all jobs" }[emptyAction] || "");
    $("empty-action").disabled = emptyAction === "scan" && $("scan-button").disabled;
    $("results-empty").hidden = false;
  }

  function renderNotes(record) {
    const pending = pendingJobs.get(record.job.id);
    const saving = pending && Object.hasOwn(pending, "application_notes");
    record.notesSave.disabled = Boolean(pending) || !record.draft.dirty;
    setText(record.notesSave, saving ? "Saving…" : "Save notes");
    record.notesMessage.classList.toggle("is-error", Boolean(record.draft.error));
    record.notesMessage.classList.toggle("is-dirty", record.draft.dirty && !record.draft.error);
    setText(record.notesMessage, record.draft.error || (saving ? "Saving your notes…" : record.draft.dirty ? "Unsaved changes" : "Notes are saved explicitly."));
    setText(record.notesSummary, record.draft.dirty ? "Application notes · unsaved" : record.draft.text ? "Application notes · added" : "Add application notes");
  }

  function createJobCard(job) {
    const card = element("article", "job-card");
    card.dataset.jobId = String(job.id);
    card.setAttribute("aria-labelledby", `job-title-${job.id}`);
    const top = element("div", "job-card-top");
    const heading = element("div");
    const company = element("p", "job-company");
    const title = element("h3", "job-title");
    title.id = `job-title-${job.id}`;
    heading.append(company, title);
    const badges = element("div", "job-badges");
    top.append(heading, badges);
    const location = element("p", "job-location");
    const metadata = element("div", "job-meta");
    const details = element("details", "job-details");
    details.append(element("summary", "", "View description"));
    const description = element("div", "job-description");
    description.tabIndex = 0;
    details.append(description);
    const footer = element("div", "job-footer");
    const statusField = element("div", "status-field");
    const statusLabel = element("label", "", "Status");
    statusLabel.htmlFor = `job-status-${job.id}`;
    const statusSelect = element("select");
    statusSelect.id = `job-status-${job.id}`;
    for (const [value, label] of Object.entries(STATUSES)) {
      const option = element("option", "", label);
      option.value = value;
      statusSelect.append(option);
    }
    statusField.append(statusLabel, statusSelect);
    const posting = element("div");
    footer.append(statusField, posting);
    const notesDetails = element("details", "notes-details");
    const notesSummary = element("summary", "", "Add application notes");
    const notesEditor = element("div", "notes-editor");
    const notesLabel = element("label", "sr-only", "Application notes");
    notesLabel.htmlFor = `job-notes-${job.id}`;
    const notesInput = element("textarea");
    notesInput.id = `job-notes-${job.id}`;
    notesInput.rows = 3;
    notesInput.placeholder = "Recruiter, application link, interview dates, next steps…";
    notesInput.setAttribute("aria-describedby", `job-notes-state-${job.id}`);
    const notesActions = element("div", "notes-actions");
    const notesMessage = element("span", "notes-status");
    notesMessage.id = `job-notes-state-${job.id}`;
    notesMessage.setAttribute("role", "status");
    const notesSave = element("button", "button button-outline button-small", "Save notes");
    notesSave.type = "button";
    notesActions.append(notesMessage, notesSave);
    notesEditor.append(notesLabel, notesInput, notesActions);
    notesDetails.append(notesSummary, notesEditor);
    card.append(top, location, metadata, details, footer, notesDetails);
    const record = {
      job, card, company, title, badges, location, metadata, description, statusSelect, posting,
      notesDetails, notesSummary, notesInput, notesSave, notesMessage,
      draft: { text: job.application_notes || "", savedText: job.application_notes || "", dirty: false, error: "" }
    };
    notesInput.value = record.draft.text;
    statusSelect.addEventListener("change", () => changeJob(record, { status: statusSelect.value }));
    notesInput.addEventListener("input", () => {
      record.draft.text = notesInput.value;
      record.draft.dirty = record.draft.text !== record.draft.savedText;
      record.draft.error = "";
      renderNotes(record);
    });
    notesSave.addEventListener("click", () => changeJob(record, { application_notes: record.draft.text }));
    return record;
  }

  function updateJobCard(record, job) {
    record.job = job;
    const pending = pendingJobs.get(job.id);
    const status = (pending && pending.status) || (Object.hasOwn(STATUSES, job.status) ? job.status : "new");
    record.card.classList.toggle("is-dismissed", status === "dismissed");
    setText(record.company, job.company || job.source_name || "Company not specified");
    setText(record.title, job.title || "Untitled role");
    record.badges.replaceChildren(element("span", `badge badge-${status}`, STATUSES[status]));
    if (job.matches && view !== "matches") record.badges.append(element("span", "badge badge-match", "Match"));
    record.location.replaceChildren(document.createTextNode(Array.isArray(job.locations) && job.locations.length ? job.locations.join(" · ") : "Location not specified"));
    if (job.remote) record.location.append(element("span", "remote-label", "Remote"));
    record.metadata.replaceChildren(
      element("span", "job-source", job.source_name || "Career source"),
      datedLabel("Posted", job.posted_at),
      datedLabel("Last seen", job.last_seen, true)
    );
    if (APPLICATION_STATUSES.has(status)) record.metadata.append(datedLabel("Applied", job.applied_at));
    setText(record.description, job.description || "This source did not provide a description. Visit the official posting for full details.");
    record.description.setAttribute("aria-label", `Description for ${job.title || "this role"}`);
    record.statusSelect.value = status;
    record.statusSelect.disabled = Boolean(pending);
    record.statusSelect.setAttribute("aria-label", `Status for ${job.title || "this role"}`);
    const href = safeUrl(job.url);
    if (record.posting.dataset.href !== (href || "")) {
      record.posting.replaceChildren(externalLink("Official posting ↗", job.url, "posting-link") || element("span", "small muted", "Official link unavailable"));
      record.posting.dataset.href = href || "";
    }
    // Reuse editor nodes and never replace a dirty draft with a polling response.
    if (!record.draft.dirty && !pending) {
      record.draft.text = job.application_notes || "";
      record.draft.savedText = record.draft.text;
      if (record.notesInput.value !== record.draft.text) record.notesInput.value = record.draft.text;
    }
    record.notesInput.setAttribute("aria-label", `Application notes for ${job.title || "this role"}`);
    renderNotes(record);
  }

  function renderJobs(jobs) {
    const list = $("job-list");
    const active = document.activeElement;
    const focusedInside = list.contains(active);
    const selection = active && active.tagName === "TEXTAREA"
      ? { start: active.selectionStart, end: active.selectionEnd, scroll: active.scrollTop } : null;
    const ids = new Set(jobs.map((job) => String(job.id)));
    let cursor = list.firstElementChild;
    for (const job of jobs) {
      let record = jobCards.get(job.id);
      if (!record) {
        record = createJobCard(job);
        jobCards.set(job.id, record);
      }
      updateJobCard(record, job);
      if (record.card === cursor) cursor = cursor.nextElementSibling;
      else list.insertBefore(record.card, cursor);
    }
    for (const card of [...list.children]) {
      if (!ids.has(card.dataset.jobId)) card.remove();
    }
    for (const [id, record] of jobCards) {
      if (!ids.has(String(id)) && !record.draft.dirty && !pendingJobs.has(id)) jobCards.delete(id);
    }
    if (focusedInside && active.isConnected) {
      if (document.activeElement !== active) active.focus({ preventScroll: true });
      if (selection) {
        active.setSelectionRange(selection.start, selection.end);
        active.scrollTop = selection.scroll;
      }
    } else if (focusedInside) {
      $("results-title").focus({ preventScroll: true });
    }
  }

  function renderPagination() {
    $("pagination").hidden = total <= CONFIG.pageSize;
    $("previous-page").disabled = jobsBusy || offset === 0;
    $("next-page").disabled = jobsBusy || offset + CONFIG.pageSize >= total;
    setText($("page-label"), `Page ${Math.floor(offset / CONFIG.pageSize) + 1} of ${Math.max(1, Math.ceil(total / CONFIG.pageSize))}`);
  }

  async function loadJobs({ clear = false } = {}) {
    clearTimeout(searchTimer);
    invalidateJobs();
    const request = jobsRequest;
    const controller = new AbortController();
    jobsController = controller;
    jobsBusy = true;
    $("job-list").setAttribute("aria-busy", "true");
    $("results-loading").hidden = false;
    $("results-empty").hidden = true;
    if (clear) $("job-list").replaceChildren();
    setText($("results-summary"), "Updating your job board…");
    renderPagination();
    const params = new URLSearchParams({ view, q: query, offset: String(offset), limit: String(CONFIG.pageSize) });
    try {
      const result = await api(`/api/jobs?${params}`, { signal: controller.signal });
      if (request !== jobsRequest) return;
      if (!result || !Array.isArray(result.jobs) || !Number.isInteger(result.total) || result.total < 0) {
        throw new Error("The server returned an incomplete job list.");
      }
      total = result.total;
      if (offset > 0 && offset >= total) {
        offset = Math.floor(Math.max(0, total - 1) / CONFIG.pageSize) * CONFIG.pageSize;
        await loadJobs();
        return;
      }
      renderJobs(result.jobs);
      jobsLoaded = true;
      setText($("results-total"), `${numberFormat.format(total)} ${VIEWS[view]}`);
      setText($("results-summary"), total
        ? `Showing ${numberFormat.format(offset + 1)}–${numberFormat.format(Math.min(offset + result.jobs.length, total))} of ${numberFormat.format(total)} ${VIEWS[view]}${query ? ` for “${query}”` : ""}`
        : `0 ${VIEWS[view]}${query ? ` for “${query}”` : ""}`);
      if (!result.jobs.length) renderEmpty();
    } catch (error) {
      if (controller.signal.aborted) return;
      showError("Unable to load jobs", error);
      setText($("results-summary"), "Jobs could not be refreshed. Any visible cards may be out of date.");
      if (!$("job-list").children.length) {
        setText($("empty-title"), "Your job board couldn’t load");
        setText($("empty-description"), "Check that the local app is running and retry. Your saved job statuses have not been changed.");
        emptyAction = "retry";
        setText($("empty-action"), "Retry loading jobs");
        $("empty-action").disabled = false;
        $("empty-action").hidden = false;
        $("results-empty").hidden = false;
        setText($("results-total"), "Unavailable");
      }
    } finally {
      if (request === jobsRequest) {
        jobsBusy = false;
        $("job-list").setAttribute("aria-busy", "false");
        $("results-loading").hidden = true;
        renderPagination();
      }
    }
  }

  function updateExportLink() {
    const params = new URLSearchParams({ view, q: query });
    const link = $("export-csv");
    link.href = `/api/export?${params}`;
    link.download = `job-scout-${view === "applied" ? "applications" : view}.csv`;
    link.title = `Export all ${VIEWS[view]}${query ? ` matching “${query}”` : ""} as CSV, including saved notes.`;
  }

  async function exportJobs(event) {
    event.preventDefault();
    if (exporting) return;
    exporting = true;
    const link = $("export-csv");
    const path = link.getAttribute("href");
    const filename = link.download;
    link.setAttribute("aria-disabled", "true");
    setText(link, "Exporting…");
    try {
      const csv = await api(path, { responseType: "blob" });
      const url = URL.createObjectURL(csv);
      try {
        const download = element("a");
        download.href = url;
        download.download = filename;
        document.body.append(download);
        download.click();
        download.remove();
      } finally {
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
      announce("CSV download ready: all matching rows in the selected view, including saved notes.");
    } catch (error) {
      showError("Unable to export jobs", error);
    } finally {
      exporting = false;
      link.removeAttribute("aria-disabled");
      setText(link, "Export CSV");
    }
  }

  function selectView(nextView) {
    if (!Object.hasOwn(VIEWS, nextView)) return;
    view = nextView;
    offset = 0;
    updateExportLink();
    for (const button of $("view-controls").querySelectorAll("[data-view]")) {
      const active = button.dataset.view === view;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    }
    $("applications-help").hidden = view !== "applied";
    loadJobs({ clear: true });
  }

  function validationError(input, message, errorId) {
    input.setCustomValidity(message);
    input.setAttribute("aria-invalid", "true");
    setText($(errorId), message);
    $(errorId).hidden = false;
    input.reportValidity();
    input.focus();
    return null;
  }

  function readPreferences() {
    const values = {};
    for (const [id, key, maximum, label] of [
      ["keywords", "keywords", 5, "keyword phrases"],
      ["locations", "locations", 3, "locations"],
      ["exclude-keywords", "exclude_keywords", 10, "excluded title phrases"]
    ]) {
      values[key] = $(id).value.split(",").map((value) => value.trim()).filter(Boolean);
      if (values[key].length > maximum) return validationError($(id), `Use at most ${maximum} ${label}, separated by commas.`, "preferences-error");
    }
    const interval = Number($("scan-interval").value);
    if (!Number.isInteger(interval) || (interval !== 0 && (interval < 30 || interval > 1440))) {
      return validationError($("scan-interval"), "Use 0 for manual scans or a whole number from 30 to 1440 minutes.", "preferences-error");
    }
    const pages = Number($("max-pages").value);
    if (!Number.isInteger(pages) || pages < 1 || pages > 10) {
      return validationError($("max-pages"), "Use a whole number from 1 to 10 pages.", "preferences-error");
    }
    return { ...values, remote_only: $("remote-only").checked, scan_interval_minutes: interval, max_pages: pages };
  }

  async function savePreferences(event) {
    event.preventDefault();
    if (preferencesSaving || !preferencesInitialized) return;
    const preferences = readPreferences();
    if (!preferences) return;
    preferencesSaving = true;
    $("preferences-fields").disabled = true;
    setText($("save-preferences"), "Saving…");
    $("preferences-error").hidden = true;
    invalidateState();
    invalidateJobs();
    try {
      const saved = await api("/api/preferences", { method: "PUT", data: preferences });
      fillPreferences(saved);
      preferencesDirty = false;
      setText($("preferences-save-state"), "All changes saved.");
      announce("Preferences saved. Refreshing matching jobs.");
      offset = 0;
    } catch (error) {
      setText($("preferences-save-state"), "Changes were not saved. Your edits are still here.");
      setText($("preferences-error"), error.message);
      $("preferences-error").hidden = false;
      showError("Unable to save preferences", error);
    } finally {
      preferencesSaving = false;
      $("preferences-fields").disabled = false;
      setText($("save-preferences"), "Save preferences");
      await refreshState({ refreshResults: true });
    }
  }

  function resetSourceForm() {
    editingSourceId = null;
    sourceDirty = false;
    $("source-form").reset();
    for (const field of $("source-form").querySelectorAll("input, select")) {
      field.setCustomValidity("");
      field.removeAttribute("aria-invalid");
    }
    $("source-error").hidden = true;
    setText($("source-form-title"), "Add a career source");
    setText($("save-source"), "Add source");
    $("cancel-source-edit").hidden = true;
  }

  function editSource(source) {
    if (sourceSaving || scanStarting || (state && state.scan.running)) return;
    if (sourceDirty && !window.confirm("Discard your unsaved source changes and edit this source?")) return;
    resetSourceForm();
    editingSourceId = source.id;
    $("source-name").value = source.name;
    $("source-url").value = source.url;
    $("source-kind").value = source.kind;
    setText($("source-form-title"), "Edit career source");
    setText($("save-source"), "Save source");
    $("cancel-source-edit").hidden = false;
    $("source-form-details").open = true;
    $("source-name").focus();
  }

  async function saveSource(event) {
    event.preventDefault();
    if (sourceSaving || scanStarting || (state && state.scan.running)) return;
    const name = $("source-name").value.trim();
    const url = $("source-url").value.trim();
    const id = editingSourceId;
    if (!name) return validationError($("source-name"), "Enter a company or source name.", "source-error");
    if (!safeUrl(url)) return validationError($("source-url"), "Enter a full HTTP or HTTPS URL without embedded credentials.", "source-error");
    sourceSaving = true;
    if (id !== null) pendingSources.add(id);
    $("source-fields").disabled = true;
    $("source-error").hidden = true;
    setText($("save-source"), "Saving…");
    invalidateState();
    if (state) renderSources();
    try {
      await api(id === null ? "/api/sources" : `/api/sources/${encodeURIComponent(id)}`, {
        method: id === null ? "POST" : "PATCH",
        data: { name, url, kind: $("source-kind").value }
      });
      resetSourceForm();
      announce(`${name} ${id === null ? "added" : "updated"}. Run a scan to discover its jobs.`);
    } catch (error) {
      setText($("source-error"), error.message);
      $("source-error").hidden = false;
      showError("Unable to save this source", error);
    } finally {
      sourceSaving = false;
      pendingSources.delete(id);
      renderScan();
      setText($("save-source"), editingSourceId === null ? "Add source" : "Save source");
      await refreshState({ refreshResults: id !== null });
      if (state) renderSources();
    }
  }

  async function changeSource(source, remove) {
    if (pendingSources.has(source.id) || scanStarting || (state && state.scan.running)) return;
    if (remove && !window.confirm(`Delete “${source.name}” and all of its jobs?\n\nSaved jobs, tracked applications, and application notes from this source will also be deleted. This cannot be undone.\n\nTo keep your history and stop future scans, choose Cancel and Disable this source instead.`)) return;
    pendingSources.add(source.id);
    invalidateState();
    if (remove) invalidateJobs();
    renderSources();
    try {
      await api(`/api/sources/${encodeURIComponent(source.id)}`, {
        method: remove ? "DELETE" : "PATCH",
        data: remove ? {} : { enabled: !source.enabled }
      });
      if (remove) {
        if (editingSourceId === source.id) resetSourceForm();
        for (const [id, record] of jobCards) {
          if (record.job.source_id === source.id) jobCards.delete(id);
        }
      }
      announce(remove ? `${source.name} and its jobs were deleted.` : `${source.name} ${source.enabled ? "disabled" : "enabled"}.`);
    } catch (error) {
      showError(`Unable to ${remove ? "delete" : "update"} ${source.name}`, error);
    } finally {
      pendingSources.delete(source.id);
      await refreshState({ refreshResults: remove });
      if (state) renderSources();
    }
  }

  async function changeJob(record, patch) {
    const id = record.job.id;
    if (pendingJobs.has(id)) return;
    if (patch.status && patch.status === record.job.status) return;
    if (Object.hasOwn(patch, "application_notes") && !record.draft.dirty) return;
    pendingJobs.set(id, patch);
    record.draft.error = "";
    invalidateState();
    invalidateJobs();
    updateJobCard(record, record.job);
    try {
      await api(`/api/jobs/${encodeURIComponent(id)}`, { method: "PATCH", data: patch });
      if (Object.hasOwn(patch, "application_notes")) {
        record.draft.savedText = patch.application_notes;
        record.draft.dirty = record.draft.text !== record.draft.savedText;
        record.job.application_notes = patch.application_notes;
        announce(`Notes saved for “${record.job.title || "this job"}”.${record.draft.dirty ? " You have newer unsaved edits." : ""}`);
      } else {
        record.job.status = patch.status;
        announce(`“${record.job.title || "Job"}” marked ${STATUSES[patch.status].toLowerCase()}.`);
      }
    } catch (error) {
      if (Object.hasOwn(patch, "application_notes")) record.draft.error = `Not saved: ${error.message}`;
      showError("Unable to update this job", error);
    } finally {
      pendingJobs.delete(id);
      updateJobCard(record, record.job);
      await refreshState({ refreshResults: true });
    }
  }

  async function startScan() {
    if (scanStarting || !state || state.scan.running || !state.sources.some((source) => source.enabled)) return;
    scanStarting = true;
    invalidateState();
    renderScan();
    try {
      await api("/api/scan", { method: "POST", data: {} });
      state.scan.running = true;
      announce("Scan started. Results will refresh when it finishes.");
    } catch (error) {
      showError("Unable to start a scan", error);
    } finally {
      await refreshState();
      scanStarting = false;
      renderScan();
    }
  }

  $("scan-button").addEventListener("click", startScan);
  $("export-csv").addEventListener("click", exportJobs);
  $("preferences-form").addEventListener("submit", savePreferences);
  $("source-form").addEventListener("submit", saveSource);
  $("cancel-source-edit").addEventListener("click", () => { resetSourceForm(); $("source-name").focus(); });
  $("dismiss-error").addEventListener("click", () => { $("error-banner").hidden = true; });
  $("retry-state").addEventListener("click", () => refreshState({ refreshResults: true }));
  $("view-controls").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-view]");
    if (button && button.dataset.view !== view) selectView(button.dataset.view);
  });
  $("job-search").addEventListener("input", () => {
    query = $("job-search").value.trim();
    offset = 0;
    updateExportLink();
    clearTimeout(searchTimer);
    invalidateJobs();
    searchTimer = setTimeout(() => loadJobs({ clear: true }), CONFIG.searchDelayMs);
  });
  function changePage(direction) {
    if (jobsBusy || (direction < 0 && offset === 0) || (direction > 0 && offset + CONFIG.pageSize >= total)) return;
    offset = Math.max(0, offset + direction * CONFIG.pageSize);
    loadJobs({ clear: true });
    $("results").focus({ preventScroll: true });
    $("results").scrollIntoView({ block: "start" });
  }
  $("previous-page").addEventListener("click", () => changePage(-1));
  $("next-page").addEventListener("click", () => changePage(1));
  $("empty-action").addEventListener("click", () => {
    if (emptyAction === "scan") startScan();
    else if (emptyAction === "clear") {
      $("job-search").value = "";
      query = "";
      offset = 0;
      updateExportLink();
      loadJobs({ clear: true });
      $("job-search").focus();
    } else if (emptyAction === "all") selectView("all");
    else if (emptyAction === "retry") loadJobs({ clear: true });
  });
  for (const [formId, errorId] of [["preferences-form", "preferences-error"], ["source-form", "source-error"]]) {
    $(formId).addEventListener("input", (event) => {
      if (typeof event.target.setCustomValidity === "function") {
        event.target.setCustomValidity("");
        event.target.removeAttribute("aria-invalid");
      }
      $(errorId).hidden = true;
      if (formId === "preferences-form" && preferencesInitialized) {
        preferencesDirty = true;
        setText($("preferences-save-state"), "Unsaved changes");
      } else if (formId === "source-form") sourceDirty = true;
    });
  }
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshState();
  });
  window.addEventListener("beforeunload", (event) => {
    if (preferencesDirty || sourceDirty || [...jobCards.values()].some((record) => record.draft.dirty)) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  window.addEventListener("pagehide", () => {
    clearTimeout(searchTimer);
    invalidateState();
    invalidateJobs();
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) refreshState({ refreshResults: true });
  });
  updateExportLink();
  refreshState();
})();
