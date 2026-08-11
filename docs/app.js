/* sim.jiucai.eu.org front-end */
(async function () {
  const MANIFEST = "data/manifest.json";
  let all = [];
  let manifest = {};
  let view = "default"; // default | broadband | all
  // 流量视图（默认推荐/全部资费）与宽带视图各自独立的筛选与排序状态
  const trafficFilters = { source: "", region: "", type: "", fee: null, traffic: null };
  const broadbandFilters = { source: "", region: "", fee: null, bandwidth: null, access: "" };
  const SORT_FIELDS = {
    traffic: [
      { field: "source", label: "运营商" },
      { field: "region", label: "地区" },
      { field: "plan_type", label: "类型" },
      { field: "monthly_fee", label: "月租" },
      { field: "general_traffic_gb", label: "通用流量" },
      { field: "orient_traffic_gb", label: "定向流量" },
      { field: "voice_minutes", label: "语音" },
      { field: "plan_name", label: "套餐名称" },
    ],
    broadband: [
      { field: "source", label: "运营商" },
      { field: "region", label: "地区" },
      { field: "monthly_fee", label: "月租" },
      { field: "broadband_mbps", label: "带宽" },
      { field: "access_method", label: "接入方式" },
      { field: "plan_name", label: "套餐名称" },
    ],
  };
  // 每个视图维护自己的多关键字排序，切换视图互不影响
  const sortKeysByView = { traffic: [], broadband: [] };
  const MAX_SORT_KEYS = 5;
  const NUMERIC_FIELDS = new Set(["monthly_fee", "general_traffic_gb", "orient_traffic_gb", "voice_minutes", "broadband_mbps"]);

  const $ = (id) => document.getElementById(id);
  const tbody = $("tbody");

  function isBroadbandView() { return view === "broadband"; }
  function profile() { return isBroadbandView() ? "broadband" : "traffic"; }
  function currentSortKeys() { return sortKeysByView[profile()]; }
  function currentSortFields() { return SORT_FIELDS[profile()]; }

  // 空值排序哨兵：升序排最后用 +Infinity/最大串，降序排最后用 -Infinity/最小串
  function sortSentinel(field, dir) {
    if (NUMERIC_FIELDS.has(field)) return dir === 1 ? Infinity : -Infinity;
    return dir === 1 ? "\uffff" : "\u0000";
  }
  function sortRows(rows) {
    const keys = currentSortKeys();
    if (!keys.length) return rows;
    return [...rows].sort((a, b) => {
      for (const k of keys) {
        const av = a[k.field] == null ? sortSentinel(k.field, k.dir) : a[k.field];
        const bv = b[k.field] == null ? sortSentinel(k.field, k.dir) : b[k.field];
        if (av < bv) return -1 * k.dir;
        if (av > bv) return 1 * k.dir;
      }
      return 0;
    });
  }
  function fieldLabel(f) {
    const hit = currentSortFields().find((s) => s.field === f);
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
  function fmtBw(r) {
    if (r.broadband_mbps == null) return "-";
    const v = r.broadband_mbps;
    return v >= 1000 && v % 1000 === 0 ? v / 1000 + "G" : v + "M";
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
    // full text, no truncation; the <td> gets a title attr for hover
    let parts = [];
    if (r.broadband) parts.push("[宽带] " + r.broadband);
    if (r.contract_desc) parts.push(r.contract_desc);
    if (r.overage) parts.push("套外:" + r.overage);
    if (r.service_content) parts.push(r.service_content);
    return parts.join(" ");
  }

  function populateSelect(sel, values) {
    for (const v of values.sort()) {
      const opt = document.createElement("option");
      opt.value = v; opt.textContent = v;
      sel.appendChild(opt);
    }
  }

  function matchesTrafficFilters(r) {
    const f = trafficFilters;
    if (f.source && r.source !== f.source) return false;
    if (f.region && r.region !== f.region) return false;
    if (f.type && r.plan_type !== f.type) return false;
    if (f.fee != null && (r.monthly_fee == null || r.monthly_fee > f.fee)) return false;
    if (f.traffic != null && (r.general_traffic_gb == null || r.general_traffic_gb < f.traffic)) return false;
    return true;
  }
  function matchesBroadbandFilters(r) {
    const f = broadbandFilters;
    if (f.source && r.source !== f.source) return false;
    if (f.region && r.region !== f.region) return false;
    if (f.fee != null && (r.monthly_fee == null || r.monthly_fee > f.fee)) return false;
    if (f.bandwidth != null && (r.broadband_mbps == null || r.broadband_mbps < f.bandwidth)) return false;
    if (f.access && r.access_method !== f.access) return false;
    return true;
  }

  function filtered() {
    const bb = isBroadbandView();
    let rows = all.filter(bb ? matchesBroadbandFilters : matchesTrafficFilters);
    if (view === "default") rows = rows.filter((r) => r.default_show);
    if (bb) rows = rows.filter((r) => r.is_broadband);
    rows = sortRows(rows);
    if (!currentSortKeys().length) {
      if (bb) {
        // 宽带默认：带宽降序（未知带宽排最后），再按月租升序
        rows.sort((a, b) => {
          const aB = a.broadband_mbps == null ? -1 : a.broadband_mbps;
          const bB = b.broadband_mbps == null ? -1 : b.broadband_mbps;
          if (aB !== bB) return bB - aB;
          const af = a.monthly_fee == null ? 1e9 : a.monthly_fee;
          const bf = b.monthly_fee == null ? 1e9 : b.monthly_fee;
          return af - bf;
        });
      } else {
        // 流量默认：推荐优先、月租升序
        rows.sort((a, b) => {
          if (a.default_show !== b.default_show) return a.default_show ? -1 : 1;
          const af = a.monthly_fee == null ? 1e9 : a.monthly_fee;
          const bf = b.monthly_fee == null ? 1e9 : b.monthly_fee;
          return af - bf;
        });
      }
    }
    return rows;
  }

  function renderSortKeys() {
    const box = $("sort-keys");
    box.innerHTML = "";
    const fields = currentSortFields();
    if (!currentSortKeys().length) {
      box.innerHTML = isBroadbandView()
        ? '<span class="sort-empty">未设置排序（默认：带宽降序、月租升序）</span>'
        : '<span class="sort-empty">未设置排序（默认：推荐优先、月租升序）</span>';
      return;
    }
    currentSortKeys().forEach((k, idx) => {
      const row = document.createElement("div");
      row.className = "sort-key-row";
      row.innerHTML =
        `<span class="sort-idx">${idx + 1}</span>` +
        `<select class="sort-field" data-idx="${idx}">` +
        fields.map((s) => `<option value="${s.field}" ${s.field === k.field ? "selected" : ""}>${s.label}</option>`).join("") +
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
        const keys = currentSortKeys();
        const pos = keys.findIndex((k) => k.field === field);
        if (pos === 0) {
          // toggle dir asc -> desc -> remove
          if (keys[0].dir === 1) keys[0].dir = -1;
          else keys.splice(0, 1);
        } else if (pos > 0) {
          keys.splice(pos, 1);
          keys.unshift({ field, dir: 1 });
        } else {
          keys.unshift({ field, dir: 1 });
          if (keys.length > MAX_SORT_KEYS) keys.length = MAX_SORT_KEYS;
        }
        renderSortKeys();
        render();
      });
    });
    $("btn-add-sort").addEventListener("click", () => {
      const keys = currentSortKeys();
      if (keys.length >= MAX_SORT_KEYS) return;
      // next unused field
      const used = new Set(keys.map((k) => k.field));
      const next = currentSortFields().find((s) => !used.has(s.field));
      if (!next) return;
      keys.push({ field: next.field, dir: 1 });
      renderSortKeys();
      render();
    });
    $("btn-clear-sort").addEventListener("click", () => {
      currentSortKeys().length = 0;
      renderSortKeys();
      render();
    });
    $("sort-keys").addEventListener("change", (ev) => {
      const t = ev.target;
      const keys = currentSortKeys();
      if (t.classList.contains("sort-field")) {
        keys[+t.dataset.idx].field = t.value;
      } else if (t.classList.contains("sort-dir")) {
        keys[+t.dataset.idx].dir = +t.value;
      }
      renderSortKeys();
      render();
    });
    $("sort-keys").addEventListener("click", (ev) => {
      if (ev.target.classList.contains("sort-del")) {
        currentSortKeys().splice(+ev.target.dataset.idx, 1);
        renderSortKeys();
        render();
      }
    });
  }

  function renderHeaderIndicators() {
    document.querySelectorAll("th[data-sort]").forEach((th) => {
      const field = th.dataset.sort;
      const keys = currentSortKeys();
      const pos = keys.findIndex((k) => k.field === field);
      th.classList.remove("sorted", "sort-asc", "sort-desc");
      const badge = th.querySelector(".sort-badge");
      if (badge) badge.textContent = "";
      if (pos >= 0) {
        th.classList.add("sorted", keys[pos].dir === 1 ? "sort-asc" : "sort-desc");
        badge.textContent = " " + (pos + 1) + (keys[pos].dir === 1 ? "↑" : "↓");
      }
    });
  }

  function render() {
    const rows = filtered();
    const bb = isBroadbandView();
    $("count").textContent = `${rows.length} 条`;
    tbody.innerHTML = "";
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="${bb ? 7 : 9}" class="empty">暂无符合条件的数据</td></tr>`;
      renderHeaderIndicators();
      return;
    }
    const frag = document.createDocumentFragment();
    for (const r of rows) {
      const tr = document.createElement("tr");
      const cls = srcClass(r.source);
      if (bb) {
        tr.innerHTML =
          `<td class="${cls}">${esc(r.source)}</td>` +
          `<td><b>${esc(r.plan_name)}</b><br>${tags(r)}</td>` +
          `<td>${esc(r.region)}</td>` +
          `<td class="num"><b>${fmtFee(r)}</b></td>` +
          `<td class="num"><b>${fmtBw(r)}</b></td>` +
          `<td>${esc(r.access_method || "-")}</td>` +
          `<td class="note" title="${esc(note(r))}">${esc(note(r))}</td>`;
      } else {
        tr.innerHTML =
          `<td class="${cls}">${esc(r.source)}</td>` +
          `<td><b>${esc(r.plan_name)}</b><br>${tags(r)}</td>` +
          `<td>${esc(r.region)}</td>` +
          `<td>${esc(r.plan_type)}</td>` +
          `<td class="num"><b>${fmtFee(r)}</b></td>` +
          `<td class="num">${fmtGb(r.general_traffic_gb)}</td>` +
          `<td class="num">${fmtGb(r.orient_traffic_gb)}</td>` +
          `<td class="num">${r.voice_minutes == null ? "-" : r.voice_minutes}</td>` +
          `<td class="note" title="${esc(note(r))}">${esc(note(r))}</td>`;
      }
      frag.appendChild(tr);
    }
    tbody.appendChild(frag);
    renderHeaderIndicators();
  }

  function readTrafficFilters() {
    trafficFilters.source = $("f-source").value;
    trafficFilters.region = $("f-region").value;
    trafficFilters.type = $("f-type").value;
    const fee = parseFloat($("f-fee").value);
    const tr = parseFloat($("f-traffic").value);
    trafficFilters.fee = isNaN(fee) ? null : fee;
    trafficFilters.traffic = isNaN(tr) ? null : tr;
  }
  function readBroadbandFilters() {
    broadbandFilters.source = $("bf-source").value;
    broadbandFilters.region = $("bf-region").value;
    const fee = parseFloat($("bf-fee").value);
    const bw = parseFloat($("bf-bandwidth").value);
    broadbandFilters.fee = isNaN(fee) ? null : fee;
    broadbandFilters.bandwidth = isNaN(bw) ? null : bw;
    broadbandFilters.access = $("bf-access").value;
  }
  function setView(v) {
    view = v;
    $("btn-default").classList.toggle("active", v === "default");
    $("btn-broadband").classList.toggle("active", v === "broadband");
    $("btn-all").classList.toggle("active", v === "all");
    const bb = v === "broadband";
    $("filters-traffic").hidden = bb;
    $("filters-broadband").hidden = !bb;
    $("th-traffic").hidden = bb;
    $("th-broadband").hidden = !bb;
    renderSortKeys();
    render();
  }
  function bindFilters() {
    const onTraffic = () => { readTrafficFilters(); render(); };
    const onBroadband = () => { readBroadbandFilters(); render(); };
    ["f-source", "f-region", "f-type", "f-fee", "f-traffic"].forEach((id) => {
      $(id).addEventListener("input", onTraffic);
      $(id).addEventListener("change", onTraffic);
    });
    ["bf-source", "bf-region", "bf-fee", "bf-bandwidth", "bf-access"].forEach((id) => {
      $(id).addEventListener("input", onBroadband);
      $(id).addEventListener("change", onBroadband);
    });
    $("btn-default").addEventListener("click", () => setView("default"));
    $("btn-broadband").addEventListener("click", () => setView("broadband"));
    $("btn-all").addEventListener("click", () => setView("all"));
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
    // init filter dropdowns (traffic + broadband 两套独立)
    populateSelect($("f-source"), [...new Set(all.map((r) => r.source))]);
    populateSelect($("f-region"), [...new Set(all.map((r) => r.region))]);
    populateSelect($("f-type"), [...new Set(all.map((r) => r.plan_type))]);
    populateSelect($("bf-source"), [...new Set(all.map((r) => r.source))]);
    populateSelect($("bf-region"), [...new Set(all.map((r) => r.region))]);
    populateSelect($("bf-access"), [...new Set(all.map((r) => r.access_method).filter(Boolean))]);
    if (manifest.updatedAt) $("footer").textContent = `更新于 ${manifest.updatedAt} · 数据来源：${manifest.sources ? manifest.sources.join(" / ") : "四大运营商"} · 共 ${all.length} 条资费`;
    else $("footer").textContent = `共 ${all.length} 条资费`;
    bindFilters();
    bindSortUI();
    renderSortKeys();
    render();
  }

  init();
})();
