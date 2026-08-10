/* sim.jiucai.eu.org front-end */
(async function () {
  const MANIFEST = "data/manifest.json";
  let all = [];
  let manifest = {};
  let view = "default"; // default | all
  const filters = { source: "", region: "", type: "", fee: null, traffic: null };
  const SORT_FIELDS = [
    { field: "source", label: "运营商" },
    { field: "region", label: "地区" },
    { field: "plan_type", label: "类型" },
    { field: "monthly_fee", label: "月租" },
    { field: "general_traffic_gb", label: "通用流量" },
    { field: "orient_traffic_gb", label: "定向流量" },
    { field: "voice_minutes", label: "语音" },
    { field: "plan_name", label: "套餐名称" },
  ];
  let sortKeys = []; // [{field, dir: 1(asc)|-1(desc)}, ...]
  const MAX_SORT_KEYS = 5;

  const $ = (id) => document.getElementById(id);
  const tbody = $("tbody");

  function sortValue(v) {
    return v == null ? Infinity : v;
  }
  function sortRows(rows) {
    const keys = sortKeys;
    if (!keys.length) return rows;
    return [...rows].sort((a, b) => {
      for (const k of keys) {
        const av = sortValue(a[k.field]);
        const bv = sortValue(b[k.field]);
        if (av < bv) return -1 * k.dir;
        if (av > bv) return 1 * k.dir;
      }
      return 0;
    });
  }
  function fieldLabel(f) {
    const hit = SORT_FIELDS.find((s) => s.field === f);
    return hit ? hit.label : f;
  }

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
    if (r.restricted) out += '<span class="tag restricted">门槛</span>';
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
    rows = sortRows(rows);
    if (!sortKeys.length) {
      // default order: recommended first, then by fee asc
      rows.sort((a, b) => {
        if (a.default_show !== b.default_show) return a.default_show ? -1 : 1;
        const af = a.monthly_fee == null ? 1e9 : a.monthly_fee;
        const bf = b.monthly_fee == null ? 1e9 : b.monthly_fee;
        return af - bf;
      });
    }
    return rows;
  }

  function renderSortKeys() {
    const box = $("sort-keys");
    box.innerHTML = "";
    if (!sortKeys.length) {
      box.innerHTML = '<span class="sort-empty">未设置排序（默认：推荐优先、月租升序）</span>';
      return;
    }
    sortKeys.forEach((k, idx) => {
      const row = document.createElement("div");
      row.className = "sort-key-row";
      row.innerHTML =
        `<span class="sort-idx">${idx + 1}</span>` +
        `<select class="sort-field" data-idx="${idx}">` +
        SORT_FIELDS.map((s) => `<option value="${s.field}" ${s.field === k.field ? "selected" : ""}>${s.label}</option>`).join("") +
        `</select>` +
        `<select class="sort-dir" data-idx="${idx}">` +
        `<option value="1" ${k.dir === 1 ? "selected" : ""}>升序 ↑</option>` +
        `<option value="-1" ${k.dir === -1 ? "selected" : ""}>降序 ↓</option>` +
        `</select>` +
        `<button class="sort-del" data-idx="${idx}">×</button>`;
      box.appendChild(row);
    });
  }

  function bindSortUI() {
    // header clicks: set as primary sort (toggle asc/desc/remove)
    document.querySelectorAll("th[data-sort]").forEach((th) => {
      th.addEventListener("click", () => {
        const field = th.dataset.sort;
        const pos = sortKeys.findIndex((k) => k.field === field);
        if (pos === 0) {
          // toggle dir asc -> desc -> remove
          if (sortKeys[0].dir === 1) sortKeys[0].dir = -1;
          else sortKeys.splice(0, 1);
        } else if (pos > 0) {
          sortKeys.splice(pos, 1);
          sortKeys.unshift({ field, dir: 1 });
        } else {
          sortKeys.unshift({ field, dir: 1 });
          if (sortKeys.length > MAX_SORT_KEYS) sortKeys.length = MAX_SORT_KEYS;
        }
        renderSortKeys();
        render();
      });
    });
    $("btn-add-sort").addEventListener("click", () => {
      if (sortKeys.length >= MAX_SORT_KEYS) return;
      // next unused field
      const used = new Set(sortKeys.map((k) => k.field));
      const next = SORT_FIELDS.find((s) => !used.has(s.field));
      if (!next) return;
      sortKeys.push({ field: next.field, dir: 1 });
      renderSortKeys();
      render();
    });
    $("btn-clear-sort").addEventListener("click", () => {
      sortKeys = [];
      renderSortKeys();
      render();
    });
    $("sort-keys").addEventListener("change", (ev) => {
      const t = ev.target;
      if (t.classList.contains("sort-field")) {
        sortKeys[+t.dataset.idx].field = t.value;
      } else if (t.classList.contains("sort-dir")) {
        sortKeys[+t.dataset.idx].dir = +t.value;
      }
      renderSortKeys();
      render();
    });
    $("sort-keys").addEventListener("click", (ev) => {
      if (ev.target.classList.contains("sort-del")) {
        sortKeys.splice(+ev.target.dataset.idx, 1);
        renderSortKeys();
        render();
      }
    });
  }

  function renderHeaderIndicators() {
    document.querySelectorAll("th[data-sort]").forEach((th) => {
      const field = th.dataset.sort;
      const pos = sortKeys.findIndex((k) => k.field === field);
      th.classList.remove("sorted", "sort-asc", "sort-desc");
      const badge = th.querySelector(".sort-badge");
      if (badge) badge.textContent = "";
      if (pos >= 0) {
        th.classList.add("sorted", sortKeys[pos].dir === 1 ? "sort-asc" : "sort-desc");
        badge.textContent = " " + (pos + 1) + (sortKeys[pos].dir === 1 ? "↑" : "↓");
      }
    });
  }

  function render() {
    const rows = filtered();
    $("count").textContent = `${rows.length} 条`;
    tbody.innerHTML = "";
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="9" class="empty">暂无符合条件的数据</td></tr>';
      renderHeaderIndicators();
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
    renderHeaderIndicators();
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
      // filtered.json holds the full corpus; latest.json is the default-shown subset.
      latest = await (await fetch(manifest.files.filteredJson || "data/filtered.json")).json();
    } catch (e) {
      try { latest = await (await fetch("data/filtered.json")).json(); } catch (e2) { latest = []; }
    }
    all = Array.isArray(latest) ? latest : (latest && latest.data) || [];
    // init filter dropdowns
    populateSelect($("f-source"), [...new Set(all.map((r) => r.source))]);
    populateSelect($("f-region"), [...new Set(all.map((r) => r.region))]);
    populateSelect($("f-type"), [...new Set(all.map((r) => r.plan_type))]);
    if (manifest.updatedAt) $("footer").textContent = `更新于 ${manifest.updatedAt} · 数据来源：${manifest.sources ? manifest.sources.join(" / ") : "四大运营商"} · 共 ${all.length} 条资费`;
    else $("footer").textContent = `共 ${all.length} 条资费`;
    bindFilters();
    bindSortUI();
    renderSortKeys();
    render();
  }

  init();
})();
