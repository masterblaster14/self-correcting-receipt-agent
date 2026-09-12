/* Receipt Agent frontend — vanilla JS, no build step. */
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const SYM = { INR: "₹", USD: "$", EUR: "€", GBP: "£" };
  const money = (x, cur = "INR") => x == null ? "—" : `${SYM[cur] ?? cur + " "}${Number(x).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  const STAGES = ["upload", "preprocess", "extract", "verify", "correct", "categorize", "policy", "report"];
  const STAGE_ALIAS = { store: "report", done: "report" };

  let current = null;        // last ProcessResult
  let imgMode = "orig";

  // ---------------------------------------------------------------- tabs
  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
  function showView(v) {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
    document.querySelectorAll(".view").forEach((s) => (s.hidden = s.id !== `view-${v}`));
    if (v === "ledger") loadLedger();
    if (v === "policy") loadPolicy();
    window.scrollTo({ top: 0 });
  }

  // ---------------------------------------------------------------- health + samples
  fetch("/api/health").then((r) => r.json()).then((h) => {
    const chip = $("modelChip");
    chip.textContent = h.mock ? "MOCK MODE · no API calls" : `${h.model} · effort ${h.effort}`;
    chip.classList.toggle("mock", h.mock);
    if (!h.mock && !h.api_key_set) toast("ANTHROPIC_API_KEY is not set — extraction will fail", 6000);
  }).catch(() => {});

  fetch("/api/samples").then((r) => r.json()).then(({ items }) => {
    const grid = $("sampleGrid");
    if (!items.length) { $("samples").hidden = true; return; }
    grid.innerHTML = items.map((s) => `
      <button class="sample ${s.name.endsWith("_bad") ? "bad" : ""}" data-url="${esc(s.url)}" data-name="${esc(s.name)}">
        <img src="${esc(s.url)}" alt="${esc(s.name)}" loading="lazy"><span>${esc(s.name.replace(/_bad$/, "").replace(/_/g, " "))}</span>
      </button>`).join("");
    grid.querySelectorAll(".sample").forEach((b) => b.addEventListener("click", async () => {
      const blob = await (await fetch(b.dataset.url)).blob();
      startJob(new File([blob], b.dataset.name + ".jpg", { type: "image/jpeg" }));
    }));
  }).catch(() => { $("samples").hidden = true; });

  // ---------------------------------------------------------------- camera
  // Phones: the native file input with capture=environment opens the camera app (works over http).
  // Desktops: that same input is just a file picker, so use getUserMedia in a modal instead.
  const isMobile = /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent) || (navigator.maxTouchPoints > 1 && /Mac/.test(navigator.userAgent));
  let camStream = null;
  async function openWebcam() {
    if (!navigator.mediaDevices?.getUserMedia || !window.isSecureContext) return false;
    try {
      camStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment", width: { ideal: 1920 }, height: { ideal: 1080 } }, audio: false });
    } catch (e) { return false; }
    $("camVideo").srcObject = camStream;
    $("camModal").hidden = false;
    return true;
  }
  function closeWebcam() {
    $("camModal").hidden = true;
    camStream?.getTracks().forEach((t) => t.stop());
    camStream = null;
  }
  function shootWebcam() {
    const v = $("camVideo");
    if (!v.videoWidth) return;
    const c = document.createElement("canvas");
    c.width = v.videoWidth; c.height = v.videoHeight;
    c.getContext("2d").drawImage(v, 0, 0);
    c.toBlob((blob) => { closeWebcam(); startJob(new File([blob], `webcam-${Date.now()}.jpg`, { type: "image/jpeg" })); }, "image/jpeg", 0.92);
  }
  $("camShoot").addEventListener("click", shootWebcam);
  $("camCancel").addEventListener("click", closeWebcam);
  document.addEventListener("keydown", (e) => {
    if ($("camModal").hidden) return;
    if (e.key === "Escape") closeWebcam();
    if (e.key === " ") { e.preventDefault(); shootWebcam(); }
  });

  // ---------------------------------------------------------------- upload wiring
  $("btnCamera").addEventListener("click", async () => {
    if (isMobile) return $("fileCamera").click();
    if (await openWebcam()) return;
    toast("No webcam available here — choose an image file instead", 3500);
    $("fileUpload").click();
  });
  $("btnUpload").addEventListener("click", () => $("fileUpload").click());
  ["fileCamera", "fileUpload"].forEach((id) => $(id).addEventListener("change", (e) => {
    const f = e.target.files?.[0];
    if (f) startJob(f);
    e.target.value = "";
  }));
  const dz = $("dropzone");
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => { const f = e.dataTransfer.files?.[0]; if (f) startJob(f); });
  document.addEventListener("paste", (e) => {
    const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith("image/"));
    if (item && !$("uploadPanel").hidden) startJob(item.getAsFile());
  });
  $("btnAgain").addEventListener("click", resetToUpload);
  $("btnCancel").addEventListener("click", resetToUpload);

  function resetToUpload() {
    $("resultPanel").hidden = true; $("processingPanel").hidden = true; $("uploadPanel").hidden = false;
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // ---------------------------------------------------------------- job lifecycle
  async function startJob(file) {
    $("uploadPanel").hidden = true; $("resultPanel").hidden = true; $("processingPanel").hidden = false;
    $("procError").hidden = true; $("btnCancel").hidden = true; $("procLog").innerHTML = "";
    $("procPreview").src = URL.createObjectURL(file);
    setStage("upload", []);

    const fd = new FormData();
    fd.append("file", file, file.name || "receipt.jpg");
    fd.append("self_correct", $("optSelfCorrect").checked);
    fd.append("max_iterations", $("optIters").value);
    fd.append("demo_fault", $("optFault").checked);
    fd.append("submitted_by", $("optPerson").value || "");
    fd.append("claimed_amount", $("optClaimAmount").value || "");
    fd.append("claimed_purpose", $("optClaimPurpose").value || "");

    let job;
    try {
      const r = await fetch("/api/process", { method: "POST", body: fd });
      if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
      job = (await r.json()).job_id;
    } catch (e) { return fail(e.message); }

    let seen = 0;
    const poll = async () => {
      let j;
      try { j = await (await fetch(`/api/jobs/${job}`)).json(); } catch { return setTimeout(poll, 800); }
      const log = $("procLog");
      for (; seen < j.events.length; seen++) {
        const ev = j.events[seen];
        log.insertAdjacentHTML("beforeend", `<div><span class="st">[${esc(ev.stage.padEnd(10))}]</span> ${esc(ev.message)}</div>`);
      }
      log.scrollTop = log.scrollHeight;
      setStage(j.stage, j.events.map((e) => e.stage));
      if (j.status === "running") return setTimeout(poll, 500);
      if (j.status === "error") return fail(j.error);
      render(j.result);
    };
    poll();
  }

  function fail(msg) {
    $("procError").hidden = false; $("procError").textContent = msg; $("btnCancel").hidden = false;
  }

  function setStage(stage, doneStages) {
    stage = STAGE_ALIAS[stage] || stage;
    const idx = STAGES.indexOf(stage);
    document.querySelectorAll("#stepper li").forEach((li) => {
      const i = STAGES.indexOf(li.dataset.stage);
      li.classList.toggle("done", i < idx || (stage === "report" && doneStages.includes("done")));
      li.classList.toggle("active", i === idx && !doneStages.includes("done"));
      // the correction stage is skipped when nothing fails - keep it neutral unless it actually ran
      if (li.dataset.stage === "correct" && i < idx && !doneStages.includes("correct")) { li.classList.remove("done"); li.style.opacity = .55; li.title = "No correction needed"; }
      else if (li.dataset.stage === "correct") { li.style.opacity = ""; li.title = ""; }
    });
  }

  // ---------------------------------------------------------------- render result
  function render(res) {
    current = res;
    const p = res.profile, cur = p.currency || "INR", verified = res.state === "VERIFIED";
    $("processingPanel").hidden = true; $("resultPanel").hidden = false;

    $("rStatus").textContent = verified ? "VERIFIED" : "FLAGGED FOR REVIEW";
    $("rStatus").classList.toggle("flag", !verified);
    $("rVendor").textContent = p.vendor_name || "Unknown vendor";
    $("rMeta").textContent = [p.date, p.time, p.vendor_address, res.submitted_by ? `submitted by ${res.submitted_by}${res.department ? " (" + res.department + ")" : ""}` : "", `#${res.id}`].filter(Boolean).join(" · ");
    $("rTotal").textContent = money(p.total, cur);
    $("rChips").innerHTML = [
      `<span class="chip accent">${esc(res.category)}</span>`,
      `<span class="chip ${res.iterations ? "warn" : "ok"}">${res.iterations ? `${res.iterations} correction pass${res.iterations > 1 ? "es" : ""}` : "first pass consistent"}</span>`,
      res.self_correction_enabled ? "" : `<span class="chip bad">baseline · no self-correction</span>`,
      `<span class="chip">${(res.total_ms / 1000).toFixed(1)}s</span>`,
    ].join("");
    $("btnPdf").href = `/api/receipts/${res.id}/pdf`;
    $("btnXlsx").href = `/api/receipts/${res.id}/xlsx`;

    // alerts
    const alerts = [];
    if (!verified) alerts.push(`<div class="alert warn"><b>Flagged for human review.</b> ${esc(res.verification.checks.filter((c) => !c.passed && c.severity === "error").map((c) => c.message).join(" "))} The data is kept but never silently accepted.</div>`);
    if (res.duplicate?.is_duplicate || res.duplicate?.similar_image) alerts.push(`<div class="alert bad"><b>${res.duplicate.is_duplicate ? "Possible duplicate submission." : "Similar receipt image already in the ledger."}</b> ${esc(res.duplicate.detail)}</div>`);
    if (res.claim?.status === "exceeds") alerts.push(`<div class="alert bad"><b>Claimed amount exceeds the receipt.</b> ${esc(res.claim.detail)}</div>`);
    else if (res.claim?.status === "ok") alerts.push(`<div class="alert info"><b>Claim checked.</b> ${esc(res.claim.detail)}${res.claim.claimed_purpose ? ` Purpose stated: "${esc(res.claim.claimed_purpose)}".` : ""}</div>`);
    if (res.policy?.status === "violation") alerts.push(`<div class="alert bad"><b>Policy violation.</b> ${esc(res.policy.reason)}</div>`);
    else if (res.policy?.status === "needs_justification") alerts.push(`<div class="alert warn"><b>Needs justification.</b> ${esc(res.policy.reason)}</div>`);
    if (res.trace.some((t) => t.demo_fault)) alerts.push(`<div class="alert info"><b>Demo mode.</b> A fault was deliberately injected into the first extraction so the correction loop is visible.</div>`);
    if (p.uncertain_fields?.length && !res.iterations) alerts.push(`<div class="alert info">Model flagged low confidence on: ${esc(p.uncertain_fields.join(", "))}.</div>`);
    $("rAlerts").innerHTML = alerts.join("");

    // image + regions
    setImg("orig");
    $("rPreInfo").innerHTML = [
      `<span class="chip">${res.preprocess_info.width}×${res.preprocess_info.height}</span>`,
      `<span class="chip">deskew ${res.preprocess_info.deskew_deg ?? 0}°</span>`,
      `<span class="chip">sharpness ${res.preprocess_info.sharpness}</span>`,
      `<span class="chip">brightness ${res.preprocess_info.mean_brightness}</span>`,
      ...(res.preprocess_info.steps || []).map((s) => `<span class="chip">${esc(s)}</span>`),
    ].join("");

    // checks
    $("rChecks").innerHTML = res.verification.checks.map((c) => {
      const cls = c.passed ? "pass" : c.severity === "error" ? "fail" : "warn";
      const mark = c.passed ? "✓" : c.severity === "error" ? "✗" : "!";
      return `<li class="${cls}"><div class="ck-mark">${mark}</div><div>
        <div class="ck-name">${esc(c.id)} · ${esc(c.name)}</div>
        <div class="ck-msg">${esc(c.message)}</div>
        ${!c.passed && c.hypothesis ? `<div class="ck-hyp">Hypothesis: ${esc(c.hypothesis)}</div>` : ""}
      </div></li>`;
    }).join("");

    // policy
    const ps = res.policy?.status || "not_checked";
    $("rPolicy").innerHTML = `
      <span class="policy-status ${ps}">${ps.replace("_", " ").toUpperCase()}${res.policy.rules_triggered?.length ? ` · rule ${esc(res.policy.rules_triggered.join(", "))}` : ""}</span>
      <div class="policy-reason">${esc(res.policy.reason || "")}</div>
      <div class="dup ${(res.duplicate?.is_duplicate || res.duplicate?.similar_image) ? "bad" : ""}">${(res.duplicate?.is_duplicate || res.duplicate?.similar_image) ? "⚠ " : "✓ "}${esc(res.duplicate?.detail || "")}</div>
      ${res.claim?.claimed_amount != null || res.claim?.claimed_purpose ? `<div class="dup ${res.claim.status === "exceeds" ? "bad" : ""}">${res.claim.status === "exceeds" ? "⚠ " : "✓ "}${esc(res.claim.detail)}${res.claim.claimed_purpose ? ` Stated purpose: "${esc(res.claim.claimed_purpose)}".` : ""}</div>` : ""}`;

    // profile
    $("rKv").innerHTML = [
      ["Vendor", p.vendor_name], ["Date", p.date], ["Invoice #", p.invoice_number], ["Payment", p.payment_method],
      ["Currency", cur], ["Pricing", p.tax_inclusive_prices == null ? "—" : p.tax_inclusive_prices ? "tax-inclusive" : "tax-exclusive"],
    ].map(([k, v]) => `<div><b>${k}</b>${esc(v || "—")}</div>`).join("");
    $("rItems").innerHTML = `<thead><tr><th>#</th><th>Item</th><th class="num">Qty</th><th class="num">Rate</th><th class="num">Amount</th></tr></thead><tbody>${
      p.line_items.length ? p.line_items.map((li, i) => `<tr><td>${i + 1}</td><td>${esc(li.description)}</td><td class="num">${li.quantity ?? ""}</td><td class="num">${li.unit_price != null ? money(li.unit_price, cur) : ""}</td><td class="num">${money(li.amount, cur)}</td></tr>`).join("")
        : `<tr><td colspan="5" class="empty">No itemised rows</td></tr>`}</tbody>`;
    const rows = [];
    if (p.subtotal != null) rows.push(["Subtotal", money(p.subtotal, cur)]);
    p.taxes.forEach((t) => rows.push([`${t.label}${t.rate_percent != null ? ` (${t.rate_percent}%)` : ""}`, money(t.amount, cur)]));
    p.other_charges.forEach((c) => rows.push([c.label, money(c.amount, cur)]));
    if (p.discount) rows.push(["Discount", "− " + money(p.discount, cur)]);
    if (p.round_off) rows.push(["Round off", (p.round_off > 0 ? "+" : "") + p.round_off.toFixed(2)]);
    $("rTotals").innerHTML = rows.map(([k, v]) => `<div><span>${esc(k)}</span><span>${v}</span></div>`).join("") + `<div class="grand"><span>Total</span><span>${money(p.total, cur)}</span></div>`;
    $("rNotes").textContent = p.notes ? `Model notes: ${p.notes}` : "";

    // trace
    $("rTraceSub").textContent = res.self_correction_enabled
      ? (res.iterations ? `${res.iterations} targeted re-examination${res.iterations === 1 ? "" : "s"} (limit ${res.max_iterations ?? $("optIters").value}) · each pass: rank failures → hypothesise → zoom → re-read → re-verify`
                        : "First extraction passed every constraint — no re-examination needed.")
      : "Self-correction disabled — single-pass baseline for comparison.";
    $("rTrace").innerHTML = res.trace.map((t) => {
      const cls = { INITIAL_EXTRACTION: "extract", AWAITING_VERIFICATION: "verify", FLAGGED_FOR_REEXAMINATION: "correct", VERIFIED: "verified", FLAGGED_FOR_REVIEW: "flagged" }[t.state];
      let extra = "";
      if (t.state === "FLAGGED_FOR_REEXAMINATION") {
        extra += `<div class="tl-box"><h5>Failing constraints</h5><ul>${t.failed_checks.map((f) => `<li>${esc(f)}</li>`).join("")}</ul></div>`;
        if (t.crops_b64?.length) extra += `<div class="crops">${t.crops_b64.map((c, i) => `<button data-crop="${i}" title="Zoomed crop sent to the model"><img src="data:image/jpeg;base64,${c}" alt=""><small>zoom: ${esc(t.focus_regions[i] || "")}</small></button>`).join("")}</div>`;
        extra += t.changes.length
          ? `<div class="tl-box"><h5>Fields changed</h5><table class="changes">${t.changes.map((c) => `<tr><td>${esc(c.field)}</td><td><span class="before">${esc(c.before ?? "∅")}</span> → <span class="after">${esc(c.after ?? "∅")}</span></td></tr>`).join("")}</table></div>`
          : `<div class="tl-box">Model stood by its previous reading.</div>`;
        if (t.revision_notes) extra += `<div class="agent-note"><b>Agent:</b> ${esc(t.revision_notes)}</div>`;
        extra += `<details class="prompt"><summary>Show targeted prompt sent to the model</summary><pre>${esc(t.prompt)}</pre></details>`;
      } else if (t.state === "INITIAL_EXTRACTION" && t.revision_notes) {
        extra += `<div class="agent-note"><b>Model:</b> ${esc(t.revision_notes)}</div>`;
      }
      return `<li class="${cls}"><div class="tl-title">${esc(t.title)}${t.duration_ms ? `<span class="tl-time">${(t.duration_ms / 1000).toFixed(1)}s</span>` : ""}</div><div class="tl-detail">${esc(t.detail)}</div>${extra}</li>`;
    }).join("");
    $("rTrace").querySelectorAll("[data-crop]").forEach((b) => b.addEventListener("click", () => lightbox(b.querySelector("img").src, b.querySelector("small").textContent)));

    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // image segment control
  $("imgSeg").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => setImg(b.dataset.img)));
  function setImg(mode) {
    imgMode = mode;
    $("imgSeg").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.img === mode));
    const res = current; if (!res) return;
    $("rImage").src = `data:image/jpeg;base64,${mode === "bin" ? res.processed_image_b64 : res.original_image_b64}`;
    const ov = $("rOverlay");
    if (mode !== "regions") { ov.innerHTML = ""; return; }
    const focused = new Set(res.trace.flatMap((t) => t.focus_regions || []));
    ov.innerHTML = (res.profile.regions || []).map((r) => `
      <div class="region ${esc(r.field)} ${focused.has(r.field) ? "focus" : ""}" style="left:${r.x * 100}%;top:${r.y * 100}%;width:${r.w * 100}%;height:${r.h * 100}%">
        <span>${esc(r.field)}${focused.has(r.field) ? " · re-examined" : ""}</span></div>`).join("")
      || `<div class="region total" style="left:4%;top:4%;width:92%;height:8%"><span>no regions returned</span></div>`;
  }

  // ---------------------------------------------------------------- ledger
  $("btnRefresh").addEventListener("click", loadLedger);
  $("ledgerFilter").addEventListener("change", loadLedger);
  async function loadLedger() {
    const all = await (await fetch("/api/receipts")).json();
    const stats = all.stats;
    // filter by employee or department, client-side (ledger is small)
    const filt = $("ledgerFilter");
    const opts = new Map();
    all.items.forEach((r) => { if (r.department) opts.set(`d:${r.department}`, `Dept: ${r.department}`); if (r.submitted_by) opts.set(`p:${r.submitted_by}`, r.submitted_by); });
    const keep = filt.value;
    filt.innerHTML = `<option value="">Everyone</option>` + [...opts].map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("");
    filt.value = [...opts.keys()].includes(keep) ? keep : "";
    const items = all.items.filter((r) => !filt.value || (filt.value.startsWith("d:") ? r.department === filt.value.slice(2) : r.submitted_by === filt.value.slice(2)));
    const spend = (stats.by_currency || []).length
      ? `<ul class="cur-list">${stats.by_currency.map((c) => `<li><span class="cur-code">${esc(c.currency)}</span><span class="cur-amt">${money(c.total, c.currency)}</span><span class="cur-n">${c.n} receipt${c.n === 1 ? "" : "s"}</span></li>`).join("")}</ul>`
      : "<span>—</span>";
    $("stats").innerHTML = [
      ["Receipts", `<span>${stats.n}</span>`], ["Total spend by currency", spend],
      ["Verified", `<span>${stats.n ? `${Math.round(100 * stats.verified / stats.n)}%` : "—"}</span>`],
      ["Self-corrected", `<span>${stats.corrected || 0}</span>`],
    ].map(([k, v]) => `<div class="stat"><b>${k}</b>${v}</div>`).join("");
    const t = $("ledgerTable");
    if (!items.length) { t.innerHTML = `<tbody><tr><td class="empty">No receipts yet — scan one.</td></tr></tbody>`; return; }
    t.innerHTML = `<thead><tr><th>When</th><th>Vendor</th><th>Date</th><th class="num">Total</th><th>Category</th><th>Employee</th><th>Status</th><th class="num">Passes</th><th>Files</th><th></th></tr></thead><tbody>${
      items.map((r) => `<tr data-id="${r.id}">
        <td class="mono">${esc(r.created_at.replace("T", " ").slice(0, 16))}</td><td>${esc(r.vendor || "—")}</td><td class="mono">${esc(r.date || "—")}</td>
        <td class="num">${money(r.total, r.currency)}</td><td>${esc(r.category)}</td>
        <td>${esc(r.submitted_by || "—")}${r.department ? `<br><small style="color:var(--muted)">${esc(r.department)}</small>` : ""}</td>
        <td><span class="chip ${r.state === "VERIFIED" ? "ok" : "warn"}">${r.state === "VERIFIED" ? "verified" : "review"}</span>${r.self_correct ? "" : ' <span class="chip bad">baseline</span>'}</td>
        <td class="num">${r.iterations}</td>
        <td style="white-space:nowrap"><a href="/api/receipts/${r.id}/pdf" target="_blank" rel="noopener" class="btn small" onclick="event.stopPropagation()">PDF</a> <a href="/api/receipts/${r.id}/xlsx" download class="btn small" onclick="event.stopPropagation()">XLSX</a></td>
        <td><button class="del" title="Delete" data-del="${r.id}">✕</button></td></tr>`).join("")}</tbody>`;
    t.querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", async () => {
      const d = await (await fetch(`/api/receipts/${tr.dataset.id}`)).json();
      showView("scan"); $("uploadPanel").hidden = true; render(d.result);
    }));
    t.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm("Delete this receipt from the ledger?")) return;
      await fetch(`/api/receipts/${b.dataset.del}`, { method: "DELETE" }); loadLedger();
    }));
  }

  $("askForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = $("askInput").value.trim(); if (!q) return;
    $("askBtn").disabled = true; $("askAnswer").hidden = false; $("askAnswer").textContent = "Thinking…";
    try {
      const r = await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: q }) });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.statusText);
      $("askAnswer").innerHTML = esc(d.answer) + (d.queries?.length ? `<div class="q">Agent ran ${d.queries.length} quer${d.queries.length === 1 ? "y" : "ies"}:<br>${d.queries.map((x) => esc(x.sql) + (x.error ? ` — error: ${esc(x.error)}` : ` — ${x.rows} row(s)`)).join("<br>")}</div>` : "");
    } catch (err) { $("askAnswer").textContent = "Error: " + err.message; }
    $("askBtn").disabled = false;
  });

  // ---------------------------------------------------------------- organisation + policy
  let defaultPolicy = "";
  async function loadPolicy() {
    const d = await (await fetch("/api/policy")).json();
    $("policyText").value = d.policy; defaultPolicy = d.default; $("policyNote").textContent = "";
    await loadOrg();
  }
  async function loadOrg() {
    const o = await (await fetch("/api/org")).json();
    $("orgName").value = o.org_name || "";
    const t = $("peopleTable");
    t.innerHTML = o.people.length
      ? `<thead><tr><th>Name</th><th>Email</th><th>Department</th><th></th></tr></thead><tbody>${
          o.people.map((p) => `<tr><td>${esc(p.name)}</td><td>${esc(p.email || "—")}</td><td>${esc(p.department || "—")}</td><td><button class="del" data-pid="${p.id}" title="Remove">✕</button></td></tr>`).join("")}</tbody>`
      : `<tbody><tr><td class="empty">No people yet — add your team above.</td></tr></tbody>`;
    t.querySelectorAll("[data-pid]").forEach((b) => b.addEventListener("click", async () => { await fetch(`/api/people/${b.dataset.pid}`, { method: "DELETE" }); loadOrg(); }));
    fillPeopleSelect(o.people);
  }
  function fillPeopleSelect(people) {
    const sel = $("optPerson");
    let saved = "";
    try { saved = localStorage.getItem("submitting_as") || ""; } catch {}
    sel.innerHTML = `<option value="">— not set —</option>` + people.map((p) => `<option value="${esc(p.name)}">${esc(p.name)}${p.department ? ` · ${esc(p.department)}` : ""}</option>`).join("");
    sel.value = people.some((p) => p.name === saved) ? saved : "";
  }
  $("optPerson").addEventListener("change", () => { try { localStorage.setItem("submitting_as", $("optPerson").value); } catch {} });
  fetch("/api/org").then((r) => r.json()).then((o) => fillPeopleSelect(o.people)).catch(() => {});
  $("btnSaveOrg").addEventListener("click", async () => {
    const r = await fetch("/api/org", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ org_name: $("orgName").value }) });
    $("orgNote").textContent = r.ok ? "Saved." : "Save failed";
  });
  $("peopleForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = { name: $("pName").value, email: $("pEmail").value, department: $("pDept").value };
    const r = await fetch("/api/people", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (r.ok) { $("peopleForm").reset(); loadOrg(); } else toast("Could not add person");
  });
  $("btnSavePolicy").addEventListener("click", async () => {
    const r = await fetch("/api/policy", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ policy: $("policyText").value }) });
    $("policyNote").textContent = r.ok ? "Saved — applies to the next scan." : "Save failed";
  });
  $("btnResetPolicy").addEventListener("click", () => { $("policyText").value = defaultPolicy; });

  // ---------------------------------------------------------------- misc
  function lightbox(src, cap) { $("lightbox").hidden = false; $("lightbox").querySelector("img").src = src; $("lbCap").textContent = cap || ""; }
  $("lightbox").addEventListener("click", () => ($("lightbox").hidden = true));
  let toastT;
  function toast(msg, ms = 3000) { const t = $("toast"); t.textContent = msg; t.hidden = false; clearTimeout(toastT); toastT = setTimeout(() => (t.hidden = true), ms); }
})();
