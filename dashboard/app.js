/* fileak console — renders the dashboard and auto-refreshes from data.json.
   Served over http: fetches data.json and polls for live updates.
   file:// fallback: renders the embedded window.FILEAK_DATA once. */
(function () {
  "use strict";

  const POLL_MS = 3000;
  let lastStamp = null;

  const VERDICT = {
    leak_detected: { cls: "leak", label: "leak" },
    safe: { cls: "safe", label: "safe" },
    inconclusive: { cls: "inconclusive", label: "inconclusive" },
  };

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, html) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  };
  const esc = (s) =>
    String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");

  function render(DATA) {
    const chaos = DATA.chaos || {};
    const tests = DATA.tests || {};
    const plan = DATA.plan || null;
    const advice = DATA.advice || null;
    const artifacts = DATA.artifacts || [];
    const prompts = DATA.prompts || null;
    const findings = chaos.findings || [];
    const profiles = chaos.profiles || [];

    let revealStep = 0;
    const reveal = (node) => {
      node.classList.add("reveal");
      node.style.animationDelay = revealStep * 50 + "ms";
      revealStep += 1;
      return node;
    };
    const clear = (id) => {
      const n = document.getElementById(id);
      if (n) n.innerHTML = "";
      return n;
    };

    (function renderStatus() {
      const cluster = clear("status-cluster");
      const leaks = chaos.leaks_found || 0;
      if (!findings.length) {
        cluster.appendChild(el("div", "verdict-chip", "no run loaded"));
        return;
      }
      const isAlert = leaks > 0;
      cluster.appendChild(
        el("div", "verdict-chip " + (isAlert ? "is-alert" : "is-safe"),
          `<span class="dot"></span>${isAlert ? "VULNERABILITIES FOUND" : "ALL CLEAR"}`)
      );
    })();

    (function renderMetrics() {
      const grid = clear("metric-grid");
      const leaks = chaos.leaks_found || 0;
      const byVerdict = (v) => findings.filter((f) => f.verdict === v).length;
      const metrics = [
        { k: "leaks detected", v: leaks, cls: leaks > 0 ? "alert" : "safe" },
        { k: "safe outcomes", v: byVerdict("safe"), cls: "safe" },
        { k: "profiles run", v: profiles.length || (chaos.profiles_run || []).length, cls: "amber" },
        { k: "findings", v: findings.length,
          sub: chaos.duration_s ? `${chaos.duration_s.toFixed(1)}s` : null, cls: "" },
      ];
      metrics.forEach((m) => {
        const card = reveal(el("div", "metric " + m.cls));
        card.appendChild(el("div", "k", m.k));
        card.appendChild(el("div", "v", esc(m.v) + (m.sub ? ` <small>· ${esc(m.sub)}</small>` : "")));
        grid.appendChild(card);
      });
    })();

    (function renderFlow() {
      const track = clear("flow-track");
      if (!track) return;
      const planned = plan ? (plan.profiles || []).length : profiles.length;
      const dropped = plan ? (plan.dropped || []).length : 0;
      const judged = findings.filter((f) => f.assertion_id !== "_boot_failure").length;
      const boot = findings.filter((f) => f.assertion_id === "_boot_failure").length;
      const fixes = advice && advice.suggestions ? advice.suggestions.length : 0;
      const stages = [
        { n: "01", name: "Plan", actor: plan ? "llm" : "engine",
          meta: plan ? `${planned} authored · ${dropped} dropped` : `${planned} profiles` },
        { n: "02", name: "Inject", actor: "engine", meta: "swap dep -> broken mock" },
        { n: "03", name: "Boot", actor: "engine", meta: boot ? `${boot} boot fail` : "app booted" },
        { n: "04", name: "Judge", actor: "kane", meta: `${judged} assertion${judged === 1 ? "" : "s"}` },
        { n: "05", name: "Verdict", actor: "engine",
          meta: `${chaos.leaks_found || 0} leak${(chaos.leaks_found || 0) === 1 ? "" : "s"}` },
        { n: "06", name: "Advise", actor: fixes ? "llm" : "engine",
          meta: fixes ? `${fixes} fix${fixes === 1 ? "" : "es"}` : "no leaks" },
      ];
      stages.forEach((s) => {
        const stage = reveal(el("div", "flow-stage done"));
        stage.innerHTML =
          `<div class="fs-n">${s.n}</div><div class="fs-name">${esc(s.name)}</div>` +
          `<div class="fs-meta">${esc(s.meta)}</div>` +
          `<span class="fs-actor ${s.actor}">${s.actor}</span>`;
        track.appendChild(stage);
      });
    })();

    (function renderPlanner() {
      const banner = $("#planner-banner");
      if (!plan) { if (banner) banner.hidden = true; return; }
      banner.hidden = false;
      $("#planner-summary").textContent = plan.summary || "LLM-authored chaos plan.";
      const dropped = (plan.dropped || []).length;
      $("#planner-meta").innerHTML =
        `<span><b>model</b> ${esc(plan.model || "—")}</span>` +
        `<span><b>mode</b> ${plan.allow_custom_code ? "free-form mock code" : "vetted templates"}</span>` +
        `<span><b>planned</b> ${(plan.profiles || []).length}</span>` +
        (dropped ? `<span><b>dropped</b> ${dropped}</span>` : "");
    })();

    (function renderExperiments() {
      const wrap = clear("experiments");
      const profileVerdict = (name) => {
        const fs = findings.filter((f) => f.profile_name === name);
        if (fs.some((f) => f.verdict === "leak_detected")) return "leak_detected";
        if (fs.some((f) => f.verdict === "inconclusive")) return "inconclusive";
        return "safe";
      };
      const list = profiles.length ? profiles : (chaos.profiles_run || []).map((n) => ({ name: n }));
      if (!list.length) { wrap.appendChild(el("p", "panel-sub", "No chaos run loaded yet.")); return; }
      list.forEach((p) => {
        const v = profileVerdict(p.name);
        const meta = VERDICT[v];
        const card = reveal(el("div", "exp-card " + meta.cls));
        const top = el("div", "exp-top");
        top.appendChild(el("div", "exp-name", esc(p.name)));
        top.appendChild(el("div", "exp-verdict " + meta.cls, meta.label));
        card.appendChild(top);
        if (p.target_package) {
          const swap = el("div", "exp-swap");
          swap.innerHTML =
            `<span class="pkg">${esc(p.target_package)}</span>` +
            `<span class="arrow">-></span>` +
            `<span class="behavior">${esc(p.behavior || "mock")}</span>`;
          card.appendChild(swap);
        }
        if (p.description) card.appendChild(el("p", "exp-desc", esc(p.description)));
        if (p.rationale) card.appendChild(el("p", "exp-rationale", `<b>rationale</b><br>${esc(p.rationale)}`));
        if (p.custom_source) card.appendChild(el("span", "exp-codeflag", "◇ llm-authored mock"));
        wrap.appendChild(card);
      });
    })();

    (function renderDropped() {
      const wrap = clear("dropped");
      if (!plan || !(plan.dropped || []).length) return;
      plan.dropped.forEach((d) => {
        wrap.appendChild(el("div", "dropped-item",
          `<b>dropped</b> ${esc(d.name)} (${esc(d.target_package)}) — <span class="why">${esc(d.reason)}</span>`));
      });
    })();

    (function renderLedger() {
      const wrap = clear("ledger");
      const head = el("div", "row head");
      ["profile", "assertion", "verdict", "indicators", ""].forEach((h, i) => {
        head.appendChild(el("div", "cell " + ["profile", "assertion", "verdict", "indicators", "chev"][i], esc(h)));
      });
      wrap.appendChild(head);
      if (!findings.length) { wrap.appendChild(el("div", "row", '<div class="cell">No findings yet.</div>')); return; }
      findings.forEach((f) => {
        const meta = VERDICT[f.verdict] || VERDICT.inconclusive;
        const row = reveal(el("div", "row"));
        row.appendChild(el("div", "cell profile", esc(f.profile_name)));
        row.appendChild(el("div", "cell assertion", esc(f.assertion_id)));
        row.appendChild(el("div", "cell", `<span class="tag ${meta.cls}">${meta.label}</span>`));
        const inds = (f.leak_indicators || []).length
          ? `<b>${f.leak_indicators.length}</b> · ${f.leak_indicators.map(esc).join(", ")}` : "—";
        row.appendChild(el("div", "cell indicators", inds));
        row.appendChild(el("div", "cell chev", "›"));
        row.addEventListener("click", () => openDrawer(f));
        wrap.appendChild(row);
      });
    })();

    (function renderFixes() {
      const panel = $("#remediation-panel");
      const wrap = clear("fixes");
      const suggestions = (advice && advice.suggestions) || [];
      if (!suggestions.length) { if (panel) panel.hidden = true; return; }
      panel.hidden = false;
      suggestions.forEach((s) => {
        const sev = (s.severity || "medium").toLowerCase();
        const card = reveal(el("div", "fix-card " + sev));
        card.innerHTML =
          `<div class="fix-top"><div class="fix-where">${esc(s.profile_name)} ` +
          `<small>· ${esc(s.target_package || s.assertion_id)}</small></div>` +
          `<span class="fix-sev ${sev}">${esc(sev)}</span></div>` +
          (s.summary ? `<p class="fix-summary">${esc(s.summary)}</p>` : "") +
          `<p class="fix-body">${esc(s.fix)}</p>`;
        wrap.appendChild(card);
      });
    })();

    (function renderPromptsPanel() {
      const panel = $("#prompts-panel");
      const calls = (prompts && prompts.calls) || [];
      const tabsWrap = clear("prompt-tabs");
      const view = $("#prompt-view");
      if (!calls.length) { if (panel) panel.hidden = true; return; }
      panel.hidden = false;
      const renderCall = (c) =>
        `<div class="prompt-block"><div class="pb-label">system prompt</div>` +
        `<pre class="pb-text">${esc(c.system || "")}</pre></div>` +
        `<div class="prompt-block"><div class="pb-label">user prompt — ${esc(c.stage || "")} · ${esc(c.model || "")}</div>` +
        `<pre class="pb-text">${esc(c.user || "")}</pre></div>` +
        (c.response_excerpt
          ? `<div class="prompt-block"><div class="pb-label">model response (excerpt)</div>` +
            `<pre class="pb-text resp">${esc(c.response_excerpt)}</pre></div>` : "");
      const show = (idx) => {
        view.innerHTML = renderCall(calls[idx]);
        Array.from(tabsWrap.children).forEach((t, i) => t.classList.toggle("active", i === idx));
      };
      calls.forEach((c, i) => {
        const tab = el("button", "tab");
        tab.innerHTML = `${esc(c.stage || "call " + (i + 1))}<span class="tk">${esc(c.model || "")}</span>`;
        tab.addEventListener("click", () => show(i));
        tabsWrap.appendChild(tab);
      });
      show(0);
    })();

    (function renderArtifacts() {
      const panel = $("#artifacts-panel");
      const tabsWrap = clear("artifact-tabs");
      const view = $("#artifact-view");
      if (!artifacts.length) { if (panel) panel.hidden = true; return; }
      panel.hidden = false;
      const highlight = (content, kind) => {
        let html = esc(content);
        if (kind === "json") {
          html = html
            .replace(/(&quot;[^&]*?&quot;)(\s*:)/g, '<span class="j-key">$1</span>$2')
            .replace(/:\s*(&quot;[^&]*?&quot;)/g, ': <span class="j-str">$1</span>')
            .replace(/\b(true|false|null)\b/g, '<span class="j-bool">$1</span>')
            .replace(/:\s*(-?\d+\.?\d*)/g, ': <span class="j-num">$1</span>');
        } else if (kind === "markdown") {
          html = html.replace(/^(#{1,6} .*)$/gm, '<span class="md-h">$1</span>');
        }
        return html;
      };
      const show = (idx) => {
        const a = artifacts[idx];
        view.innerHTML = highlight(a.content, a.kind);
        Array.from(tabsWrap.children).forEach((t, i) => t.classList.toggle("active", i === idx));
      };
      artifacts.forEach((a, i) => {
        const tab = el("button", "tab");
        tab.innerHTML = `${esc(a.name)}<span class="tk">${esc(a.kind)}</span>`;
        tab.title = a.description || a.name;
        tab.addEventListener("click", () => show(i));
        tabsWrap.appendChild(tab);
      });
      show(0);
    })();

    (function renderTests() {
      const total = tests.total || 0, passed = tests.passed || 0;
      const skipped = tests.skipped || 0, failed = tests.failed || 0;
      const summary = clear("test-summary");
      const big = el("div", "test-bigstat");
      big.innerHTML =
        `<div class="v">${passed}<small style="font-size:20px;color:var(--ink-dim)">/${total}</small></div>` +
        `<div class="k">tests passing · ${tests.duration_s || 0}s</div>`;
      summary.appendChild(big);
      const pct = (n) => (total ? (n / total) * 100 : 0);
      const barWrap = el("div");
      barWrap.innerHTML =
        `<div class="bar-track"><div class="bar-pass" style="width:${pct(passed)}%"></div>` +
        `<div class="bar-skip" style="width:${pct(skipped)}%"></div>` +
        `<div class="bar-fail" style="width:${pct(failed)}%"></div></div>` +
        `<div class="bar-legend">` +
        `<span><i class="swatch" style="background:var(--safe)"></i>${passed} passed</span>` +
        `<span><i class="swatch" style="background:var(--line-bright)"></i>${skipped} skipped</span>` +
        `<span><i class="swatch" style="background:var(--alert)"></i>${failed} failed</span></div>`;
      summary.appendChild(barWrap);
      const filesWrap = clear("test-files");
      (tests.files || []).forEach((file) => {
        const card = el("div", "tfile");
        const counts =
          `<span class="ok">${file.passed || 0}✓</span>` +
          (file.skipped ? `<span class="sk">${file.skipped}⏭</span>` : "") +
          (file.failed ? `<span class="er">${file.failed}✗</span>` : "");
        card.innerHTML = `<span class="tname">${esc(file.name)}</span><span class="tcount">${counts}</span>`;
        filesWrap.appendChild(card);
      });
    })();

    if (DATA.generated_at) {
      const d = new Date(DATA.generated_at);
      $("#generated-stamp").textContent = "updated " + d.toLocaleTimeString();
    }
    renumberSections();
  }

  function openDrawer(f) {
    const meta = VERDICT[f.verdict] || VERDICT.inconclusive;
    const evidence = f.evidence || [];
    const eviHtml = evidence.length
      ? evidence.map((e) => `<div class="evi">${esc(e)}</div>`).join("")
      : '<div class="evi none">No leak evidence — graceful failure or clean pass.</div>';
    $("#drawer-body").innerHTML =
      `<h3>${esc(f.profile_name)}</h3>` +
      `<p class="d-sub">assertion · ${esc(f.assertion_id)}</p>` +
      `<dl class="d-meta">` +
      `<dt>verdict</dt><dd><span class="tag ${meta.cls}">${meta.label}</span></dd>` +
      `<dt>kane status</dt><dd>${esc(f.kane_status)}</dd>` +
      `<dt>indicators</dt><dd>${(f.leak_indicators || []).map(esc).join(", ") || "—"}</dd></dl>` +
      `<p class="d-evi-title">evidence (${evidence.length}, truncated &amp; local-only)</p>` + eviHtml;
    $("#drawer").hidden = false;
    $("#drawer-scrim").hidden = false;
  }
  function closeDrawer() { $("#drawer").hidden = true; $("#drawer-scrim").hidden = true; }
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#drawer-scrim").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

  function renumberSections() {
    let n = 1;
    document.querySelectorAll(".panel .idx").forEach((node) => {
      const panel = node.closest(".panel");
      if (panel && !panel.hidden) { node.textContent = String(n).padStart(2, "0"); n += 1; }
    });
  }

  let liveTimer = null;
  function flashLive() {
    const dot = $("#live-dot");
    if (!dot) return;
    dot.classList.add("pulse");
    clearTimeout(liveTimer);
    liveTimer = setTimeout(() => dot.classList.remove("pulse"), 1200);
  }
  function applyIfNew(data) {
    if (!data) return;
    if (data.generated_at && data.generated_at === lastStamp) return;
    lastStamp = data.generated_at || String(Date.now());
    render(data);
    flashLive();
  }
  function poll() {
    fetch("data.json", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null)).then(applyIfNew).catch(() => {});
  }
  fetch("data.json", { cache: "no-store" })
    .then((r) => { if (!r.ok) throw new Error("no data.json"); return r.json(); })
    .then((data) => {
      applyIfNew(data);
      setInterval(poll, POLL_MS);
      const ind = $("#live-indicator");
      if (ind) ind.hidden = false;
    })
    .catch(() => { if (window.FILEAK_DATA) render(window.FILEAK_DATA); });
})();
