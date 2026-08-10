/* sim.jiucai.eu.org front-end */
(async function () {
  const MANIFEST = "data/manifest.json";
  let all = [];
  let manifest = {};
  let view = "default"; // default | all
  const filters = { source: "", region: "", type: "", fee: null, traffic: null };

  const $ = (id) => document.getElementById(id);
  const tbody = $("tbody");

  function fmtFee(r) {
    if (r.monthly_fee == null) return "-";
    return r.monthly_fee % 1 === 0 ? r.monthly_fee : r.monthly_fee.toFixed(1);
  }
  function fmtGb(v) {
    if (v == null) return "-";
    return (v % 1 === 0 ? v : v.toFixed(1)) + "G";
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }
  function srcClass(src) { return "src-" + (src || "").slice(-2); }
  function tags(r) {
    let out = "";
    if (r.region === "北京") out += '<span class="tag bj">北京</span>';
    if (r.plan_type === "流量包") out += '<span class="tag pack">流量包</span>';
    if (r.excluded_phone_contract || r.contract) out += '<span class="tag contract">合约/预存</span>';
    return out;
  }
  function note(r) {
    let parts = [];
    if (r.contract_desc) parts.push(esc(r.contract_desc.slice(0, 40)));
    else if (r.overage) parts.push("套外:" + esc(r.overage.slice(0, 50)));
    else if (r.service_content) parts.push(esc(r.service_content.slice(0, 60)));
    return parts.join(" ");
  }

  function populateSelect(sel, values) {
    for (const v of values.sort()) {
      const opt = document.createElement("option");
      opt.value = v; opt.textContent = v;
      sel.appendChild(opt);
    }
  }

  function matchesFilters(r) {
    if (filters.source && r.source !== filters.source) return false;
    if (filters.region && r.region !== filters.region) return false;
    if (filters.type && r.plan_type !== filters.type) return false;
    if (filters.fee != null && (r.monthly_fee == null || r.monthly_fee > filters.fee)) return false;
    if (filters.traffic != null && (r.general_traffic_gb == null || r.general_traffic_gb < filters.traffic)) return false;
    return true;
  }

  function filtered() {
    let rows = all.filter(matchesFilters);
    if (view === "default") rows = rows.filter((r) => r.default_show);
    rows.sort((a, b) => {
      if (a.default_show !== b.default_show) return a.default_show ? -1 : 1;
      const af = a.monthly_fee == null ? 1e9 : a.monthly_fee;
      const bf = b.monthly_fee == null ? 1e9 : b.monthly_fee;
      return af - bf;
    });
    return rows;
  }

  function render() {
    const rows = filtered();
    $("count").textContent = `${rows.length} 条`;
    tbody.innerHTML = "";
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="9" class="empty">暂无符合条件的数据</td></tr>';
      return;
    }
    const frag = document.createDocumentFragment();
    for (const r of rows) {
      const tr = document.createElement("tr");
      const cls = srcClass(r.source);
      tr.innerHTML =
        `<td class="${cls}">${esc(r.source)}</td>` +
        `<td><b>${esc(r.plan_name)}</b><br>${tags(r)}</td>` +
        `<td>${esc(r.region)}</td>` +
        `<td>${esc(r.plan_type)}</td>` +
        `<td class="num"><b>${fmtFee(r)}</b></td>` +
        `<td class="num">${fmtGb(r.general_traffic_gb)}</td>` +
        `<td class="num">${fmtGb(r.orient_traffic_gb)}</td>` +
        `<td class="num">${r.voice_minutes == null ? "-" : r.voice_minutes}</td>` +
        `<td class="note">${note(r)}</td>`;
      frag.appendChild(tr);
    }
    tbody.appendChild(frag);
  }

  function bindFilters() {
    const on = () => {
      filters.source = $("f-source").value;
      filters.region = $("f-region").value;
      filters.type = $("f-type").value;
      const fee = parseFloat($("f-fee").value);
      const tr = parseFloat($("f-traffic").value);
      filters.fee = isNaN(fee) ? null : fee;
      filters.traffic = isNaN(tr) ? null : tr;
      render();
    };
    ["f-source", "f-region", "f-type", "f-fee", "f-traffic"].forEach((id) => {
      $(id).addEventListener("input", on);
      $(id).addEventListener("change", on);
    });
    $("btn-default").addEventListener("click", () => { view = "default"; $("btn-default").classList.add("active"); $("btn-all").classList.remove("active"); render(); });
    $("btn-all").addEventListener("click", () => { view = "all"; $("btn-all").classList.add("active"); $("btn-default").classList.remove("active"); render(); });
  }

  async function init() {
    let latest = null;
    try {
      const m = await (await fetch(MANIFEST)).json();
      manifest = m;
      latest = await (await fetch(manifest.files.latestJson || "data/latest.json")).json();
    } catch (e) {
      try { latest = await (await fetch("data/latest.json")).json(); } catch (e2) { latest = []; }
    }
    all = Array.isArray(latest) ? latest : (latest && latest.data) || [];
    // init filter dropdowns
    populateSelect($("f-source"), [...new Set(all.map((r) => r.source))]);
    populateSelect($("f-region"), [...new Set(all.map((r) => r.region))]);
    populateSelect($("f-type"), [...new Set(all.map((r) => r.plan_type))]);
    if (manifest.updatedAt) $("footer").textContent = `更新于 ${manifest.updatedAt} · 数据来源：${manifest.sources ? manifest.sources.join(" / ") : "四大运营商"} · 共 ${all.length} 条资费`;
    else $("footer").textContent = `共 ${all.length} 条资费`;
    bindFilters();
    render();
  }

  init();
})();
