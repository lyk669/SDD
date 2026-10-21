/**
 * api.js — 与后端 FastAPI 通信的薄封装
 *
 * 约定：
 *   - 同源访问 /api/*
 *   - 配置写操作需在 localStorage 写入 configToken（对应服务端 CONFIG_TOKEN）
 */
(function (global) {
  "use strict";

  var BASE = "/api";

  function getConfigToken() {
    return localStorage.getItem("configToken") || "local-demo-token";
  }

  function setConfigToken(token) {
    localStorage.setItem("configToken", token || "local-demo-token");
  }

  async function request(path, options) {
    options = options || {};
    var headers = Object.assign(
      { Accept: "application/json" },
      options.headers || {}
    );
    if (options.body && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json; charset=utf-8";
    }

    var res = await fetch(BASE + path, {
      method: options.method || "GET",
      headers: headers,
      body: options.body ? JSON.stringify(options.body) : undefined,
    });

    var text = await res.text();
    var data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (_) {
        data = { detail: text };
      }
    }

    if (!res.ok) {
      var msg =
        (data && (data.detail || data.message)) ||
        "请求失败 (" + res.status + ")";
      if (Array.isArray(msg)) {
        msg = msg
          .map(function (x) {
            return x.msg || JSON.stringify(x);
          })
          .join("; ");
      }
      var err = new Error(String(msg));
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function qs(params) {
    var parts = [];
    Object.keys(params || {}).forEach(function (k) {
      var v = params[k];
      if (v === undefined || v === null || v === "") return;
      parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
    });
    return parts.length ? "?" + parts.join("&") : "";
  }

  global.ReportAPI = {
    getConfigToken: getConfigToken,
    setConfigToken: setConfigToken,

    health: function () {
      return request("/health");
    },

    listReports: function (filters) {
      filters = filters || {};
      return request(
        "/reports" +
          qs({
            limit: filters.limit || 100,
            date_from: filters.dateFrom,
            date_to: filters.dateTo,
            team: filters.team === "全部" ? "" : filters.team,
          })
      );
    },

    getReport: function (id) {
      return request("/reports/" + encodeURIComponent(id));
    },

    listMembers: function () {
      return request("/members");
    },

    saveMembers: function (members) {
      return request("/members", {
        method: "PUT",
        body: members,
        headers: { "X-Config-Token": getConfigToken() },
      });
    },

    getConfig: function () {
      return request("/config");
    },

    saveConfig: function (config) {
      return request("/config", {
        method: "PUT",
        body: config,
        headers: { "X-Config-Token": getConfigToken() },
      });
    },

    generate: function (opts) {
      return request("/generate", {
        method: "POST",
        body: opts || { dry_run: true, force: true },
      });
    },

    sendReport: function (id) {
      return request("/reports/" + encodeURIComponent(id) + "/send", {
        method: "POST",
      });
    },

    listEmails: function (filters) {
      filters = filters || {};
      return request(
        "/emails" +
          qs({
            limit: filters.limit || 100,
            date_from: filters.dateFrom,
            date_to: filters.dateTo,
          })
      );
    },

    getEmail: function (id) {
      return request("/emails/" + encodeURIComponent(id));
    },

    getReportAnalysis: function (id) {
      return request("/reports/" + encodeURIComponent(id) + "/analysis");
    },

    listReflections: function (opts) {
      opts = opts || {};
      return request("/reflections" + qs({ limit: opts.limit || 30 }));
    },

    getReflection: function (date) {
      return request("/reflections/" + encodeURIComponent(date));
    },
  };
})(window);
