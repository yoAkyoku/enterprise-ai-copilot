(() => {
  "use strict";

  let apiToken = "";
  let dashboard = null;
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    })[character]);
  }

  async function request(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (apiToken) headers.set("Authorization", `Bearer ${apiToken}`);
    const response = await fetch(path, { ...options, headers });
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try { detail = (await response.json()).detail || detail; } catch (_) { /* response was not JSON */ }
      throw new Error(detail);
    }
    return response.status === 204 ? null : response.json();
  }

  async function requestRaw(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (apiToken) headers.set("Authorization", `Bearer ${apiToken}`);
    const response = await fetch(path, { ...options, headers });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    return response;
  }

  function toast(message, isError = false) {
    const element = $("#toast");
    element.textContent = message;
    element.style.background = isError ? "#a84d55" : "#111933";
    element.classList.add("show");
    window.setTimeout(() => element.classList.remove("show"), 3200);
  }

  function renderAgents(agents) {
    const copy = {
      "customer-service-agent": ["Verify customer questions with grounded ERP evidence.", ["order status", "handoff"]],
    };
    const html = agents.map((agent) => {
      const [description, tags] = copy[agent.id] || ["Bounded workflow ready for supervised execution.", ["policy checked"]];
      return `<article class="agent-card"><div class="agent-top"><span class="agent-orb">✦</span><div><h4>${escapeHtml(agent.id.replaceAll("-", " "))}</h4><small>${escapeHtml(agent.status)} · ${escapeHtml(agent.tool_count || 0)} tool</small></div><span class="ready-label">READY</span></div><p class="agent-description">${escapeHtml(description)}</p><div class="agent-tags">${tags.map((tag) => `<span class="agent-tag">${escapeHtml(tag)}</span>`).join("")}</div></article>`;
    }).join("");
    $("#agent-grid").innerHTML = html || `<div class="loading-card">No agents are currently registered.</div>`;
    $("#agent-directory").innerHTML = html || `<div class="loading-card">No agents are currently registered.</div>`;
    $("#agent-nav-count").textContent = agents.length;
  }

  function renderActivity(recent) {
    const labels = { "run.created": "Run created", "tool.completed": "MCP evidence returned", "run.succeeded": "Run verified", "attachment.created": "Image evidence stored", "attachment.deleted": "Image evidence removed" };
    const icons = { "run.created": "＋", "tool.completed": "⌘", "run.succeeded": "✓", "attachment.created": "▧", "attachment.deleted": "×" };
    const items = (recent || []).slice().reverse().map((event) => `<div class="activity-item"><span class="activity-icon">${escapeHtml(icons[event] || "•")}</span><div class="activity-body"><strong>${escapeHtml(labels[event] || event)}</strong><small>Policy and scope checks recorded</small></div><span class="activity-time">just now</span></div>`).join("");
    $("#activity-list").innerHTML = items || `<div class="empty-state">No events received yet. Launch a read-only run to see its trace.</div>`;
  }

  function renderAttachments(items) {
    $("#attachment-count").textContent = `${items.length} file${items.length === 1 ? "" : "s"}`;
    if (!items.length) {
      $("#attachment-grid").innerHTML = `<div class="empty-state">No image evidence in this workspace.</div>`;
      return;
    }
    $("#attachment-grid").innerHTML = items.map((item) => `<article class="attachment-tile"><img class="attachment-preview" data-attachment-id="${escapeHtml(item.id)}" alt="${escapeHtml(item.filename)}" /><div class="attachment-meta"><strong title="${escapeHtml(item.filename)}">${escapeHtml(item.filename)}</strong><small>${escapeHtml(item.width)} × ${escapeHtml(item.height)} · ${Math.ceil(item.size_bytes / 1024)} KB</small><div class="attachment-actions"><small>${escapeHtml(item.image_format)}</small><button class="delete-link" data-delete-id="${escapeHtml(item.id)}" type="button">Delete</button></div><div class="analysis-actions"><button class="analysis-link" data-analyze-id="${escapeHtml(item.id)}" data-task="describe" type="button">Describe</button><button class="analysis-link" data-analyze-id="${escapeHtml(item.id)}" data-task="ocr" type="button">OCR</button></div><div class="analysis-result" data-analysis-result="${escapeHtml(item.id)}"></div></div></article>`).join("");
    $$("img[data-attachment-id]").forEach(async (image) => {
      try {
        const response = await requestRaw(`/api/v1/attachments/${encodeURIComponent(image.dataset.attachmentId)}/content`);
        const blob = await response.blob();
        image.src = URL.createObjectURL(blob);
      } catch (error) { image.alt = "Image unavailable"; }
    });
    $$('[data-delete-id]').forEach((button) => button.addEventListener("click", async () => {
      const filename = button.closest(".attachment-tile")?.querySelector(".attachment-meta strong")?.textContent || "this image";
      if (!window.confirm(`Delete ${filename}? This removes the stored evidence from the current workspace.`)) return;
      try { await request(`/api/v1/attachments/${encodeURIComponent(button.dataset.deleteId)}`, { method: "DELETE" }); toast("Image evidence removed"); await loadDashboard(); }
      catch (error) { toast(error.message, true); }
    }));
    $$('[data-analyze-id]').forEach((button) => button.addEventListener("click", async () => {
      if (!window.confirm("Send this image to the configured Vision/OCR provider?")) return;
      const result = $(`[data-analysis-result="${button.dataset.analyzeId}"]`);
      result.textContent = "Analyzing…";
      try {
        const analysis = await request(`/api/v1/attachments/${encodeURIComponent(button.dataset.analyzeId)}/analyze`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task: button.dataset.task, allow_external_processing: true }) });
        result.textContent = `${analysis.task.toUpperCase()} · ${analysis.provider}: ${analysis.text}`;
      } catch (error) { result.textContent = error.message; toast(error.message, true); }
    }));
  }

  function renderApprovals(items) {
    const count = items.length;
    $("#approval-nav-count").textContent = count;
    $("#approval-page-count").textContent = count;
    if (!count) {
      $("#approval-list").innerHTML = `<div class="empty-state">No actions are waiting for review.</div>`;
      return;
    }
    $("#approval-list").innerHTML = items.map((item) => `<article class="approval-card"><div class="approval-card-head"><div><p class="eyebrow">${escapeHtml(item.risk)} · ${escapeHtml(item.status)}</p><h4>${escapeHtml(item.tool_name)}</h4></div><span class="approval-hash">${escapeHtml(item.arguments_hash.slice(0, 12))}…</span></div><pre>${escapeHtml(JSON.stringify(item.arguments, null, 2))}</pre><small>Expires ${escapeHtml(item.expires_at)} · requester scope is enforced server-side</small><div class="approval-actions"><button class="button button-primary" type="button" data-approve-id="${escapeHtml(item.id)}">Approve</button><button class="button button-ghost" type="button" data-reject-id="${escapeHtml(item.id)}">Reject</button></div><div class="approval-result" data-approval-result="${escapeHtml(item.id)}"></div></article>`).join("");
    $$('[data-approve-id]').forEach((button) => button.addEventListener("click", async () => {
      const result = $(`[data-approval-result="${button.dataset.approveId}"]`);
      try {
        const approved = await request(`/api/v1/approvals/${encodeURIComponent(button.dataset.approveId)}/approve`, { method: "POST" });
        result.textContent = `Approved. One-time token: ${approved.approval_token}`;
        toast("Approval recorded; token issued once");
        await loadDashboard();
      } catch (error) { result.textContent = error.message; toast(error.message, true); }
    }));
    $$('[data-reject-id]').forEach((button) => button.addEventListener("click", async () => {
      const reason = window.prompt("Reason for rejecting this action?", "Not approved by operator");
      if (reason === null) return;
      const result = $(`[data-approval-result="${button.dataset.rejectId}"]`);
      try {
        await request(`/api/v1/approvals/${encodeURIComponent(button.dataset.rejectId)}/reject`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason }) });
        result.textContent = "Rejected and recorded in the audit trail.";
        toast("Approval rejected");
        await loadDashboard();
      } catch (error) { result.textContent = error.message; toast(error.message, true); }
    }));
  }

  function renderDashboard(payload) {
    dashboard = payload;
    $("#workspace-name").textContent = payload.workspace_id || "workspace";
    $("#connection-label").textContent = "API CONNECTED";
    $("#hero-date").textContent = new Intl.DateTimeFormat("en-US", { weekday: "long", day: "numeric", month: "long", year: "numeric" }).format(new Date()).toUpperCase();
    $("#metric-agents").textContent = payload.agents.length;
    $("#metric-mcp").textContent = payload.mcp.filter((service) => service.status === "healthy").length;
    $("#metric-approvals").textContent = payload.approvals.pending;
    $("#approval-nav-count").textContent = payload.approvals.pending;
    $("#metric-audit").textContent = payload.audit.event_count;
    renderAgents(payload.agents);
    renderActivity(payload.audit.recent);
    renderAttachments(payload.attachments.items);
    $("#token-banner").style.display = "none";
  }

  async function loadApprovals() {
    try { renderApprovals((await request("/api/v1/approvals?approval_status=pending")).items || []); }
    catch (_) { $("#approval-list").innerHTML = `<div class="empty-state">Approval service is unavailable for this deployment.</div>`; }
  }

  async function loadDashboard() {
    try { renderDashboard(await request("/api/v1/dashboard")); await loadApprovals(); }
    catch (error) {
      $("#connection-label").textContent = "AUTH REQUIRED";
      $("#token-banner").style.display = "flex";
      if (apiToken) toast(error.message, true);
    }
  }

  async function upload(file) {
    const form = new FormData();
    form.append("image", file);
    try { await request("/api/v1/attachments", { method: "POST", body: form }); toast("Image validated and stored in this tenant"); await loadDashboard(); }
    catch (error) { toast(error.message, true); }
  }

  function wireNavigation() {
    $$(".nav-item").forEach((item) => item.addEventListener("click", () => {
      $$(".nav-item").forEach((nav) => nav.classList.remove("active")); item.classList.add("active");
      const view = item.dataset.view;
      $("#page-title").textContent = item.textContent.trim();
      $("#overview-view").classList.toggle("hidden-view", view !== "overview");
      $("#chat-view").classList.toggle("hidden-view", view !== "agents" && view !== "overview" && view !== "approvals");
      $("#agents-view").classList.toggle("hidden-view", view !== "agents");
      $("#approvals-view").classList.toggle("hidden-view", view !== "approvals");
      if (view === "overview") { $("#chat-view").classList.add("hidden-view"); }
      if (view === "agents") { $("#overview-view").classList.add("hidden-view"); $("#chat-view").classList.add("hidden-view"); }
      if (view === "approvals") { $("#overview-view").classList.add("hidden-view"); $("#chat-view").classList.add("hidden-view"); $("#approvals-view").classList.remove("hidden-view"); loadApprovals(); }
      if (["audit", "mcp", "knowledge", "plugins"].includes(view)) { $("#overview-view").classList.remove("hidden-view"); toast(`${item.textContent.trim()} view is represented in the live overview`, false); }
    }));
    $("#open-chat").addEventListener("click", () => { $("#query-input").focus(); $("#overview-view").classList.add("hidden-view"); $("#chat-view").classList.remove("hidden-view"); $("#page-title").textContent = "New run"; });
    $("#view-agents").addEventListener("click", () => $("[data-view=agents]").click());
    $("#open-attachments").addEventListener("click", () => $("#evidence-panel").scrollIntoView({ behavior: "smooth" }));
    $$(".quick-action[data-query]").forEach((button) => button.addEventListener("click", () => { $("#query-input").value = button.dataset.query; $("#order-input").value = button.dataset.order || ""; $("#open-chat").click(); }));
  }

  function wireAttachments() {
    const input = $("#image-input"); const zone = $("#drop-zone");
    $("#choose-image").addEventListener("click", () => input.click());
    input.addEventListener("change", () => { if (input.files[0]) upload(input.files[0]); input.value = ""; });
    ["dragenter", "dragover"].forEach((type) => zone.addEventListener(type, (event) => { event.preventDefault(); zone.classList.add("dragover"); }));
    ["dragleave", "drop"].forEach((type) => zone.addEventListener(type, (event) => { event.preventDefault(); zone.classList.remove("dragover"); }));
    zone.addEventListener("drop", (event) => { const file = event.dataTransfer.files[0]; if (file) upload(file); });
  }

  function wireRunForm() {
    $("#run-form").addEventListener("submit", async (event) => {
      event.preventDefault(); $("#run-status").textContent = "Running policy and evidence checks…";
      try {
        const result = await request("/api/v1/runs", { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": `web-${Date.now()}` }, body: JSON.stringify({ query: $("#query-input").value, order_id: $("#order-input").value || null }) });
        $("#run-result").innerHTML = `<div class="result-success"><strong>${escapeHtml(result.status)}</strong><br />${escapeHtml(result.message)}<br /><small>Trace ${escapeHtml(result.trace_id)} · Source ${escapeHtml(result.source_id || "n/a")}</small></div>`;
        $("#run-status").textContent = "Run complete · evidence recorded"; await loadDashboard();
      } catch (error) { $("#run-status").textContent = error.message; $("#run-result").innerHTML = `<div class="result-placeholder">The run did not complete. The failure was not reported as success.</div>`; }
    });
  }

  $("#connect-button").addEventListener("click", async () => { apiToken = $("#token-input").value.trim(); if (!apiToken) return toast("Enter an API token first", true); await loadDashboard(); });
  $("#refresh-button").addEventListener("click", async () => { await loadDashboard(); toast("Runtime signal refreshed"); });
  wireNavigation(); wireAttachments(); wireRunForm(); loadDashboard();
})();
