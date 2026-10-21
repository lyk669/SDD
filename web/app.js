/**
 * app.js — 日报管道工作台（原生 JS）
 *
 * 后端本质是「定时日报管道」，不是从 GitHub 抓取仓库正文：
 *   采集(commit 元数据 + 飞书任务变更) → 按成员聚合 → 生成 Markdown/HTML → SMTP 推送 → SQLite 存档
 * 本页负责：查看历史日报、维护成员映射、配置采集/推送、手动触发管道。
 */
(function () {
  "use strict";

  /* ====================================================================== *
   * 1. 图标与工具
   * ====================================================================== */
  var ICONS = {
    reports: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6"/><path d="M9 17h4"/>',
    plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
    download: '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>',
    mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/>',
    refresh: '<path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 4v5h-5"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    checkCircle: '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
    alert: '<path d="M10.3 3.9 2 18a2 2 0 0 0 1.7 3h16.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/>',
    edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
    trash: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/>',
    close: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    chevron: '<path d="m6 9 6 6 6-6"/>',
    back: '<path d="m12 19-7-7 7-7"/><path d="M19 12H5"/>',
    commit: '<circle cx="12" cy="12" r="3"/><path d="M3 12h6"/><path d="M15 12h6"/>',
    task: '<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>',
    lock: '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
    inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.5 5.5 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.5A2 2 0 0 0 16.7 4H7.3a2 2 0 0 0-1.8 1.5z"/>',
    eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    sort: '<path d="m7 15 5 5 5-5"/><path d="m7 9 5-5 5 5"/>',
  };

  function icon(name, size) {
    var s = size || 18;
    return (
      '<svg viewBox="0 0 24 24" width="' +
      s +
      '" height="' +
      s +
      '" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      (ICONS[name] || "") +
      "</svg>"
    );
  }

  function esc(v) {
    if (v === null || v === undefined) return "";
    return String(v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function dash(v) {
    return v === null || v === undefined || v === "" ? "—" : String(v);
  }

  function fmtTime(iso) {
    if (!iso) return "—";
    var m = String(iso).match(/T(\d{2}:\d{2})/);
    return m ? m[1] : "—";
  }

  function fmtDateTime(s) {
    if (!s) return "—";
    return String(s).replace("T", " ").replace(/\+.*/, "").replace(/\.\d+$/, "");
  }

  function initial(name) {
    return (name || "?").trim().charAt(0) || "?";
  }

  function download(filename, content, mime) {
    var blob = new Blob([content], { type: mime || "text/plain;charset=utf-8" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    setTimeout(function () {
      URL.revokeObjectURL(url);
    }, 0);
  }

  function identityKeys(member) {
    var keys = {};
    [member.github, member.lark, member.lark_open_id, member.lark_user_id].forEach(function (k) {
      if (k && String(k).trim()) keys[String(k).trim().toLowerCase()] = true;
    });
    return Object.keys(keys);
  }

  /* ====================================================================== *
   * 2. 应用状态
   * ====================================================================== */
  var state = {
    reports: [],
    members: [],
    settings: null,
    teamName: "",
    listLoading: false,
    listError: "",
    filters: { dateFrom: "", dateTo: "", team: "全部", order: "desc" },
    page: 1,
    pageSize: 6,
    detailTab: "members",
    detail: null,
    detailAnalysis: null,
    detailLoading: false,
    runExpanded: {},
    tokenDraft: ReportAPI.getConfigToken(),
    emails: [],
    emailLoading: false,
    emailError: "",
    emailFilters: { dateFrom: "", dateTo: "" },
    emailDetail: null,
    emailTab: "preview",
  };

  var ROUTE_TITLES = {
    reports: "日报历史",
    emails: "发送记录",
    members: "成员映射",
    settings: "采集与推送",
    runs: "管道运行",
  };

  /** 管道说明条：说明采集范围（会采仓库提交，但不进日报的是 diff/源码） */
  function pipelineBanner() {
    return (
      '<div class="notice notice--info" style="margin:0">' +
      icon("info", 16) +
      "<span>会从配置的 GitHub 仓库采集当日 <strong>commit 记录</strong>（说明、作者、+行/−行、改动文件数），" +
      "并从飞书采集任务状态变更；再按成员生成日报、可选邮件推送。" +
      "写入日报的是提交摘要与统计，按设计<strong>不含代码 diff / 源文件正文</strong>。</span></div>"
    );
  }

  /* ====================================================================== *
   * 3. Toast / Modal
   * ====================================================================== */
  function toast(tone, title, msg) {
    var region = document.getElementById("toastRegion");
    var el = document.createElement("div");
    el.className = "toast toast--" + (tone || "info");
    el.innerHTML =
      '<span class="toast__icon">' +
      icon(tone === "ok" ? "checkCircle" : tone === "error" ? "alert" : "info") +
      "</span>" +
      '<div class="toast__body"><div class="toast__title">' +
      esc(title) +
      '</div><div class="toast__msg">' +
      esc(msg || "") +
      "</div></div>" +
      '<button class="toast__close" aria-label="关闭通知" data-action="toast-close">' +
      icon("close", 16) +
      "</button>";
    region.appendChild(el);
    var timer = setTimeout(function () {
      dismissToast(el);
    }, 3200);
    el.addEventListener("mouseenter", function () {
      clearTimeout(timer);
    });
    el.addEventListener("mouseleave", function () {
      timer = setTimeout(function () {
        dismissToast(el);
      }, 1600);
    });
  }

  function dismissToast(el) {
    if (!el || !el.parentNode) return;
    el.classList.add("is-leaving");
    setTimeout(function () {
      el.remove();
    }, 180);
  }

  var modalState = { onConfirm: null };

  function openModal(opts) {
    var root = document.getElementById("modalRoot");
    modalState.onConfirm = opts.onConfirm || null;
    root.innerHTML =
      '<div class="modal" role="dialog" aria-modal="true">' +
      '<div class="modal__head"><h2>' +
      esc(opts.title || "确认") +
      '</h2><button class="row-action" style="margin-left:auto" aria-label="关闭" data-action="modal-close">' +
      icon("close", 16) +
      "</button></div>" +
      '<div class="modal__body">' +
      (opts.body || "") +
      "</div>" +
      '<div class="modal__foot">' +
      '<button class="btn" data-action="modal-close">' +
      esc(opts.cancelLabel || "取消") +
      "</button>" +
      '<button class="btn ' +
      (opts.tone === "danger" ? "btn--danger" : "btn--primary") +
      '" data-action="modal-confirm">' +
      esc(opts.confirmLabel || "确认") +
      "</button>" +
      "</div></div>";
  }

  function closeModal() {
    document.getElementById("modalRoot").innerHTML = "";
    modalState.onConfirm = null;
  }

  /* ====================================================================== *
   * 4. 路由
   * ====================================================================== */
  function parseHash() {
    var raw = (location.hash || "").replace(/^#\/?/, "");
    var parts = raw.split("/").filter(Boolean);
    if (!parts.length) return { name: "reports", param: "" };
    return { name: parts[0], param: parts.slice(1).join("/") };
  }

  function navigate(path) {
    location.hash = "#/" + path;
  }

  function setNavActive(name) {
    Array.prototype.forEach.call(document.querySelectorAll(".nav__item"), function (el) {
      if (el.getAttribute("data-route") === name) el.setAttribute("aria-current", "page");
      else el.removeAttribute("aria-current");
    });
  }

  function setTopbar(title, crumb) {
    document.getElementById("topbarTitle").textContent = title;
    document.getElementById("topbarCrumb").textContent = crumb || "";
  }

  function renderTopActions() {
    document.getElementById("topbarActions").innerHTML =
      '<button class="btn btn--primary" data-action="generate" title="触发采集 → 生成 → 写入 SQLite（默认 dry-run，不发信）">' +
      icon("refresh") +
      "生成今日日报</button>";
  }

  async function routeTo() {
    var r = parseHash();
    var view;
    try {
      switch (r.name) {
        case "reports":
          view = r.param ? await pageReportDetail(r.param) : await pageReportsList();
          break;
        case "emails":
          view = r.param ? await pageEmailDetail(r.param) : await pageEmailsList();
          break;
        case "members":
          if (r.param === "new") view = await pageMemberForm(null);
          else if (/\/edit$/.test(r.param))
            view = await pageMemberForm(r.param.replace(/\/edit$/, ""));
          else view = await pageMembersList();
          break;
        case "settings":
          view = await pageSettings();
          break;
        case "runs":
          view = await pageRuns();
          break;
        default:
          view = notFoundView("未找到该页面", "请从左侧导航选择一个模块。");
      }
    } catch (err) {
      view = {
        title: ROUTE_TITLES[r.name] || "错误",
        crumb: "",
        html:
          '<div class="page"><div class="card">' +
          errorState("加载失败", err.message || String(err), '<button class="btn" data-action="list-refresh">重试</button>') +
          "</div></div>",
      };
    }

    document.getElementById("main").innerHTML = view.html;
    setNavActive(r.name);
    setTopbar(view.title, view.crumb);
    renderTopActions();
    if (typeof view.mount === "function") view.mount();
    window.scrollTo(0, 0);
  }

  /* ====================================================================== *
   * 5. 通用 UI 片段
   * ====================================================================== */
  function badge(text, tone) {
    var cls =
      { ok: "badge--ok", warn: "badge--warn", danger: "badge--danger", info: "badge--info", mute: "badge--mute" }[
        tone
      ] || "";
    return '<span class="badge ' + cls + '">' + esc(text) + "</span>";
  }

  function reportStatusBadge(status) {
    return status === "ok" ? badge("正常", "ok") : badge("降级", "warn");
  }

  function emptyState(title, desc, actionHtml) {
    return (
      '<div class="state">' +
      icon("inbox", 40) +
      "<h3>" +
      esc(title) +
      "</h3><p>" +
      esc(desc) +
      "</p>" +
      (actionHtml || "") +
      "</div>"
    );
  }

  function errorState(title, message, actionHtml) {
    return (
      '<div class="state state--error">' +
      icon("alert", 40) +
      "<h3>" +
      esc(title) +
      "</h3><p>" +
      esc(message) +
      "</p>" +
      (actionHtml || "") +
      "</div>"
    );
  }

  function notFoundView(title, desc) {
    return {
      title: "未找到",
      crumb: "",
      html:
        '<div class="page"><div class="card"><div class="state">' +
        icon("alert", 40) +
        "<h3>" +
        esc(title) +
        "</h3><p>" +
        esc(desc) +
        "</p>" +
        '<a class="btn" href="#/reports">' +
        icon("back") +
        "返回日报列表</a></div></div></div>",
    };
  }

  function loadingRows(n) {
    var html = '<div class="card__body card__body--flush">';
    for (var i = 0; i < n; i++) html += '<div class="skeleton skel-row"></div>';
    return html + "</div>";
  }

  /* ====================================================================== *
   * 6. 日报列表
   * ====================================================================== */
  function validateDateRange() {
    var f = state.filters;
    if (f.dateFrom && f.dateTo && f.dateFrom > f.dateTo) {
      return "开始日期（" + f.dateFrom + "）晚于结束日期（" + f.dateTo + "）";
    }
    return "";
  }

  function sortedReports() {
    var list = state.reports.slice();
    list.sort(function (a, b) {
      return state.filters.order === "asc"
        ? a.report_date < b.report_date
          ? -1
          : 1
        : a.report_date > b.report_date
          ? -1
          : 1;
    });
    return list;
  }

  async function loadReports() {
    state.listError = validateDateRange();
    if (state.listError) {
      state.reports = [];
      return;
    }
    state.listLoading = true;
    try {
      state.reports = await ReportAPI.listReports(state.filters);
      if (!state.teamName && state.reports.length) {
        state.teamName = state.reports[0].team_name;
      }
    } catch (err) {
      state.listError = err.message || String(err);
      state.reports = [];
    } finally {
      state.listLoading = false;
    }
  }

  async function pageReportsList() {
    if (!state.reports.length && !state.listError && !state.listLoading) {
      await loadReports();
    }

    var headActions =
      '<button class="btn btn--primary" data-action="generate">' +
      icon("refresh") +
      "生成今日日报</button>" +
      '<button class="btn" data-action="list-refresh">' +
      icon("refresh") +
      "刷新列表</button>";

    var body;
    if (state.listLoading) body = loadingRows(5);
    else if (state.listError)
      body = errorState(
        "查询失败",
        state.listError,
        '<button class="btn" data-action="list-reset">清除筛选</button>'
      );
    else body = reportTable();

    var teams = ["全部"];
    if (state.teamName) teams.push(state.teamName);
    state.reports.forEach(function (r) {
      if (teams.indexOf(r.team_name) === -1) teams.push(r.team_name);
    });
    var teamOptions = teams
      .map(function (t) {
        return (
          '<option value="' +
          esc(t) +
          '"' +
          (state.filters.team === t ? " selected" : "") +
          ">" +
          esc(t) +
          "</option>"
        );
      })
      .join("");

    return {
      title: "日报历史",
      crumb: (state.teamName || "团队") + " · SQLite 存档",
      html:
        '<div class="page stack-6">' +
        pipelineBanner() +
        '<div class="page-head">' +
        '<div class="page-head__main"><h2>日报历史</h2>' +
        '<p class="page-head__desc">查看管道已生成的团队日报（按成员整理「代码提交 / 任务进展」）。可筛选日期、手动再跑一遍管道，或把某日日报发信给 Leader。</p></div>' +
        '<div class="page-head__actions">' +
        headActions +
        "</div></div>" +
        '<div class="card">' +
        '<div class="toolbar" role="search">' +
        '<div class="field"><label class="field__label" for="f-from">开始日期</label>' +
        '<input class="input" type="date" id="f-from" data-filter="dateFrom" value="' +
        esc(state.filters.dateFrom) +
        '"/></div>' +
        '<div class="field"><label class="field__label" for="f-to">结束日期</label>' +
        '<input class="input" type="date" id="f-to" data-filter="dateTo" value="' +
        esc(state.filters.dateTo) +
        '"/></div>' +
        '<div class="field"><label class="field__label" for="f-team">团队</label>' +
        '<select class="select" id="f-team" data-filter="team">' +
        teamOptions +
        "</select></div>" +
        '<div class="toolbar__spacer"></div>' +
        '<button class="btn btn--ghost" data-action="list-reset">清除筛选</button>' +
        "</div>" +
        body +
        "</div></div>",
    };
  }

  function reportTable() {
    var list = sortedReports();
    if (!list.length) {
      return emptyState(
        "暂无日报存档",
        "SQLite 里还没有日报。请先配置成员与数据源，再点击「生成今日日报」（等价于命令行 main.py --dry-run --force）。",
        '<button class="btn btn--primary" data-action="generate">' + icon("refresh") + "生成今日日报</button>"
      );
    }

    var totalPages = Math.max(1, Math.ceil(list.length / state.pageSize));
    state.page = Math.min(state.page, totalPages);
    var start = (state.page - 1) * state.pageSize;
    var rows = list.slice(start, start + state.pageSize);
    var active = state.filters.order === "asc";

    var head =
      "<thead><tr>" +
      '<th><button class="sort-btn" data-action="list-sort" data-active="' +
      active +
      '">日期 ' +
      icon("sort", 13) +
      "</button></th>" +
      "<th>团队</th>" +
      '<th class="num">成员</th>' +
      '<th class="num">提交数 / 任务数</th>' +
      "<th>管道结果</th>" +
      "<th>邮件</th>" +
      "<th>生成时间</th>" +
      '<th class="col-actions">操作</th>' +
      "</tr></thead>";

    var body = rows
      .map(function (r) {
        return (
          "<tr>" +
          '<td data-label="日期"><span class="num">' +
          esc(r.report_date) +
          "</span></td>" +
          '<td data-label="团队">' +
          esc(r.team_name) +
          "</td>" +
          '<td class="num" data-label="成员">' +
          r.member_count +
          "</td>" +
          '<td class="num" data-label="提交数 / 任务数"><span class="num">' +
          r.commits_count +
          " / " +
          r.tasks_count +
          "</span></td>" +
          '<td data-label="管道结果">' +
          reportStatusBadge(r.status) +
          "</td>" +
          '<td data-label="邮件">' +
          (r.email_sent ? badge("已发送", "ok") : badge("未发送", "mute")) +
          "</td>" +
          '<td data-label="生成时间"><span class="num">' +
          esc(fmtDateTime(r.generated_at)) +
          "</span></td>" +
          '<td class="col-actions" data-label="操作">' +
          '<button class="row-action" title="查看详情" data-action="open-report" data-id="' +
          esc(r.id) +
          '">' +
          icon("eye", 17) +
          "</button></td></tr>"
        );
      })
      .join("");

    var pager =
      '<div class="pagination" data-pagination>' +
      "<span>共 " +
      list.length +
      " 条 · 第 " +
      state.page +
      " / " +
      totalPages +
      " 页</span>" +
      '<span class="flex-row">' +
      '<button class="btn btn--sm" data-action="page-prev"' +
      (state.page <= 1 ? " disabled" : "") +
      ">上一页</button>" +
      '<button class="btn btn--sm" data-action="page-next"' +
      (state.page >= totalPages ? " disabled" : "") +
      ">下一页</button>" +
      "</span></div>";

    return (
      '<div class="table-wrap"><table class="table table--stack">' +
      head +
      "<tbody>" +
      body +
      "</tbody></table></div>" +
      pager
    );
  }

  /* ====================================================================== *
   * 7. 日报详情
   * ====================================================================== */
  async function pageReportDetail(idOrDate) {
    var report = null;
    // 支持 id 或日期跳转
    if (/^\d+$/.test(idOrDate)) {
      report = await ReportAPI.getReport(Number(idOrDate));
    } else {
      var list = state.reports.length ? state.reports : await ReportAPI.listReports({});
      state.reports = list;
      var hit = list.find(function (x) {
        return x.report_date === idOrDate;
      });
      if (!hit) return notFoundView("未找到该日报", "日期 " + idOrDate + " 没有对应的日报记录。");
      report = await ReportAPI.getReport(hit.id);
    }
    state.detail = report;

    var analysis = null;
    try {
      analysis = await ReportAPI.getReportAnalysis(report.id);
    } catch (_) {
      analysis = null;
    }
    state.detailAnalysis = analysis;

    var actions =
      '<button class="btn" data-action="report-regenerate" title="重新跑一遍采集→生成管道">' +
      icon("refresh") +
      "重新跑管道</button>" +
      '<button class="btn btn--primary" data-action="report-send" data-id="' +
      esc(report.id) +
      '">' +
      icon("mail") +
      "推送给 Leader</button>" +
      '<button class="btn" data-action="export-md">' +
      icon("download") +
      "Markdown</button>" +
      '<button class="btn" data-action="export-html">' +
      icon("download") +
      "HTML</button>";

    return {
      title: "日报详情",
      crumb: report.report_date + " · " + report.team_name,
      html:
        '<div class="page stack-6">' +
        '<div class="page-head"><div class="page-head__main">' +
        '<a class="btn btn--ghost btn--sm" href="#/reports" style="align-self:flex-start;margin-bottom:4px">' +
        icon("back", 16) +
        "返回日报历史</a>" +
        "<h2>" +
        esc(report.team_name) +
        " 日报 — " +
        esc(report.report_date) +
        "</h2>" +
        '<p class="page-head__desc">本页展示管道产出：每位成员的 GitHub 提交记录与飞书任务变更（摘要与统计，不含 diff 源码）。</p>' +
        '<div class="flex-row" style="gap:12px">' +
        reportStatusBadge(report.status) +
        (report.email_sent
          ? badge("邮件已发送", "ok") +
            '<span class="muted" style="font-size:13px">发送时间 ' +
            esc(fmtDateTime(report.last_sent_at)) +
            "</span>"
          : badge("邮件未发送", "mute")) +
        '<span class="muted" style="font-size:13px">生成时间 ' +
        esc(fmtDateTime(report.generated_at)) +
        "</span></div></div>" +
        '<div class="page-head__actions">' +
        actions +
        "</div></div>" +
        '<div class="card">' +
        sourceStrip(report) +
        '<div class="tabs" role="tablist">' +
        tabBtn("analysis", "分析", state.detailTab === "analysis") +
        tabBtn("members", "按成员查看", state.detailTab === "members") +
        tabBtn("reflection", "反思", state.detailTab === "reflection") +
        tabBtn("preview", "邮件 HTML", state.detailTab === "preview") +
        tabBtn("markdown", "Markdown", state.detailTab === "markdown") +
        "</div>" +
        detailPanel(report) +
        "</div></div>",
    };
  }

  function tabBtn(id, label, selected) {
    return (
      '<button class="tab" role="tab" aria-selected="' +
      selected +
      '" data-action="detail-tab" data-tab="' +
      id +
      '">' +
      esc(label) +
      "</button>"
    );
  }

  function sourceStrip(r) {
    function card(name, src) {
      src = src || { success: true, count: 0, error: "" };
      return (
        '<div class="source-card"><div class="source-card__top">' +
        '<span class="source-card__name">' +
        esc(name) +
        '</span><span class="muted" style="font-size:12px">采集 ' +
        (src.count || 0) +
        " 条</span>" +
        (src.success ? badge("正常", "ok") : badge("获取失败", "danger")) +
        "</div>" +
        (src.error ? '<p class="source-card__err">' + esc(src.error) + "</p>" : "") +
        "</div>"
      );
    }
    var sources = r.sources || {};
    return (
      '<div class="source-strip" style="padding:16px 24px">' +
      card("代码提交（commit 元数据）", sources.github) +
      card("任务进展（状态变更）", sources.lark) +
      "</div>"
    );
  }

  function detailPanel(r) {
    if (state.detailTab === "analysis") {
      return analysisPanel();
    }
    if (state.detailTab === "reflection") {
      return reflectionPanel();
    }
    if (state.detailTab === "preview") {
      return (
        '<div class="card__body card__body--flush"><iframe class="preview-frame" title="日报 HTML 预览" sandbox="" srcdoc="' +
        esc(r.html) +
        '"></iframe></div>'
      );
    }
    if (state.detailTab === "markdown") {
      return '<pre class="code-block" aria-label="Markdown 源码">' + esc(r.markdown) + "</pre>";
    }
    var members = r.members || [];
    if (!members.length) {
      return (
        '<div class="card__body">' +
        emptyState("暂无成员明细", "该日报没有可解析的成员分区，可切换到 HTML / Markdown 查看。") +
        "</div>"
      );
    }
    return (
      '<div class="card__body card__body--flush">' +
      members.map(memberBlock).join("") +
      "</div>"
    );
  }

  function memberBlock(m) {
    return (
      '<section class="member-block"><header class="member-block__head">' +
      '<span class="avatar" aria-hidden="true">' +
      esc(initial(m.name)) +
      '</span><span><span class="member-block__name">' +
      esc(m.name) +
      '</span><span class="member-block__handle">@' +
      esc(m.github_username || "—") +
      '</span></span><span class="member-block__counts">' +
      '<span class="chip">' +
      icon("commit", 14) +
      " " +
      (m.commits || []).length +
      '</span><span class="chip">' +
      icon("task", 14) +
      " " +
      (m.tasks || []).length +
      "</span></span></header>" +
      '<div class="member-cols"><div class="member-col">' +
      commitsCol(m) +
      '</div><div class="member-col">' +
      tasksCol(m) +
      "</div></div></section>"
    );
  }

  function colTitle(iconName, label, count) {
    return (
      '<h3 class="member-col__title">' +
      icon(iconName, 15) +
      esc(label) +
      '<span class="count">' +
      count +
      "</span></h3>"
    );
  }

  function notice(tone, text) {
    var ic = tone === "mute" ? "info" : "alert";
    var cls =
      tone === "danger"
        ? "notice--danger"
        : tone === "warn"
          ? "notice--warn"
          : tone === "mute"
            ? "notice--mute"
            : "notice--info";
    return (
      '<div class="notice ' +
      cls +
      '">' +
      icon(ic, 16) +
      "<span>" +
      esc(text) +
      "</span></div>"
    );
  }

  function commitsCol(m) {
    var body;
    if (m.commits_failed && m.commits && m.commits.length)
      body = notice("warn", "数据获取失败（以下为已成功采集的部分）") + m.commits.map(commitRow).join("");
    else if (m.commits_failed) body = notice("danger", "数据获取失败");
    else if (!m.commits || !m.commits.length) body = notice("mute", "今日无记录");
    else body = m.commits.map(commitRow).join("");
    return colTitle("commit", "代码提交", (m.commits || []).length) + body;
  }

  function tasksCol(m) {
    var body;
    if (m.tasks_failed && m.tasks && m.tasks.length)
      body = notice("warn", "数据获取失败（以下为已成功采集的部分）") + m.tasks.map(taskRow).join("");
    else if (m.tasks_failed) body = notice("danger", "数据获取失败");
    else if (!m.tasks || !m.tasks.length) body = notice("mute", "今日无记录");
    else body = m.tasks.map(taskRow).join("");
    return colTitle("task", "任务进展", (m.tasks || []).length) + body;
  }

  function commitRow(c) {
    return (
      '<div class="record"><div class="record__main"><span class="chip">' +
      esc(c.repo) +
      '</span><span class="record__msg">' +
      esc(c.message) +
      '</span></div><div class="record__meta"><span class="delta"><span class="add">+' +
      c.additions +
      '</span> <span class="del">-' +
      c.deletions +
      "</span></span><span>" +
      c.files_changed +
      ' files</span><span class="record__time">' +
      esc(fmtTime(c.timestamp)) +
      "</span></div></div>"
    );
  }

  function taskRow(t) {
    return (
      '<div class="record"><div class="record__main"><span class="record__title">' +
      esc(t.title) +
      '</span></div><div class="record__meta"><span class="status-flow"><span class="from">' +
      esc(t.status_from || "—") +
      '</span><span class="arrow">→</span><span class="to" data-status="' +
      esc(t.status_to) +
      '">' +
      esc(t.status_to) +
      '</span></span><span class="record__time">' +
      esc(fmtTime(t.updated_at)) +
      "</span></div></div>"
    );
  }

  function analysisSection(title, body) {
    return (
      '<section><h3 class="member-col__title" style="margin-bottom:8px">' +
      esc(title) +
      "</h3>" +
      body +
      "</section>"
    );
  }

  function actionItemsHtml(items) {
    var typeCn = { decision: "决策", todo: "待办", issue: "问题" };
    return (
      '<div class="stack-4">' +
      items
        .map(function (it) {
          var t = typeCn[it.type] || "其他";
          var tone = it.type === "decision" ? "ok" : it.type === "todo" ? "info" : "warn";
          return (
            '<div class="record"><div class="record__main"><span style="margin-right:8px">' +
            badge(t, tone) +
            '</span><span class="record__title">' +
            esc(it.text) +
            '</span></div><div class="record__meta"><span>' +
            esc(it.people || "—") +
            "</span></div></div>"
          );
        })
        .join("") +
      "</div>"
    );
  }

  function risksHtml(risks) {
    return (
      '<div class="stack-4">' +
      risks
        .map(function (r) {
          return (
            '<div class="record"><div class="record__main"><span class="record__title">' +
            esc(r.text) +
            '</span></div><div class="record__meta">' +
            badge(r.category || "风险", "warn") +
            "</div></div>"
          );
        })
        .join("") +
      "</div>"
    );
  }

  function analysisPanel() {
    var a = state.detailAnalysis;
    if (!a) {
      return (
        '<div class="card__body">' +
        emptyState("暂无分析数据", "该日报没有归档 AI 分析，可能是早期版本或未配置 DEEPSEEK_API_KEY。") +
        "</div>"
      );
    }
    var blocks = [];
    if (a.summary) {
      blocks.push(
        analysisSection(
          "一句话结论",
          '<div class="readonly-note" style="margin:0">' + esc(a.summary).replace(/\n/g, "<br>") + "</div>"
        )
      );
    }
    var cmp = a.comparison;
    if (cmp && (cmp.summary || (cmp.changes || []).length || cmp.trend)) {
      var inner = "";
      if (cmp.summary) inner += '<p style="margin:0 0 6px">' + esc(cmp.summary) + "</p>";
      if (cmp.trend) inner += '<div style="margin-bottom:6px">' + badge("趋势：" + cmp.trend, "info") + "</div>";
      if ((cmp.changes || []).length) {
        inner +=
          '<ul style="margin:0;padding-left:18px">' +
          cmp.changes.map(function (c) {
            return "<li>" + esc(c) + "</li>";
          }).join("") +
          "</ul>";
      }
      blocks.push(analysisSection("环比结论", inner));
    }
    if ((a.highlights || []).length) {
      blocks.push(
        analysisSection(
          "今日亮点",
          '<ol style="margin:0;padding-left:20px">' +
            a.highlights.map(function (h) {
              return "<li>" + esc(h) + "</li>";
            }).join("") +
            "</ol>"
        )
      );
    }
    if ((a.action_items || []).length) {
      blocks.push(analysisSection("群讨论要点", actionItemsHtml(a.action_items)));
    }
    if ((a.risks || []).length) {
      blocks.push(analysisSection("风险与待办", risksHtml(a.risks)));
    }
    if (!blocks.length) {
      return (
        '<div class="card__body">' +
        emptyState("暂无分析数据", "该日报没有可展示的分析内容。") +
        "</div>"
      );
    }
    return '<div class="card__body stack-6">' + blocks.join("") + "</div>";
  }

  function reflectionList(title, finalItems, aiItems, source) {
    function texts(items) {
      return (items || [])
        .map(function (it) {
          return typeof it === "string" ? it : it.text || "";
        })
        .filter(function (t) {
          return t;
        });
    }
    var finals = texts(finalItems);
    var ais = texts(aiItems);
    var body = "";
    if (finals.length) {
      body +=
        '<ol style="margin:0;padding-left:20px">' +
        finals.map(function (t) {
          return "<li>" + esc(t) + "</li>";
        }).join("") +
        "</ol>";
    } else {
      body += '<p class="muted" style="margin:0">（无内容）</p>';
    }
    if (source !== "ai" && ais.length) {
      body +=
        '<details style="margin-top:8px"><summary style="cursor:pointer;color:#4a6cf7">AI 草稿（点开对比）</summary>' +
        '<ol style="margin:6px 0 0;padding-left:20px;color:#666">' +
        ais.map(function (t) {
          return "<li>" + esc(t) + "</li>";
        }).join("") +
        "</ol></details>";
    }
    return (
      '<section><h3 class="member-col__title" style="margin-bottom:8px">' +
      esc(title) +
      '<span class="count">' +
      finals.length +
      "</span></h3>" +
      body +
      "</section>"
    );
  }

  function reflectionPanel() {
    var a = state.detailAnalysis;
    if (!a) {
      return (
        '<div class="card__body">' +
        emptyState("暂无反思数据", "该日报没有归档问题与思考 / 明日计划。") +
        "</div>"
      );
    }
    var problems = a.problems || [];
    var nextPlan = a.next_plan || [];
    var aiProblems = a.ai_problems || [];
    var aiNextPlan = a.ai_next_plan || [];
    if (!problems.length && !nextPlan.length && !aiProblems.length && !aiNextPlan.length) {
      return (
        '<div class="card__body">' +
        emptyState("暂无反思数据", "该日报没有可展示的问题与思考 / 明日计划。") +
        "</div>"
      );
    }
    var srcLabel = { ai: "AI 草稿", manual: "用户输入", hybrid: "AI + 用户混合" }[a.reflection_source] || "AI 草稿";
    return (
      '<div class="card__body stack-6">' +
      '<div class="notice notice--info" style="margin:0">' +
      icon("info", 16) +
      "<span>来源：" +
      esc(srcLabel) +
      "。AI 草稿与最终内容并列展示，便于对比。</span></div>" +
      reflectionList("问题与思考", problems, aiProblems, a.reflection_source) +
      reflectionList("明日计划", nextPlan, aiNextPlan, a.reflection_source) +
      "</div>"
    );
  }

  /* ====================================================================== *
   * 8. 发送记录（邮件）
   * ====================================================================== */
  function emailTabBtn(id, label, selected) {
    return (
      '<button class="tab" role="tab" aria-selected="' +
      selected +
      '" data-action="email-tab" data-tab="' +
      id +
      '">' +
      esc(label) +
      "</button>"
    );
  }

  async function loadEmails() {
    var f = state.emailFilters;
    if (f.dateFrom && f.dateTo && f.dateFrom > f.dateTo) {
      state.emailError = "开始日期晚于结束日期";
      state.emails = [];
      return;
    }
    state.emailError = "";
    state.emailLoading = true;
    try {
      state.emails = await ReportAPI.listEmails(f);
    } catch (err) {
      state.emailError = err.message || String(err);
      state.emails = [];
    } finally {
      state.emailLoading = false;
    }
  }

  async function pageEmailsList() {
    if (!state.emails.length && !state.emailError && !state.emailLoading) {
      await loadEmails();
    }

    var headActions =
      '<button class="btn" data-action="emails-refresh">' +
      icon("refresh") +
      "刷新列表</button>";

    var body;
    if (state.emailLoading) body = loadingRows(5);
    else if (state.emailError)
      body = errorState(
        "查询失败",
        state.emailError,
        '<button class="btn" data-action="emails-reset">清除筛选</button>'
      );
    else body = emailTable();

    return {
      title: "发送记录",
      crumb: "SMTP 已发送邮件 · 快照存档",
      html:
        '<div class="page stack-6">' +
        pipelineBanner() +
        '<div class="page-head">' +
        '<div class="page-head__main"><h2>发送记录</h2>' +
        '<p class="page-head__desc">每次通过 SMTP 成功发送的日报邮件都会在此快照存档，可回看当时的主题、收件人与正文内容。</p></div>' +
        '<div class="page-head__actions">' +
        headActions +
        "</div></div>" +
        '<div class="card">' +
        '<div class="toolbar" role="search">' +
        '<div class="field"><label class="field__label" for="ef-from">开始日期</label>' +
        '<input class="input" type="date" id="ef-from" data-email-filter="dateFrom" value="' +
        esc(state.emailFilters.dateFrom) +
        '"/></div>' +
        '<div class="field"><label class="field__label" for="ef-to">结束日期</label>' +
        '<input class="input" type="date" id="ef-to" data-email-filter="dateTo" value="' +
        esc(state.emailFilters.dateTo) +
        '"/></div>' +
        '<div class="toolbar__spacer"></div>' +
        '<button class="btn btn--ghost" data-action="emails-reset">清除筛选</button>' +
        "</div>" +
        body +
        "</div></div>",
    };
  }

  function emailTable() {
    if (!state.emails.length) {
      return emptyState(
        "暂无发送记录",
        "还没有通过邮件推送过日报。在「日报历史」里打开某日报，点击「推送给 Leader」，发送成功后这里会出现记录。"
      );
    }

    var rows = state.emails
      .map(function (e) {
        return (
          "<tr>" +
          '<td data-label="发送时间"><span class="num">' +
          esc(fmtDateTime(e.sent_at)) +
          "</span></td>" +
          '<td data-label="主题">' +
          esc(e.subject) +
          "</td>" +
          '<td data-label="日报日期"><span class="num">' +
          esc(e.report_date) +
          "</span></td>" +
          '<td data-label="收件人">' +
          esc((e.recipients || []).join(", ") || "—") +
          "</td>" +
          '<td data-label="状态">' +
          (e.status === "sent" ? badge("已发送", "ok") : badge(e.status, "warn")) +
          "</td>" +
          '<td class="col-actions" data-label="操作">' +
          '<button class="row-action" title="查看内容" data-action="open-email" data-id="' +
          esc(e.id) +
          '">' +
          icon("eye", 17) +
          "</button></td></tr>"
        );
      })
      .join("");

    return (
      '<div class="table-wrap"><table class="table table--stack"><thead><tr>' +
      "<th>发送时间</th><th>主题</th><th>日报日期</th><th>收件人</th><th>状态</th>" +
      '<th class="col-actions">操作</th>' +
      "</tr></thead><tbody>" +
      rows +
      "</tbody></table></div>"
    );
  }

  async function pageEmailDetail(id) {
    var email = await ReportAPI.getEmail(id);
    state.emailDetail = email;

    var actions =
      '<a class="btn btn--ghost btn--sm" href="#/emails">' +
      icon("back", 16) +
      "返回发送记录</a>" +
      (email.report_id
        ? '<a class="btn btn--primary btn--sm" href="#/reports/' +
          esc(email.report_id) +
          '">' +
          icon("eye", 16) +
          "查看对应日报</a>"
        : "");

    return {
      title: "邮件详情",
      crumb: email.report_date + " · " + (email.subject || "无主题"),
      html:
        '<div class="page stack-6">' +
        '<div class="page-head"><div class="page-head__main">' +
        '<div class="flex-row" style="gap:8px;align-self:flex-start;margin-bottom:4px">' +
        actions +
        "</div>" +
        "<h2>" +
        esc(email.subject || "（无主题）") +
        "</h2>" +
        '<p class="page-head__desc">发送邮件正文快照 — 主题 / 发件人 / 收件人 / 发送时间。</p>' +
        '<div class="flex-row" style="gap:12px">' +
        (email.status === "sent" ? badge("已发送", "ok") : badge(email.status, "warn")) +
        '<span class="muted" style="font-size:13px">发送时间 ' +
        esc(fmtDateTime(email.sent_at)) +
        "</span></div></div></div>" +
        '<div class="card">' +
        '<div class="card__body stack-6" style="border-bottom:1px solid var(--border)">' +
        '<div class="form-grid">' +
        '<div class="field"><label class="field__label">发件人</label><div class="readonly-note">' +
        esc(email.from_addr || "—") +
        "</div></div>" +
        '<div class="field span-2"><label class="field__label">收件人</label><div class="readonly-note">' +
        esc((email.recipients || []).join(", ") || "—") +
        "</div></div>" +
        "</div></div>" +
        '<div class="tabs" role="tablist">' +
        emailTabBtn("preview", "邮件 HTML", state.emailTab === "preview") +
        emailTabBtn("markdown", "Markdown", state.emailTab === "markdown") +
        "</div>" +
        emailPanel(email) +
        "</div></div>",
    };
  }

  function emailPanel(email) {
    if (state.emailTab === "markdown") {
      return '<pre class="code-block" aria-label="Markdown 源码">' + esc(email.markdown || "") + "</pre>";
    }
    return (
      '<div class="card__body card__body--flush"><iframe class="preview-frame" title="邮件 HTML 预览" sandbox="" srcdoc="' +
      esc(email.html || "") +
      '"></iframe></div>'
    );
  }

  /* ====================================================================== *
   * 9. 成员
   * ====================================================================== */
  async function ensureMembers() {
    var data = await ReportAPI.listMembers();
    state.members = data.members || [];
    if (data.team_name) state.teamName = data.team_name;
  }

  async function pageMembersList() {
    await ensureMembers();
    var rows = state.members
      .map(function (m) {
        var keys = identityKeys(m);
        return (
          "<tr>" +
          '<td data-label="姓名"><span class="flex-row" style="gap:10px"><span class="avatar" aria-hidden="true">' +
          esc(initial(m.name)) +
          "</span>" +
          esc(m.name) +
          "</span></td>" +
          '<td data-label="提交账号"><span class="num">@' +
          esc(m.github) +
          "</span></td>" +
          '<td data-label="飞书账号"><span class="num">' +
          esc(m.lark) +
          "</span></td>" +
          '<td data-label="飞书 Open ID"><span class="num">' +
          esc(dash(m.lark_open_id)) +
          "</span></td>" +
          '<td class="num" data-label="对齐键数">' +
          keys.length +
          "</td>" +
          '<td class="col-actions" data-label="操作">' +
          '<button class="row-action" title="编辑" data-action="member-edit" data-id="' +
          esc(m.id) +
          '">' +
          icon("edit", 17) +
          '</button><button class="row-action row-action--danger" title="删除" data-action="member-delete" data-id="' +
          esc(m.id) +
          '">' +
          icon("trash", 17) +
          "</button></td></tr>"
        );
      })
      .join("");

    var table = state.members.length
      ? '<div class="table-wrap"><table class="table table--stack"><thead><tr>' +
        "<th>姓名</th><th>提交账号</th><th>飞书账号</th><th>飞书 Open ID</th>" +
        '<th class="num">对齐键数</th><th class="col-actions">操作</th>' +
        "</tr></thead><tbody>" +
        rows +
        "</tbody></table></div>"
      : emptyState(
          "暂无成员",
          "管道按「成员」聚合工作数据。请先配置姓名与各平台账号的对应关系。",
          '<a class="btn btn--primary" href="#/members/new">' + icon("plus") + "新增成员</a>"
        );

    return {
      title: "成员映射",
      crumb: "聚合键 · " + state.members.length + " 人",
      html:
        '<div class="page stack-6"><div class="page-head"><div class="page-head__main">' +
        "<h2>成员映射</h2><p class=\"page-head__desc\">把 GitHub 登录名、飞书账号等映射到真实姓名，管道采集后才能按人拼出日报段落。保存写入 config.yaml 的 members。</p>" +
        '</div><div class="page-head__actions"><a class="btn btn--primary" href="#/members/new">' +
        icon("plus") +
        "新增成员</a></div></div><div class=\"card\">" +
        table +
        "</div></div>",
    };
  }

  async function pageMemberForm(id) {
    await ensureMembers();
    var editing = !!id;
    var m = editing
      ? state.members.find(function (x) {
          return x.id === id;
        })
      : null;
    if (editing && !m) return notFoundView("未找到该成员", "成员 ID " + id + " 不存在。");
    var v = m || { name: "", github: "", lark: "", lark_open_id: "", lark_user_id: "" };

    function field(key, label, required, type, hint, placeholder) {
      return (
        '<div class="field" data-field="' +
        key +
        '"><label class="field__label" for="m-' +
        key +
        '">' +
        esc(label) +
        (required ? '<span class="req">*</span>' : "") +
        '</label><input class="input" id="m-' +
        key +
        '" name="' +
        key +
        '" type="' +
        (type || "text") +
        '" value="' +
        esc(v[key] || "") +
        '" placeholder="' +
        esc(placeholder || "") +
        '"' +
        (required ? ' aria-required="true"' : "") +
        '/><span class="field__hint">' +
        esc(hint) +
        '</span><span class="field__error" role="alert"></span></div>'
      );
    }

    return {
      title: editing ? "编辑成员" : "新增成员",
      crumb: editing ? v.name : "创建",
      html:
        '<div class="page stack-6"><div class="page-head"><div class="page-head__main">' +
        '<a class="btn btn--ghost btn--sm" href="#/members" style="align-self:flex-start;margin-bottom:4px">' +
        icon("back", 16) +
        "返回成员列表</a><h2>" +
        (editing ? "编辑成员" : "新增成员") +
        '</h2></div></div><form class="card" id="member-form" novalidate>' +
        '<div class="card__body stack-6"><div class="form-grid">' +
        '<div class="span-2">' +
        field("name", "姓名", true, "text", "日报里该成员分区的标题。", "张三") +
        "</div>" +
        field("github", "GitHub 用户名", true, "text", "用来对齐 commit 作者登录名（不是仓库地址）。", "zhangsan") +
        field("lark", "飞书账号（邮箱）", true, "email", "用来对齐任务负责人。", "zhangsan@company.com") +
        field("lark_open_id", "飞书 Open ID", false, "text", "可选，多键对齐时使用。", "ou_xxx") +
        field("lark_user_id", "飞书 User ID", false, "text", "可选，多键对齐时使用。", "uid_xxx") +
        '</div></div><div class="card__head" style="border-top:1px solid var(--border);border-bottom:0;justify-content:flex-end">' +
        '<a class="btn" href="#/members">取消</a>' +
        '<button class="btn btn--primary" type="submit" data-action="member-save" data-id="' +
        esc(id || "") +
        '">' +
        icon("check") +
        (editing ? "保存修改" : "创建成员") +
        "</button></div></form></div>",
      mount: function () {
        var form = document.getElementById("member-form");
        if (!form) return;
        form.addEventListener("submit", function (e) {
          e.preventDefault();
          saveMember(id);
        });
      },
    };
  }

  async function saveMember(id) {
    var form = document.getElementById("member-form");
    if (!form) return;
    var data = {
      name: form.name.value.trim(),
      github: form.github.value.trim(),
      lark: form.lark.value.trim(),
      lark_open_id: form.lark_open_id.value.trim(),
      lark_user_id: form.lark_user_id.value.trim(),
    };
    if (!data.name || !data.github || !data.lark) {
      toast("error", "校验失败", "姓名、GitHub、飞书账号为必填。");
      return;
    }

    var btn = form.querySelector('[data-action="member-save"]');
    var original = btn.innerHTML;
    btn.setAttribute("data-loading", "true");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>保存中';

    try {
      await ensureMembers();
      var next = state.members.map(function (m) {
        return {
          id: m.id,
          name: m.name,
          github: m.github,
          lark: m.lark,
          lark_open_id: m.lark_open_id || "",
          lark_user_id: m.lark_user_id || "",
        };
      });
      if (id) {
        next = next.map(function (m) {
          return m.id === id ? Object.assign({}, m, data) : m;
        });
      } else {
        next.push(Object.assign({ id: "m-new" }, data));
      }
      var resp = await ReportAPI.saveMembers(next);
      toast("ok", "成员已保存", resp.message || data.name);
      state.members = [];
      navigate("members");
    } catch (err) {
      toast("error", "保存失败", err.message || String(err));
    } finally {
      btn.innerHTML = original;
      btn.removeAttribute("data-loading");
      btn.disabled = false;
    }
  }

  function confirmDeleteMember(id) {
    var m = state.members.find(function (x) {
      return x.id === id;
    });
    if (!m) return;
    openModal({
      title: "删除成员",
      tone: "danger",
      body:
        "确定删除成员 <b>" +
        esc(m.name) +
        "</b>（@" +
        esc(m.github) +
        "）吗？此操作会写入 config.yaml。",
      confirmLabel: "删除",
      onConfirm: async function () {
        try {
          var next = state.members
            .filter(function (x) {
              return x.id !== id;
            })
            .map(function (x) {
              return {
                name: x.name,
                github: x.github,
                lark: x.lark,
                lark_open_id: x.lark_open_id || "",
                lark_user_id: x.lark_user_id || "",
              };
            });
          await ReportAPI.saveMembers(next);
          closeModal();
          toast("ok", "成员已删除", m.name);
          state.members = [];
          routeTo();
        } catch (err) {
          toast("error", "删除失败", err.message || String(err));
        }
      },
    });
  }

  /* ====================================================================== *
   * 10. 设置
   * ====================================================================== */
  async function pageSettings() {
    var s = await ReportAPI.getConfig();
    state.settings = s;
    state.teamName = s.team_name || state.teamName;
    s.schedule = s.schedule || {};
    s.github = s.github || {};
    s.lark = s.lark || {};
    s.email = s.email || {};
    s.storage = s.storage || {};

    function section(id, title, desc, body) {
      return (
        '<section class="settings-section" aria-labelledby="sh-' +
        id +
        '"><div class="settings-section__head"><h2 id="sh-' +
        id +
        '">' +
        esc(title) +
        "</h2><p>" +
        esc(desc) +
        '</p></div><div class="card__body">' +
        body +
        "</div></section>"
      );
    }

    function input(id, label, value, type, hint) {
      return (
        '<div class="field"><label class="field__label" for="s-' +
        id +
        '">' +
        esc(label) +
        '</label><input class="input" id="s-' +
        id +
        '" name="' +
        id +
        '" type="' +
        (type || "text") +
        '" value="' +
        esc(value == null ? "" : value) +
        '"/>' +
        (hint ? '<span class="field__hint">' + esc(hint) + "</span>" : "") +
        "</div>"
      );
    }

    function textarea(id, label, value, hint) {
      return (
        '<div class="field span-2"><label class="field__label" for="s-' +
        id +
        '">' +
        esc(label) +
        '</label><textarea class="textarea" id="s-' +
        id +
        '" name="' +
        id +
        '" spellcheck="false">' +
        esc(value || "") +
        "</textarea>" +
        (hint ? '<span class="field__hint">' + esc(hint) + "</span>" : "") +
        "</div>"
      );
    }

    return {
      title: "采集与推送",
      crumb: "config.yaml · 管道参数",
      html:
        '<div class="page stack-6">' +
        pipelineBanner() +
        '<div class="page-head"><div class="page-head__main">' +
        "<h2>采集与推送</h2><p class=\"page-head__desc\">配置日报管道：从哪里采集「工作痕迹」、何时跑、发给谁。密钥只写环境变量名，不在此录入明文。</p>" +
        '</div><div class="page-head__actions">' +
        '<button class="btn" data-action="settings-health">' +
        icon("checkCircle") +
        "检查连通性</button>" +
        '<button class="btn btn--primary" data-action="settings-save">' +
        icon("check") +
        "保存配置</button></div></div>" +
        '<form class="card" id="settings-form" novalidate>' +
        section(
          "token",
          "配置令牌",
          "对应服务端 CONFIG_TOKEN，保护写配置接口。",
          '<div class="form-grid">' +
            input("config_token", "X-Config-Token", state.tokenDraft, "text", "仅保存在本机浏览器。") +
            "</div>"
        ) +
        section(
          "team",
          "团队",
          "写入日报标题，例如「研发一组 日报 — 日期」。",
          '<div class="form-grid">' + input("team_name", "团队名称", s.team_name, "text") + "</div>"
        ) +
        section(
          "schedule",
          "排期（cron 语义）",
          "工作日定时触发；周末/节假日默认跳过（可用 --force 强制）。",
          '<div class="form-grid">' +
            input("hour", "小时（0–23）", s.schedule.hour != null ? s.schedule.hour : 18, "number") +
            input("minute", "分钟（0–59）", s.schedule.minute != null ? s.schedule.minute : 0, "number") +
            '<div class="field span-2"><label class="check"><input type="checkbox" name="skip_weekends"' +
            (s.schedule.skip_weekends !== false ? " checked" : "") +
            "/>跳过周末</label></div>" +
            textarea(
              "holidays",
              "节假日（每行一个，YYYY-MM-DD）",
              (s.schedule.holidays || []).join("\n")
            ) +
            "</div>"
        ) +
        section(
          "github",
          "代码提交采集",
          "通过 GitHub Commits API 采集这些仓库当日的提交记录：作者、commit message、增删行数、改动文件数。按设计不把 diff/源码写进日报。",
          '<div class="form-grid">' +
            input("token_env", "Token 环境变量名", s.github.token_env || "GITHUB_TOKEN", "text") +
            textarea(
              "repos",
              "监听仓库（每行一个 owner/repo）",
              (s.github.repos || []).join("\n"),
              "管道只会查询这些仓库在当日窗口内的提交记录。"
            ) +
            "</div>"
        ) +
        section(
          "lark",
          "任务进展采集",
          "从飞书任务清单拉取当日状态变更（新建 / 进行中 / 已完成等）。",
          '<div class="form-grid">' +
            input("app_id_env", "App ID 环境变量名", s.lark.app_id_env || "LARK_APP_ID", "text") +
            input(
              "app_secret_env",
              "App Secret 环境变量名",
              s.lark.app_secret_env || "LARK_APP_SECRET",
              "text"
            ) +
            '<div class="span-2">' +
            input(
              "tasklist_id",
              "任务清单 GUID",
              s.lark.tasklist_id || "",
              "text",
              "飞书任务 URL 中 task-lists/ 后面的那段。"
            ) +
            "</div></div>"
        ) +
        section(
          "email",
          "邮件推送（Notifier）",
          "把生成好的 HTML 日报通过 SMTP 发给 Leader；dry-run 模式会跳过本步。",
          '<div class="form-grid">' +
            input("smtp_host", "SMTP 主机", s.email.smtp_host || "", "text") +
            input("smtp_port", "SMTP 端口", s.email.smtp_port || 465, "number") +
            '<div class="span-2"><label class="check"><input type="checkbox" name="use_ssl"' +
            (s.email.use_ssl !== false ? " checked" : "") +
            "/>使用 SSL</label></div>" +
            input("username_env", "用户名环境变量名", s.email.username_env || "SMTP_USERNAME", "text") +
            input("password_env", "密码环境变量名", s.email.password_env || "SMTP_PASSWORD", "text") +
            input("from_addr", "发件地址", s.email.from_addr || "", "email") +
            textarea("recipients", "收件人（每行一个）", (s.email.recipients || []).join("\n")) +
            "</div>"
        ) +
        section(
          "storage",
          "本地存档",
          "管道生成的日报写入 SQLite，供本工作台浏览历史。",
          '<div class="form-grid"><div class="span-2"><div class="field"><label class="field__label" for="s-db_path">数据库路径</label>' +
            '<input class="input" id="s-db_path" name="db_path" type="text" value="' +
            esc(s.storage.db_path || "data/daily_reports.db") +
            '" readonly/><span class="readonly-note">' +
            icon("lock", 14) +
            "只读</span></div></div></div>"
        ) +
        '<div class="card__head" style="border-top:1px solid var(--border);border-bottom:0;justify-content:flex-end">' +
        '<button class="btn btn--primary" type="submit" data-action="settings-save">' +
        icon("check") +
        "保存配置</button></div></form></div>",
      mount: function () {
        var form = document.getElementById("settings-form");
        if (!form) return;
        form.addEventListener("submit", function (e) {
          e.preventDefault();
          saveSettings();
        });
      },
    };
  }

  async function saveSettings() {
    var f = document.getElementById("settings-form");
    if (!f) return;
    ReportAPI.setConfigToken(f.config_token.value.trim() || "local-demo-token");
    state.tokenDraft = ReportAPI.getConfigToken();

    var hour = parseInt(f.hour.value, 10);
    var minute = parseInt(f.minute.value, 10);
    var port = parseInt(f.smtp_port.value, 10);
    if (isNaN(hour) || hour < 0 || hour > 23) {
      toast("error", "保存失败", "小时需为 0–23");
      return;
    }
    if (isNaN(minute) || minute < 0 || minute > 59) {
      toast("error", "保存失败", "分钟需为 0–59");
      return;
    }
    if (isNaN(port) || port < 1 || port > 65535) {
      toast("error", "保存失败", "SMTP 端口无效");
      return;
    }

    var repos = f.repos.value
      .split("\n")
      .map(function (x) {
        return x.trim();
      })
      .filter(Boolean);
    if (!repos.length) {
      toast("error", "保存失败", "至少配置一个监听仓库（owner/repo）");
      return;
    }
    var recipients = f.recipients.value
      .split("\n")
      .map(function (x) {
        return x.trim();
      })
      .filter(Boolean);
    if (!recipients.length) {
      toast("error", "保存失败", "至少需要一名收件人");
      return;
    }

    var holidays = f.holidays.value
      .split("\n")
      .map(function (x) {
        return x.trim();
      })
      .filter(Boolean);

    // 保留现有 members，避免设置页覆盖成员
    var current = state.settings || (await ReportAPI.getConfig());
    var body = {
      team_name: f.team_name.value.trim(),
      schedule: {
        hour: hour,
        minute: minute,
        skip_weekends: f.skip_weekends.checked,
        holidays: holidays,
      },
      github: { token_env: f.token_env.value.trim(), repos: repos },
      lark: {
        app_id_env: f.app_id_env.value.trim(),
        app_secret_env: f.app_secret_env.value.trim(),
        tasklist_id: f.tasklist_id.value.trim(),
      },
      email: {
        smtp_host: f.smtp_host.value.trim(),
        smtp_port: port,
        use_ssl: f.use_ssl.checked,
        username_env: f.username_env.value.trim(),
        password_env: f.password_env.value.trim(),
        from_addr: f.from_addr.value.trim(),
        recipients: recipients,
      },
      storage: { db_path: f.db_path.value.trim() },
      members: current.members || [],
    };

    try {
      var resp = await ReportAPI.saveConfig(body);
      state.settings = body;
      state.teamName = body.team_name;
      toast("ok", "配置已保存", resp.message || "");
    } catch (err) {
      toast("error", "保存失败", err.message || String(err));
    }
  }

  /* ====================================================================== *
   * 11. 运行记录（由日报历史派生展示）
   * ====================================================================== */
  async function pageRuns() {
    if (!state.reports.length) {
      try {
        state.reports = await ReportAPI.listReports({ limit: 50 });
      } catch (_) {
        state.reports = [];
      }
    }
    var list = state.reports.slice(0, 20);
    var rows = list
      .map(function (r) {
        var id = "run-" + r.id;
        var open = !!state.runExpanded[id];
        var main =
          '<tr class="run-row" data-action="run-toggle" data-id="' +
          esc(id) +
          '">' +
          '<td data-label="运行"><span class="flex-row" style="gap:8px">' +
          icon("chevron", 14) +
          '<span class="num">' +
          esc(id) +
          "</span></span></td>" +
          '<td data-label="开始时间"><span class="num">' +
          esc(fmtDateTime(r.generated_at)) +
          "</span></td>" +
          '<td data-label="触发"><span class="badge badge--plain badge--mute">历史</span></td>' +
          '<td data-label="耗时"><span class="num">—</span></td>' +
          '<td data-label="提交采集">' +
          (r.status === "ok" ? badge("正常", "ok") : badge("降级", "warn")) +
          "</td>" +
          '<td data-label="任务采集">' +
          (r.status === "ok" ? badge("正常", "ok") : badge("降级", "warn")) +
          "</td>" +
          '<td data-label="邮件">' +
          badge("见日志", "mute") +
          "</td>" +
          '<td data-label="结果">' +
          (r.status === "ok" ? badge("成功", "ok") : badge("降级", "warn")) +
          "</td></tr>";
        if (!open) return main;
        return (
          main +
          '<tr><td colspan="8"><div class="run-log">' +
          "[pipeline] report id=" +
          esc(r.id) +
          " date=" +
          esc(r.report_date) +
          " team=" +
          esc(r.team_name) +
          "\n[collect] commits=" +
          r.commits_count +
          " tasks=" +
          r.tasks_count +
          " members=" +
          r.member_count +
          "\n[result] status=" +
          esc(r.status) +
          " （降级=某一数据源失败但仍生成了日报）" +
          "</div></td></tr>"
        );
      })
      .join("");

    var table = list.length
      ? '<div class="table-wrap"><table class="table table--stack"><thead><tr>' +
        "<th>运行</th><th>开始时间</th><th>触发</th><th>耗时</th><th>提交采集</th><th>任务采集</th><th>邮件</th><th>结果</th>" +
        "</tr></thead><tbody>" +
        rows +
        "</tbody></table></div>"
      : emptyState("暂无管道运行摘要", "先生成一份日报后，这里会按存档展示每次管道结果。");

    return {
      title: "管道运行",
      crumb: "采集 → 生成 → 推送",
      html:
        '<div class="page stack-6">' +
        pipelineBanner() +
        '<div class="page-head"><div class="page-head__main">' +
        "<h2>管道运行</h2><p class=\"page-head__desc\">后端主入口是一次性任务（main.py / cron），不会常驻。此处根据已存日报还原最近几次管道结果摘要。</p>" +
        '</div><div class="page-head__actions"><button class="btn btn--primary" data-action="generate">' +
        icon("refresh") +
        "生成今日日报</button></div></div><div class=\"card\">" +
        table +
        "</div></div>",
    };
  }

  /* ====================================================================== *
   * 12. 操作
   * ====================================================================== */
  async function doGenerate() {
    toast(
      "info",
      "管道执行中",
      "采集提交记录与任务变更 → 按成员聚合 → 生成日报 → 写入 SQLite（dry-run，不发信）"
    );
    try {
      var resp = await ReportAPI.generate({ dry_run: true, force: true });
      toast("ok", "日报已生成", resp.message || "已写入本地存档，可在「日报历史」查看");
      state.reports = [];
      await loadReports();
      routeTo();
    } catch (err) {
      toast("error", "管道失败", err.message || String(err));
    }
  }

  async function doHealthCheck() {
    try {
      var h = await ReportAPI.health();
      var side = document.getElementById("sideStatus");
      if (side) side.textContent = h.ok ? "配置可读" : "配置异常";
      var db = document.getElementById("sideDb");
      if (db && h.db) {
        var parts = String(h.db).replace(/\\/g, "/").split("/");
        db.textContent = parts.slice(-2).join("/");
      }
      var src = (h.sources || [])
        .map(function (s) {
          var label =
            s.id === "github"
              ? "提交采集"
              : s.id === "lark_task"
                ? "任务采集"
                : s.id === "lark_msg"
                  ? "群消息(未启用)"
                  : s.id;
          return label + (s.ok ? "✓" : "✗");
        })
        .join(" · ");
      toast(h.ok ? "ok" : "error", "连通性检查", src || (h.ok ? "正常" : "异常"));
    } catch (err) {
      toast("error", "检查失败", err.message || String(err));
    }
  }

  async function doSend(id) {
    try {
      var resp = await ReportAPI.sendReport(id);
      toast("ok", "已推送", resp.message || "邮件已发送给配置的收件人");
      state.reports = [];
      state.emails = [];
      routeTo();
    } catch (err) {
      toast("error", "推送失败", err.message || String(err));
    }
  }

  /* ====================================================================== *
   * 13. 事件委托
   * ====================================================================== */
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-action]");
    if (!t) return;
    var action = t.getAttribute("data-action");

    switch (action) {
      case "list-refresh":
        state.reports = [];
        state.listLoading = true;
        routeTo().then(function () {
          return loadReports().then(routeTo);
        });
        break;
      case "list-reset":
        state.filters = { dateFrom: "", dateTo: "", team: "全部", order: "desc" };
        state.page = 1;
        state.listError = "";
        state.reports = [];
        state.listLoading = true;
        routeTo().then(function () {
          return loadReports().then(routeTo);
        });
        break;
      case "emails-refresh":
        state.emails = [];
        state.emailLoading = true;
        routeTo().then(function () {
          return loadEmails().then(routeTo);
        });
        break;
      case "emails-reset":
        state.emailFilters = { dateFrom: "", dateTo: "" };
        state.emailError = "";
        state.emails = [];
        state.emailLoading = true;
        routeTo().then(function () {
          return loadEmails().then(routeTo);
        });
        break;
      case "list-sort":
        state.filters.order = state.filters.order === "asc" ? "desc" : "asc";
        routeTo();
        break;
      case "page-prev":
        if (state.page > 1) {
          state.page--;
          routeTo();
        }
        break;
      case "page-next":
        state.page++;
        routeTo();
        break;
      case "open-report":
        navigate("reports/" + t.getAttribute("data-id"));
        break;
      case "open-email":
        navigate("emails/" + t.getAttribute("data-id"));
        break;
      case "detail-tab":
        state.detailTab = t.getAttribute("data-tab");
        routeTo();
        break;
      case "email-tab":
        state.emailTab = t.getAttribute("data-tab");
        routeTo();
        break;
      case "export-md":
        if (state.detail)
          download(
            "report-" + state.detail.report_date + ".md",
            state.detail.markdown,
            "text/markdown;charset=utf-8"
          );
        break;
      case "export-html":
        if (state.detail)
          download(
            "report-" + state.detail.report_date + ".html",
            state.detail.html,
            "text/html;charset=utf-8"
          );
        break;
      case "report-send":
        doSend(t.getAttribute("data-id"));
        break;
      case "report-regenerate":
      case "generate":
        doGenerate();
        break;
      case "settings-health":
        doHealthCheck();
        break;
      case "settings-save": {
        var f = document.getElementById("settings-form");
        if (f) f.dispatchEvent(new Event("submit", { cancelable: true }));
        break;
      }
      case "member-edit":
        navigate("members/" + t.getAttribute("data-id") + "/edit");
        break;
      case "member-delete":
        confirmDeleteMember(t.getAttribute("data-id"));
        break;
      case "run-toggle": {
        var id = t.getAttribute("data-id");
        state.runExpanded[id] = !state.runExpanded[id];
        routeTo();
        break;
      }
      case "toast-close":
        dismissToast(t.closest(".toast"));
        break;
      case "modal-close":
        closeModal();
        break;
      case "modal-confirm":
        if (modalState.onConfirm) modalState.onConfirm();
        break;
      default:
        break;
    }
  });

  document.addEventListener("change", function (e) {
    var el = e.target.closest("[data-filter]");
    if (!el) return;
    state.filters[el.getAttribute("data-filter")] = el.value;
    state.page = 1;
    state.reports = [];
    state.listLoading = true;
    routeTo().then(function () {
      return loadReports().then(routeTo);
    });
  });

  document.addEventListener("change", function (e) {
    var el = e.target.closest("[data-email-filter]");
    if (!el) return;
    state.emailFilters[el.getAttribute("data-email-filter")] = el.value;
    state.emails = [];
    state.emailLoading = true;
    routeTo().then(function () {
      return loadEmails().then(routeTo);
    });
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && document.getElementById("modalRoot").innerHTML) closeModal();
  });

  /* ====================================================================== *
   * 14. 启动
   * ====================================================================== */
  async function boot() {
    if (!location.hash) location.hash = "#/reports";
    renderTopActions();
    doHealthCheck();
    window.addEventListener("hashchange", routeTo);
    await routeTo();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
