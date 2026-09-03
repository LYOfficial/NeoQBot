(function () {
  "use strict";

  var state = {
    csrf: "",
    username: "",
    role: "operator",
    activeView: "dashboard",
    config: null,
    settingsRevision: "",
    qqUrls: {},
    qqLoginBotId: "",
    qqLoginTimer: null,
    qqLoginPolling: false,
    qqQrRefreshing: false,
    qqQrLastRefresh: 0,
    settingsStep: "llm",
    settingsDirty: false,
    orchestrationNodes: [],
    orchestrationEdges: [],
    orchestrationSelected: "",
    orchestrationDirty: false,
    orchestrationMenuPoint: { x: 120, y: 120 },
    orchestrationDrag: null,
    orchestrationConnect: null,
    orchestrationSearch: "",
    orchestrationStatuses: { qq: [], feishu: [] },
    orchestrationSensitiveEditsAllowed: false,
    orchestrationIdentityChanges: { qq_created: [], qq_deleted: [], feishu_created: [], feishu_deleted: [] },
    orchestrationContextNodeId: "",
    nodeDialog: null,
    groupWorkspaceNodeId: "",
    groupWorkspaceKind: "messages",
    groupWorkspaceOffset: 0,
    groupWorkspaceHasMore: false,
    groupWorkspaceSearchTimer: null,
    recordsOffset: 0,
    recordsHasMore: false,
    recordsSearchTimer: null,
    commandIndex: 0
  };

  var viewPaths = {
    dashboard: "/dashboard",
    integrations: "/orchestration",
    settings: "/settings",
    records: "/records",
    users: "/user"
  };

  function $(id) { return document.getElementById(id); }
  function clone(value) { return value === undefined ? undefined : JSON.parse(JSON.stringify(value)); }
  function listValue(value) { return (value || []).join("\n"); }
  function splitList(value) {
    return String(value || "").split(/[\n,]/).map(function (item) { return item.trim(); }).filter(Boolean);
  }
  function numberValue(value, fallback) {
    var parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }
  function pretty(value) { return JSON.stringify(value || {}, null, 2); }
  function parseJsonText(value, label) {
    try { return JSON.parse(value || "{}"); }
    catch (_) { throw new Error(label + "不是有效 JSON"); }
  }
  function errorText(detail) {
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map(function (item) { return item.msg || JSON.stringify(item); }).join("；");
    return JSON.stringify(detail || "请求失败");
  }

  function showToast(message, isError) {
    var toast = $("toast");
    toast.textContent = message;
    toast.className = "toast show" + (isError ? " error" : "");
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(function () { toast.className = "toast"; }, 3200);
  }

  var confirmResolver = null;

  function settleConfirm(value) {
    if (!confirmResolver) return;
    var resolve = confirmResolver;
    confirmResolver = null;
    if ($("confirm-dialog").open) $("confirm-dialog").close();
    resolve(value);
  }

  function confirmAction(options) {
    options = options || {};
    if (confirmResolver) settleConfirm(false);
    $("confirm-title").textContent = options.title || "确认操作";
    $("confirm-kicker").textContent = options.kicker || "Confirm action";
    $("confirm-message").textContent = options.message || "是否继续？";
    $("confirm-icon").textContent = options.icon || (options.danger ? "!" : "?");
    $("confirm-cancel").textContent = options.cancelText || "取消";
    $("confirm-accept").textContent = options.confirmText || "确认";
    $("confirm-dialog").classList.toggle("danger", Boolean(options.danger));
    $("confirm-dialog").showModal();
    window.setTimeout(function () { $("confirm-accept").focus(); }, 20);
    return new Promise(function (resolve) { confirmResolver = resolve; });
  }

  function showValueDialog(options) {
    options = options || {};
    $("value-title").textContent = options.title || "查看内容";
    $("value-message").textContent = options.message || "请妥善保管以下内容。";
    $("value-content").value = options.value || "";
    $("value-dialog").showModal();
    window.setTimeout(function () { $("value-content").focus(); $("value-content").select(); }, 20);
  }

  async function api(path, options) {
    options = options || {};
    options.headers = options.headers || {};
    if (options.body && typeof options.body !== "string") {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(options.body);
    }
    if (state.csrf && (options.method || "GET") !== "GET") {
      options.headers["X-CSRF-Token"] = state.csrf;
    }
    var response = await fetch(path, options);
    var data = {};
    try { data = await response.json(); } catch (_) { data = {}; }
    if (!response.ok) {
      if (response.status === 401) showLogin();
      var error = new Error(errorText(data.detail || data.error || "请求失败"));
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function commitTheme(theme) {
    var light = theme === "light";
    document.documentElement.dataset.theme = light ? "light" : "dark";
    $("theme-button").setAttribute("aria-checked", light ? "true" : "false");
    $("theme-button").setAttribute("aria-label", light ? "切换深色模式" : "切换浅色模式");
    $("theme-button").title = light ? "切换深色模式" : "切换浅色模式";
    $("theme-label").textContent = light ? "切换深色模式" : "切换浅色模式";
    try { window.localStorage.setItem("neoqbot-theme", light ? "light" : "dark"); } catch (_) {}
  }

  function applyTheme(theme, animate) {
    var reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!animate || reducedMotion) {
      commitTheme(theme);
      return;
    }
    if (typeof document.startViewTransition !== "function") {
      var overlay = $("theme-wipe");
      $("theme-button").disabled = true;
      overlay.className = "theme-wipe-overlay " + (theme === "light" ? "to-light" : "to-dark");
      void overlay.offsetWidth;
      overlay.classList.add("active");
      window.setTimeout(function () { commitTheme(theme); }, 320);
      window.setTimeout(function () {
        overlay.className = "theme-wipe-overlay";
        $("theme-button").disabled = false;
      }, 650);
      return;
    }
    $("theme-button").disabled = true;
    document.documentElement.classList.add("theme-transitioning");
    var transition = document.startViewTransition(function () { commitTheme(theme); });
    transition.finished.finally(function () {
      document.documentElement.classList.remove("theme-transitioning");
      $("theme-button").disabled = false;
    });
  }

  function initializeTheme() {
    var theme = "dark";
    try { theme = window.localStorage.getItem("neoqbot-theme") || "dark"; } catch (_) {}
    applyTheme(theme === "light" ? "light" : "dark", false);
  }

  function hideAllRoots() {
    ["login-view", "password-view", "app-view"].forEach(function (id) { $(id).classList.add("hidden"); });
  }

  function applySession(session) {
    state.csrf = session.csrf_token;
    state.username = session.username;
    state.role = session.role || "operator";
  }

  function renderCurrentUser() {
    var initial = (state.username.charAt(0) || "U").toUpperCase();
    var roleLabel = state.role === "admin" ? "管理员" : "子用户";
    $("current-user").textContent = state.username;
    $("current-user-avatar").textContent = initial;
    $("current-user-role").textContent = roleLabel;
    $("profile-avatar").textContent = initial;
    $("profile-display-name").textContent = state.username;
    $("profile-role").textContent = roleLabel;
    $("profile-username").value = state.username;
    $("users-nav").classList.toggle("hidden", state.role !== "admin");
  }

  function showLogin() {
    state.csrf = "";
    hideAllRoots();
    $("login-view").classList.remove("hidden");
    window.setTimeout(function () { $("login-password").focus(); }, 50);
  }

  function showPasswordChange() {
    hideAllRoots();
    $("password-view").classList.remove("hidden");
    window.setTimeout(function () { $("current-password").focus(); }, 50);
  }

  function viewFromLocation() {
    var pathname = window.location.pathname.replace(/\/+$/, "") || "/dashboard";
    return Object.keys(viewPaths).find(function (name) { return viewPaths[name] === pathname; }) || "dashboard";
  }

  function updateViewLocation(name, replace) {
    var path = viewPaths[name];
    if (!path || window.location.pathname === path) return;
    window.history[replace ? "replaceState" : "pushState"]({ view: name }, "", path);
  }

  async function showApp() {
    hideAllRoots();
    $("app-view").classList.remove("hidden");
    renderCurrentUser();
    await switchView(viewFromLocation(), { skipLocation: true });
  }

  async function initialize() {
    initializeTheme();
    try {
      var session = await api("/api/gui/auth/session");
      applySession(session);
      if (session.must_change_password) showPasswordChange();
      else await showApp();
    } catch (_) {
      showLogin();
    }
  }

  $("login-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    var button = event.submitter;
    button.disabled = true;
    try {
      var result = await api("/api/gui/auth/login", {
        method: "POST",
        body: { username: $("login-username").value.trim(), password: $("login-password").value }
      });
      applySession(result);
      $("login-password").value = "";
      if (result.must_change_password) showPasswordChange();
      else await showApp();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  $("password-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    var current = $("current-password").value;
    var password = $("new-password").value;
    if (password !== $("confirm-password").value) {
      showToast("两次输入的新密码不一致", true);
      return;
    }
    var button = event.submitter;
    button.disabled = true;
    try {
      await api("/api/gui/auth/password", {
        method: "POST",
        body: { current_password: current, new_password: password }
      });
      var session = await api("/api/gui/auth/session");
      applySession(session);
      $("password-form").reset();
      showToast("密码已更新");
      await showApp();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  async function logoutCurrentUser() {
    try { await api("/api/gui/auth/logout", { method: "POST" }); } catch (_) {}
    if ($("profile-dialog").open) $("profile-dialog").close();
    showLogin();
  }

  $("logout-button").addEventListener("click", logoutCurrentUser);

  $("theme-button").addEventListener("click", function () {
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true);
  });

  $("confirm-cancel").addEventListener("click", function () { settleConfirm(false); });
  $("confirm-accept").addEventListener("click", function () { settleConfirm(true); });
  $("confirm-dialog").addEventListener("cancel", function (event) {
    event.preventDefault();
    settleConfirm(false);
  });

  function closeValueDialog() {
    if ($("value-dialog").open) $("value-dialog").close();
  }

  $("value-close").addEventListener("click", closeValueDialog);
  $("value-done").addEventListener("click", closeValueDialog);
  $("value-dialog").addEventListener("cancel", function (event) {
    event.preventDefault();
    closeValueDialog();
  });
  $("value-copy").addEventListener("click", async function () {
    try {
      await navigator.clipboard.writeText($("value-content").value);
      showToast("内容已复制");
      closeValueDialog();
    } catch (_) {
      $("value-content").focus();
      $("value-content").select();
      showToast("内容已选中，请按 Ctrl+C 复制", true);
    }
  });

  function openProfileDialog() {
    renderCurrentUser();
    $("profile-name-form").reset();
    $("profile-password-form").reset();
    $("profile-username").value = state.username;
    $("profile-dialog").showModal();
  }

  function closeProfileDialog() {
    if ($("profile-dialog").open) $("profile-dialog").close();
  }

  $("profile-button").addEventListener("click", openProfileDialog);
  $("profile-close").addEventListener("click", closeProfileDialog);
  $("profile-dialog").addEventListener("cancel", function (event) {
    event.preventDefault();
    closeProfileDialog();
  });
  $("profile-logout").addEventListener("click", logoutCurrentUser);

  $("profile-name-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    var button = event.submitter;
    var username = $("profile-username").value.trim();
    button.disabled = true;
    try {
      var result = await api("/api/gui/auth/profile", {
        method: "PUT",
        body: { username: username, current_password: $("profile-username-password").value }
      });
      state.username = result.username;
      state.role = result.role || state.role;
      renderCurrentUser();
      $("profile-username-password").value = "";
      showToast("用户名已更新");
      if (state.activeView === "users" && state.role === "admin") await loadUsers();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  $("profile-password-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    var password = $("profile-new-password").value;
    if (password !== $("profile-confirm-password").value) {
      showToast("两次输入的新密码不一致", true);
      return;
    }
    var button = event.submitter;
    button.disabled = true;
    try {
      await api("/api/gui/auth/password", {
        method: "POST",
        body: { current_password: $("profile-current-password").value, new_password: password }
      });
      $("profile-password-form").reset();
      showToast("密码已更新，其他设备的会话已退出");
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  var titles = {
    dashboard: ["Overview", "运行概览"],
    integrations: ["Orchestration", "资源编排"],
    settings: ["Configuration", "系统设置"],
    records: ["Audit trail", "记录与审计"],
    users: ["Access control", "用户管理"]
  };

  async function switchView(name, options) {
    options = options || {};
    if (name === "users" && state.role !== "admin") {
      showToast("只有管理员可以管理平台用户", true);
      if (!options.skipLocation) updateViewLocation(state.activeView, true);
      return;
    }
    if (state.activeView === "integrations" && name !== "integrations" && state.orchestrationDirty) {
      if (!await confirmAction({
        title: "离开资源编排？",
        message: "编排还有未保存的更改，离开后将丢失。",
        confirmText: "放弃更改并离开",
        danger: true
      })) {
        if (options.skipLocation) updateViewLocation(state.activeView, true);
        return;
      }
      state.orchestrationDirty = false;
    }
    if (state.activeView === "settings" && name !== "settings" && state.settingsDirty) {
      if (!await confirmAction({
        title: "离开系统设置？",
        message: "系统设置还有未保存的更改，离开后将丢失。",
        confirmText: "放弃更改并离开",
        danger: true
      })) {
        if (options.skipLocation) updateViewLocation(state.activeView, true);
        return;
      }
      state.settingsDirty = false;
    }
    state.activeView = name;
    if (!options.skipLocation) updateViewLocation(name);
    document.querySelectorAll(".view").forEach(function (node) { node.classList.remove("active"); });
    document.querySelectorAll(".nav-item").forEach(function (node) { node.classList.toggle("active", node.dataset.view === name); });
    $("view-" + name).classList.add("active");
    $("page-kicker").textContent = titles[name][0];
    $("page-title").textContent = titles[name][1];
    document.querySelector(".sidebar").classList.remove("open");
    try {
      if (name === "dashboard") await loadDashboard();
      if (name === "integrations") await loadOrchestration();
      if (name === "settings") await loadSettings();
      if (name === "records") await loadRecords();
      if (name === "users") await loadUsers();
    } catch (error) {
      showToast(error.message, true);
    }
  }

  document.querySelectorAll(".nav-item").forEach(function (button) {
    button.addEventListener("click", function () { switchView(button.dataset.view); });
  });
  window.addEventListener("popstate", function () {
    switchView(viewFromLocation(), { skipLocation: true });
  });
  $("menu-button").addEventListener("click", function () { document.querySelector(".sidebar").classList.toggle("open"); });
  $("refresh-button").addEventListener("click", function () { switchView(state.activeView); });

  function renderDiagnostics(diagnostics) {
    var target = $("diagnostics-list");
    target.replaceChildren();
    var items = [];
    (diagnostics.errors || []).forEach(function (value) { items.push(["error", value]); });
    (diagnostics.warnings || []).forEach(function (value) { items.push(["warning", value]); });
    if (!items.length) items.push(["ok", "配置检查通过，没有发现问题。"]);
    items.forEach(function (item) {
      var node = document.createElement("div");
      node.className = "diagnostic " + item[0];
      node.textContent = item[1];
      target.appendChild(node);
    });
    var banner = $("security-banner");
    var important = (diagnostics.warnings || []).filter(function (text) {
      return text.indexOf("初始密码") >= 0 || text.indexOf("Webhook") >= 0;
    });
    if (important.length) {
      banner.textContent = important.join(" · ");
      banner.classList.remove("hidden");
    } else {
      banner.classList.add("hidden");
    }
    var connection = $("connection-pill");
    if ((diagnostics.errors || []).length) {
      connection.textContent = diagnostics.errors.length + " 项配置错误";
      connection.className = "pill health-error";
    } else if ((diagnostics.warnings || []).length) {
      connection.textContent = diagnostics.warnings.length + " 项提醒";
      connection.className = "pill health-warning";
    } else {
      connection.textContent = "服务正常";
      connection.className = "pill health-ok";
    }
  }

  function taskTag(label, enabled) {
    var node = document.createElement("span");
    node.className = "task-tag" + (enabled ? " on" : "");
    node.textContent = label + (enabled ? " 开" : " 关");
    return node;
  }

  function renderBotSummary(bots) {
    var target = $("bot-summary");
    target.replaceChildren();
    if (!bots.length) {
      var empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "还没有配置 QQ Bot。";
      target.appendChild(empty);
      return;
    }
    bots.forEach(function (bot) {
      var row = document.createElement("div");
      row.className = "summary-row";
      var title = document.createElement("div");
      var strong = document.createElement("strong");
      strong.textContent = bot.name;
      var small = document.createElement("div");
      small.className = "muted";
      small.textContent = bot.enabled ? bot.groups.length + " 个群" : "账号已停用";
      title.append(strong, small);
      var tags = document.createElement("div");
      tags.className = "task-tags";
      tags.append(
        taskTag("入群管理", bot.enabled && bot.task_presence.join_management),
        taskTag("消息记录/分析", bot.enabled && bot.task_presence.message_detection),
        taskTag("公告同步", bot.enabled && bot.task_presence.announcement_sync)
      );
      row.append(title, tags);
      target.appendChild(row);
    });
  }

  async function loadDashboard() {
    var data = await api("/api/gui/dashboard");
    $("count-joins").textContent = data.counts.join_requests || 0;
    $("count-messages").textContent = data.counts.group_messages || 0;
    $("count-runs").textContent = data.counts.moderation_runs || 0;
    $("count-notices").textContent = data.counts.announcements || 0;
    var graph = data.orchestration || {};
    $("managed-groups").textContent = (data.managed_groups || []).length + " 个群 · " + (graph.edges || 0) + " 条连接";
    $("dry-run-badge").textContent = data.dry_run ? "安全演练中" : "真实动作已启用";
    $("dry-run-badge").className = "pill" + (data.dry_run ? "" : " solid");
    renderBotSummary(data.bots || []);
    renderDiagnostics(data.diagnostics);
  }

  async function runJob(job, botId, button) {
    if (button) button.disabled = true;
    $("job-output").textContent = "任务执行中…";
    try {
      var suffix = botId ? "?bot_id=" + encodeURIComponent(botId) : "";
      var result = await api("/api/gui/jobs/" + job + suffix, { method: "POST" });
      $("job-output").textContent = JSON.stringify(result.result, null, 2);
      showToast("任务执行完成");
      await loadDashboard();
    } catch (error) {
      $("job-output").textContent = error.message;
      showToast(error.message, true);
    } finally {
      if (button) button.disabled = false;
    }
  }

  document.querySelectorAll("[data-job]").forEach(function (button) {
    button.addEventListener("click", function () { runJob(button.dataset.job, "", button); });
  });

  function statusText(status) {
    if (status && status.ok) return "已连接 · " + JSON.stringify(status);
    return "未连接 · " + (status && status.error ? status.error : JSON.stringify(status || {}));
  }

  function integrationButton(label, action) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "button secondary";
    button.textContent = label;
    button.addEventListener("click", action);
    return button;
  }

  function qqPublicUrl(bot) {
    return bot.webui_public_url || "";
  }

  async function refreshQQCode(requestNewCode) {
    if (!state.qqLoginBotId || state.qqQrRefreshing) return;
    state.qqQrRefreshing = true;
    var image = $("qq-qrcode");
    var placeholder = $("qq-qr-placeholder");
    var refreshFailure = "";
    placeholder.classList.remove("hidden");
    $("qq-qr-caption").textContent = requestNewCode ? "正在请求 NapCat 生成新二维码…" : "正在读取二维码…";
    image.onload = function () {
      placeholder.classList.add("hidden");
      image.classList.add("loaded");
      $("qq-qr-caption").textContent = refreshFailure
        ? "新二维码申请失败，当前显示缓存图片：" + refreshFailure
        : "请在约 2 分钟内扫码；临近过期时页面会自动申请新二维码。";
    };
    image.onerror = function () {
      image.classList.remove("loaded");
      placeholder.classList.remove("hidden");
      $("qq-qr-caption").textContent = "二维码尚未生成，请确认 qq-bridge 正在运行。";
    };
    var refreshStamp = Date.now();
    state.qqQrLastRefresh = refreshStamp;
    try {
      if (requestNewCode) {
        await api("/api/gui/integrations/qq/qrcode/refresh?bot_id=" + encodeURIComponent(state.qqLoginBotId), { method: "POST" });
      }
      image.src = "/api/gui/integrations/qq/qrcode?bot_id=" + encodeURIComponent(state.qqLoginBotId) + "&t=" + refreshStamp;
    } catch (error) {
      refreshFailure = error.message;
      $("qq-qr-caption").textContent = error.message;
      showToast(error.message, true);
      image.src = "/api/gui/integrations/qq/qrcode?bot_id=" + encodeURIComponent(state.qqLoginBotId) + "&t=" + refreshStamp;
    } finally {
      state.qqQrRefreshing = false;
    }
  }

  function qqLoginStatusText(bot) {
    if (!bot) return "未找到 QQ Bot 状态";
    var onebot = bot.onebot_status || bot.status || {};
    var napcat = bot.napcat_status || {};
    if (bot.connection_state === "connected") {
      var info = onebot.login_info || {};
      return "已登录" + (info.nickname ? " · " + info.nickname : "") + (info.user_id ? "（" + info.user_id + "）" : "");
    }
    if (bot.connection_state === "qq_logged_in_onebot_unavailable") {
      return "QQ 已登录，但 OneBot HTTP 服务异常：" + (onebot.error || "请重新部署以应用自动配置");
    }
    if (bot.connection_state === "qq_offline") return "QQ 登录态已离线，请刷新二维码重新登录。";
    if (bot.connection_state === "waiting_for_scan") return "NapCat 已连接，等待扫码确认…";
    if (bot.connection_state === "disabled") return "此 QQ Bot 已停用。";
    if (bot.connection_state === "credentials_missing") return bot.connection_message;
    return "NapCat 无法连接：" + (napcat.error || onebot.error || "请检查 qq-bridge 容器");
  }

  async function refreshQQLoginState() {
    if (!state.qqLoginBotId || state.qqLoginPolling) return;
    state.qqLoginPolling = true;
    try {
      var data = await api("/api/gui/integrations/qq?bot_id=" + encodeURIComponent(state.qqLoginBotId));
      var bot = (data.bots || [])[0];
      $("qq-login-state").textContent = qqLoginStatusText(bot);
      if (bot && bot.connection_state === "connected") {
        $("qq-login-state").classList.add("success");
        $("qq-qr-stage").classList.add("success");
        $("qq-qr-caption").textContent = "登录成功，可以关闭此窗口。";
      } else {
        $("qq-login-state").classList.remove("success");
        $("qq-qr-stage").classList.remove("success");
        if (Date.now() - state.qqQrLastRefresh > 105000) refreshQQCode(true);
      }
    } catch (error) {
      $("qq-login-state").textContent = "状态检查失败：" + error.message;
      $("qq-login-state").classList.remove("success");
      $("qq-qr-stage").classList.remove("success");
    } finally {
      state.qqLoginPolling = false;
    }
  }

  function openQQLogin(bot) {
    state.qqLoginBotId = bot.id;
    state.qqUrls[bot.id] = qqPublicUrl(bot);
    $("qq-login-new-window").disabled = !state.qqUrls[bot.id];
    $("qq-login-new-window").title = state.qqUrls[bot.id]
      ? "打开显式配置的 NapCat WebUI 地址"
      : "NapCat WebUI 默认仅在容器内部可用";
    $("qq-login-title").textContent = bot.name + " · 扫码登录";
    $("qq-login-state").textContent = "正在连接 NapCat…";
    $("qq-login-state").classList.remove("success");
    $("qq-qr-stage").classList.remove("success");
    state.qqQrLastRefresh = 0;
    $("qq-login-dialog").showModal();
    refreshQQCode(true);
    refreshQQLoginState();
    window.clearInterval(state.qqLoginTimer);
    state.qqLoginTimer = window.setInterval(refreshQQLoginState, 3000);
  }

  function closeQQLogin() {
    window.clearInterval(state.qqLoginTimer);
    state.qqLoginTimer = null;
    state.qqLoginBotId = "";
    state.qqLoginPolling = false;
    state.qqQrRefreshing = false;
    $("qq-qrcode").removeAttribute("src");
    $("qq-qrcode").classList.remove("loaded");
    $("qq-login-dialog").close();
  }

  async function feishuAction(botId, action, button, output) {
    button.disabled = true;
    output.textContent = action === "login" ? "等待飞书 CLI 返回授权信息…" : "正在退出飞书…";
    try {
      var data = await api("/api/gui/integrations/feishu/" + action + "?bot_id=" + encodeURIComponent(botId), { method: "POST" });
      output.textContent = typeof data.result === "string" ? data.result : JSON.stringify(data.result, null, 2);
      showToast("飞书操作已完成");
    } catch (error) {
      output.textContent = error.message;
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  function safeIdentifier(value, fallback) {
    var cleaned = String(value || "").toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "");
    return (cleaned || fallback || "resource").slice(0, 52);
  }

  function uniqueIdentifier(prefix, values) {
    var used = new Set(values || []);
    var index = 1;
    var candidate = prefix;
    while (used.has(candidate)) candidate = prefix + "-" + (++index);
    return candidate;
  }

  function graphNode(id) {
    return state.orchestrationNodes.find(function (node) { return node.id === id; });
  }

  function graphResource(id) {
    return (state.config.orchestration.resources || []).find(function (resource) { return resource.id === id; });
  }

  function defaultQQTasks() {
    return {
      join_management: { enabled: false, detect_requests: false, execute_management: false, auto_approve: false, auto_reject: false, minimum_confidence: 0.88 },
      message_detection: { enabled: false, record: false, scheduled_analysis: false, interval_minutes: 30, window_minutes: 5, risk_threshold: 0.7, max_messages_per_run: 300 },
      announcement_sync: { enabled: false, sync_interval_minutes: 30, feishu_bot_id: "" }
    };
  }

  function isQQGroupAssignment(edge) {
    var resource = graphResource(edge.target);
    return edge.source.indexOf("qq-bot:") === 0 && resource && resource.kind === "qq_group" && ["manages", "observes"].indexOf(edge.relation) >= 0;
  }

  function defaultNodePosition(kind, index) {
    var columns = { qq_bot: 80, feishu_bot: 80, qq_group: 430, feishu_group: 430, knowledge_base: 790 };
    var laneOffset = kind === "feishu_bot" || kind === "feishu_group" ? 300 : 70;
    return { x: columns[kind] || 80, y: laneOffset + (index % 5) * 145 };
  }

  function nextEdgeId(source, target, relation, edges) {
    var prefix = safeIdentifier("edge-" + source + "-" + target + "-" + relation, "edge").slice(0, 82);
    return uniqueIdentifier(prefix, (edges || state.orchestrationEdges).map(function (edge) { return edge.id; }));
  }

  function normalizeOrchestration(config) {
    normalizeBotConfig(config);
    config.orchestration = config.orchestration || {};
    config.orchestration.resources = config.orchestration.resources || [];
    config.orchestration.edges = config.orchestration.edges || [];
    config.orchestration.layout = config.orchestration.layout || {};

    var nodes = [];
    config.qq.bots.forEach(function (bot, index) {
      nodes.push({ id: "qq-bot:" + bot.id, kind: "qq_bot", name: bot.name, enabled: bot.enabled, ref: bot });
      if (!config.orchestration.layout["qq-bot:" + bot.id]) config.orchestration.layout["qq-bot:" + bot.id] = defaultNodePosition("qq_bot", index);
    });
    config.feishu.bots.forEach(function (bot, index) {
      nodes.push({ id: "feishu-bot:" + bot.id, kind: "feishu_bot", name: bot.name, enabled: bot.enabled, ref: bot });
      if (!config.orchestration.layout["feishu-bot:" + bot.id]) config.orchestration.layout["feishu-bot:" + bot.id] = defaultNodePosition("feishu_bot", index);
    });
    config.orchestration.resources.forEach(function (resource, index) {
      nodes.push({ id: resource.id, kind: resource.kind, name: resource.name, enabled: resource.enabled, ref: resource });
      if (!config.orchestration.layout[resource.id]) config.orchestration.layout[resource.id] = defaultNodePosition(resource.kind, index);
    });
    var resources = new Map(config.orchestration.resources.map(function (resource) { return [resource.id, resource]; }));
    config.orchestration.edges.forEach(function (edge) {
      var target = resources.get(edge.target);
      var assignment = edge.source.indexOf("qq-bot:") === 0 && target && target.kind === "qq_group" && ["manages", "observes"].indexOf(edge.relation) >= 0;
      if (assignment && !edge.tasks) edge.tasks = defaultQQTasks();
      if (!assignment && edge.tasks) delete edge.tasks;
    });
    state.orchestrationNodes = nodes;
    state.orchestrationEdges = config.orchestration.edges;
  }

  function nodeKindLabel(kind) {
    return ({ qq_bot: "QQ / ONEBOT", feishu_bot: "FEISHU BOT", qq_group: "QQ GROUP", feishu_group: "FEISHU GROUP", knowledge_base: "KNOWLEDGE" })[kind] || kind;
  }

  function nodeGlyph(kind) {
    return ({ qq_bot: "Q", feishu_bot: "F", qq_group: "#", feishu_group: "群", knowledge_base: "▤" })[kind] || "·";
  }

  function relationLabel(relation) {
    return ({ manages: "管理", observes: "监听", archives_to: "归档", searches: "检索", syncs: "同步" })[relation] || relation;
  }

  function defaultRelation(source, target) {
    if (!source || !target) return "syncs";
    if (/bot$/.test(source.kind) && /group$/.test(target.kind)) return "manages";
    if (target.kind === "knowledge_base") return source.kind === "feishu_bot" ? "searches" : "archives_to";
    if (target.kind === "feishu_bot") return "archives_to";
    return "syncs";
  }

  function graphPath(startX, startY, endX, endY) {
    var bend = Math.max(70, Math.abs(endX - startX) * 0.48);
    return "M " + startX + " " + startY + " C " + (startX + bend) + " " + startY + ", " + (endX - bend) + " " + endY + ", " + endX + " " + endY;
  }

  function renderOrchestrationEdges() {
    var layer = $("orchestration-edge-layer");
    layer.replaceChildren();
    var canvas = $("orchestration-canvas");
    var canvasRect = canvas.getBoundingClientRect();
    state.orchestrationEdges.forEach(function (edge) {
      var sourceNode = document.querySelector('.graph-node[data-node-id="' + CSS.escape(edge.source) + '"]');
      var targetNode = document.querySelector('.graph-node[data-node-id="' + CSS.escape(edge.target) + '"]');
      if (!sourceNode || !targetNode) return;
      if (sourceNode.classList.contains("search-muted") || targetNode.classList.contains("search-muted")) return;
      var sourceRect = sourceNode.getBoundingClientRect();
      var targetRect = targetNode.getBoundingClientRect();
      var startX = sourceRect.right - canvasRect.left;
      var startY = sourceRect.top + sourceRect.height / 2 - canvasRect.top;
      var endX = targetRect.left - canvasRect.left;
      var endY = targetRect.top + targetRect.height / 2 - canvasRect.top;
      var group = document.createElementNS("http://www.w3.org/2000/svg", "g");
      group.classList.add("edge-group");
      group.dataset.edgeId = edge.id;
      var hit = document.createElementNS("http://www.w3.org/2000/svg", "path");
      var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      var d = graphPath(startX, startY, endX, endY);
      hit.setAttribute("d", d);
      hit.setAttribute("class", "graph-edge-hit");
      path.setAttribute("d", d);
      path.setAttribute("class", "graph-edge" + (edge.enabled ? "" : " disabled"));
      path.setAttribute("marker-end", "url(#edge-arrow)");
      var label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x", String((startX + endX) / 2));
      label.setAttribute("y", String((startY + endY) / 2 - 8));
      label.setAttribute("class", "graph-edge-label");
      label.textContent = relationLabel(edge.relation);
      hit.addEventListener("dblclick", function () {
        state.orchestrationEdges.splice(state.orchestrationEdges.indexOf(edge), 1);
        markOrchestrationDirty();
        renderOrchestration();
      });
      group.append(hit, path, label);
      layer.appendChild(group);
    });
  }

  function graphConnectionCount(nodeId) {
    return state.orchestrationEdges.filter(function (edge) { return edge.source === nodeId || edge.target === nodeId; }).length;
  }

  function renderOrchestration() {
    var layer = $("orchestration-node-layer");
    layer.replaceChildren();
    var search = state.orchestrationSearch.trim().toLowerCase();
    var matchCount = 0;
    state.orchestrationNodes.forEach(function (node) {
      var position = state.config.orchestration.layout[node.id] || { x: 80, y: 80 };
      var searchable = [node.name, node.id, node.ref.external_id, nodeKindLabel(node.kind)].join(" ").toLowerCase();
      var matchesSearch = !search || searchable.indexOf(search) >= 0;
      if (matchesSearch) matchCount++;
      var card = document.createElement("article");
      card.className = "graph-node " + node.kind + (node.enabled ? "" : " disabled") + (state.orchestrationSelected === node.id ? " selected" : "") + (matchesSearch ? "" : " search-muted") + (search && matchesSearch ? " search-match" : "");
      card.dataset.nodeId = node.id;
      card.tabIndex = 0;
      card.style.left = position.x + "px";
      card.style.top = position.y + "px";
      card.innerHTML = '<div class="node-accent"></div><div class="node-head"><span class="node-glyph"></span><div><span class="node-kind"></span><strong class="node-name"></strong></div><i class="node-state"></i></div><div class="node-meta"><span class="node-detail"></span><span class="node-links"></span></div><button class="node-port" type="button" title="拖动以连接"></button>';
      card.querySelector(".node-glyph").textContent = nodeGlyph(node.kind);
      card.querySelector(".node-kind").textContent = nodeKindLabel(node.kind);
      card.querySelector(".node-name").textContent = node.name;
      card.querySelector(".node-detail").textContent = node.ref.external_id || node.ref.id || "未配置标识";
      card.querySelector(".node-links").textContent = graphConnectionCount(node.id) + " links";
      card.querySelector(".node-port").setAttribute("aria-label", "从 " + node.name + " 创建连接");
      card.addEventListener("click", function () {
        state.orchestrationSelected = node.id;
        renderOrchestration();
        renderOrchestrationInspector(node.id);
      });
      card.addEventListener("keydown", function (event) {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        state.orchestrationSelected = node.id;
        renderOrchestration();
        renderOrchestrationInspector(node.id);
      });
      card.addEventListener("contextmenu", function (event) {
        event.preventDefault();
        event.stopPropagation();
        state.orchestrationSelected = node.id;
        state.orchestrationContextNodeId = node.id;
        renderOrchestration();
        renderOrchestrationInspector(node.id);
        showNodeContextMenu(node, event.clientX, event.clientY);
      });
      card.addEventListener("dblclick", function (event) {
        if (node.kind !== "qq_group" || event.target.closest("button")) return;
        event.preventDefault();
        openGroupWorkspace(node);
      });
      card.addEventListener("pointerdown", function (event) {
        if (event.button !== 0 || event.target.closest("button, input, textarea, select")) return;
        state.orchestrationDrag = { id: node.id, startX: event.clientX, startY: event.clientY, x: position.x, y: position.y, moved: false };
        card.setPointerCapture(event.pointerId);
      });
      card.querySelector(".node-port").addEventListener("pointerdown", function (event) {
        event.preventDefault();
        event.stopPropagation();
        var rect = card.getBoundingClientRect();
        var canvasRect = $("orchestration-canvas").getBoundingClientRect();
        state.orchestrationConnect = { source: node.id, startX: rect.right - canvasRect.left, startY: rect.top + rect.height / 2 - canvasRect.top };
        this.setPointerCapture(event.pointerId);
      });
      layer.appendChild(card);
    });
    $("orchestration-empty").classList.toggle("hidden", state.orchestrationNodes.length > 0);
    $("orchestration-counts").textContent = search ? matchCount + " / " + state.orchestrationNodes.length + " 个节点匹配" : state.orchestrationNodes.length + " 个节点 · " + state.orchestrationEdges.length + " 条连接";
    window.requestAnimationFrame(renderOrchestrationEdges);
  }

  function focusOrchestrationNode(nodeId) {
    var node = graphNode(nodeId);
    if (!node) return;
    state.orchestrationSelected = nodeId;
    renderOrchestration();
    renderOrchestrationInspector(nodeId);
    var position = state.config.orchestration.layout[nodeId] || { x: 0, y: 0 };
    var viewport = $("orchestration-viewport");
    viewport.scrollTo({
      left: Math.max(0, position.x - viewport.clientWidth / 2 + 110),
      top: Math.max(0, position.y - viewport.clientHeight / 2 + 55),
      behavior: "smooth"
    });
  }

  function canvasPoint(clientX, clientY) {
    var canvas = $("orchestration-canvas");
    var rect = canvas.getBoundingClientRect();
    return { x: clientX - rect.left, y: clientY - rect.top };
  }

  function markOrchestrationDirty() {
    state.orchestrationDirty = true;
    $("orchestration-state").textContent = "有未保存的更改";
    $("orchestration-save").disabled = false;
  }

  function recordBotIdentityChange(platform, action, botId) {
    var createdKey = platform + "_created";
    var deletedKey = platform + "_deleted";
    var changes = state.orchestrationIdentityChanges;
    if (action === "created") {
      if (changes[deletedKey].indexOf(botId) >= 0) {
        changes[deletedKey] = changes[deletedKey].filter(function (id) { return id !== botId; });
      } else if (changes[createdKey].indexOf(botId) < 0) changes[createdKey].push(botId);
    } else if (changes[createdKey].indexOf(botId) >= 0) {
      changes[createdKey] = changes[createdKey].filter(function (id) { return id !== botId; });
    } else if (changes[deletedKey].indexOf(botId) < 0) changes[deletedKey].push(botId);
  }

  function syncOrchestrationToBots() {
    state.config.qq.bots.forEach(function (bot) {
      var nodeId = "qq-bot:" + bot.id;
      var search = state.orchestrationEdges.find(function (edge) {
        return edge.enabled && edge.source === nodeId && edge.target.indexOf("feishu-bot:") === 0 && edge.relation === "searches";
      });
      if (search) bot.search_feishu_bot_id = search.target.replace("feishu-bot:", "");
    });
    state.orchestrationEdges.forEach(function (edge) {
      if (isQQGroupAssignment(edge) && !edge.tasks) edge.tasks = defaultQQTasks();
      if (!isQQGroupAssignment(edge) && edge.tasks) delete edge.tasks;
    });
    state.config.orchestration.edges = state.orchestrationEdges;
  }

  function statusForNode(node) {
    var list = node.kind === "qq_bot" ? state.orchestrationStatuses.qq : state.orchestrationStatuses.feishu;
    return list.find(function (item) { return item.id === node.ref.id; });
  }

  function inspectorSection(title) {
    var section = document.createElement("section");
    section.className = "inspector-section";
    var heading = document.createElement("h4");
    heading.textContent = title;
    section.appendChild(heading);
    return section;
  }

  async function renderGroupActivity(node, host) {
    if (!node.ref.external_id) {
      host.textContent = "填写群号并保存编排后，即可读取这个群的聊天、公告和分析记录。";
      return;
    }
    host.textContent = "正在读取群数据…";
    try {
      var url = "/api/gui/orchestration/group?group_id=" + encodeURIComponent(node.ref.external_id) + "&resource_id=" + encodeURIComponent(node.ref.id) + "&limit=30";
      var data = await api(url);
      host.replaceChildren();
      var metrics = document.createElement("div");
      metrics.className = "inspector-metrics";
      [["入群申请", data.counts.joins], ["群聊消息", data.counts.messages], ["分析报告", data.counts.moderation], ["公告版本", data.counts.announcements]].forEach(function (item) {
        var metric = document.createElement("div");
        metric.innerHTML = "<span></span><strong></strong>";
        metric.querySelector("span").textContent = item[0];
        metric.querySelector("strong").textContent = item[1] || 0;
        metrics.appendChild(metric);
      });
      host.appendChild(metrics);
      var manager = document.createElement("p");
      manager.className = "inspector-muted";
      manager.textContent = data.managers.length ? "管理 Bot：" + data.managers.map(function (bot) { return bot.name; }).join("、") : "尚未连接管理 Bot";
      host.appendChild(manager);
    } catch (error) {
      host.textContent = error.message;
    }
  }

  function groupRecordKindLabel(kind) {
    return { messages: "聊天记录", announcements: "群公告", joins: "入群申请", moderation: "风控分析" }[kind] || "群记录";
  }

  function groupRecordRangeLabel(value) {
    return { all: "全部记录", today: "今天", week: "最近一周", month: "最近一月", half_year: "最近半年", year: "最近一年" }[value] || value;
  }

  function displayRecordTime(value) {
    if (!value) return "时间未知";
    var parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return value;
    return parsed.toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function currentGroupWorkspaceNode() {
    return graphNode(state.groupWorkspaceNodeId);
  }

  function groupWorkspaceParameters(extra) {
    var node = currentGroupWorkspaceNode();
    if (!node) throw new Error("群节点已经不存在，请关闭窗口后重试");
    var parameters = new URLSearchParams({
      group_id: node.ref.external_id,
      resource_id: node.ref.id
    });
    Object.keys(extra || {}).forEach(function (key) {
      if (extra[key] !== undefined && extra[key] !== null && extra[key] !== "") parameters.set(key, String(extra[key]));
    });
    return parameters;
  }

  function appendGroupProfileValue(target, label, value) {
    var term = document.createElement("dt");
    var description = document.createElement("dd");
    term.textContent = label;
    description.textContent = value || "未设置";
    target.append(term, description);
  }

  function renderGroupWorkspaceMetadata(data) {
    var resource = data.resource || {};
    $("group-workspace-title").textContent = resource.name || "QQ群 " + data.group_id;
    $("group-workspace-subtitle").textContent = "群号 " + data.group_id + " · " + data.managers.length + " 个关联 Bot";
    $("group-workspace-avatar").textContent = (resource.name || "群").charAt(0).toUpperCase();
    ["messages", "announcements", "joins", "moderation"].forEach(function (kind) {
      $("group-count-" + kind).textContent = data.counts[kind] || 0;
    });
    var profile = $("group-profile-list");
    profile.replaceChildren();
    appendGroupProfileValue(profile, "群名称", resource.name || "未命名群");
    appendGroupProfileValue(profile, "QQ群号", data.group_id);
    appendGroupProfileValue(profile, "资源 ID", resource.id);
    appendGroupProfileValue(profile, "状态", resource.enabled === false ? "已停用" : "已启用");
    appendGroupProfileValue(profile, "说明", resource.description || "暂无说明");
    var managerList = $("group-manager-list");
    managerList.replaceChildren();
    data.managers.forEach(function (manager) {
      var card = document.createElement("article");
      var tasks = [];
      (manager.assignments || []).forEach(function (assignment) {
        if (assignment.tasks.join_management.enabled) tasks.push("入群管理");
        if (assignment.tasks.message_detection.enabled) tasks.push("消息记录");
        if (assignment.tasks.announcement_sync.enabled) tasks.push("公告同步");
      });
      card.innerHTML = '<span class="group-manager-avatar"></span><div><strong></strong><small></small></div><i></i>';
      card.querySelector(".group-manager-avatar").textContent = (manager.name || manager.id).charAt(0).toUpperCase();
      card.querySelector("strong").textContent = manager.name || manager.id;
      card.querySelector("small").textContent = tasks.length ? Array.from(new Set(tasks)).join(" · ") : "仅保留连接";
      card.querySelector("i").className = manager.enabled ? "online" : "";
      card.querySelector("i").title = manager.enabled ? "Bot 已启用" : "Bot 已停用";
      managerList.appendChild(card);
    });
    if (!data.managers.length) {
      var empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "尚未连接 QQ Bot。";
      managerList.appendChild(empty);
    }
  }

  function groupStatusTag(text, tone) {
    var tag = document.createElement("span");
    tag.className = "group-status-tag" + (tone ? " " + tone : "");
    tag.textContent = text;
    return tag;
  }

  function joinStatusLabel(value) {
    return {
      received: "已登记",
      detected: "仅检测",
      manual_review: "待人工审核",
      approved: "已同意",
      rejected: "已拒绝",
      dry_run: "演练未执行",
      action_failed: "执行失败"
    }[value] || value || "已登记";
  }

  function joinDecisionLabel(value) {
    return { approve: "建议同意", reject: "建议拒绝", manual_review: "转人工审核" }[value] || value;
  }

  function renderGroupMessages(records, target) {
    records.slice().reverse().forEach(function (record) {
      var row = document.createElement("article");
      row.className = "qq-message-row";
      var avatar = document.createElement("span");
      avatar.className = "qq-message-avatar";
      avatar.textContent = (record.sender_name || record.user_id || "?").charAt(0).toUpperCase();
      var body = document.createElement("div");
      body.className = "qq-message-body";
      var meta = document.createElement("div");
      meta.className = "qq-message-meta";
      var sender = document.createElement("strong");
      sender.textContent = record.sender_name || record.user_id || "未知成员";
      var source = document.createElement("span");
      source.textContent = (record.sender_name && record.user_id ? "QQ " + record.user_id + " · " : "") + (record.bot_id ? "经 " + record.bot_id + " 收集 · " : "") + displayRecordTime(record.sent_at);
      meta.append(sender, source);
      var bubble = document.createElement("div");
      bubble.className = "qq-message-bubble";
      bubble.textContent = record.text || "[空消息]";
      var id = document.createElement("small");
      id.textContent = "消息 ID：" + (record.message_id || "-");
      body.append(meta, bubble, id);
      row.append(avatar, body);
      target.appendChild(row);
    });
  }

  function renderGroupAnnouncements(records, target) {
    records.forEach(function (record) {
      var card = document.createElement("article");
      card.className = "group-announcement-card" + (record.is_deleted ? " deleted" : "");
      var header = document.createElement("header");
      var copy = document.createElement("div");
      var title = document.createElement("h3");
      title.textContent = record.title || "未命名公告";
      var meta = document.createElement("small");
      meta.textContent = "公告 ID：" + record.announcement_id + " · " + displayRecordTime(record.published_at || record.first_seen_at);
      copy.append(title, meta);
      var tags = document.createElement("div");
      tags.className = "group-status-tags";
      if (record.is_deleted) tags.appendChild(groupStatusTag("已在群聊删除", "danger"));
      else if (record.is_current) tags.appendChild(groupStatusTag("当前公告", "success"));
      else tags.appendChild(groupStatusTag("历史版本", "muted"));
      tags.appendChild(groupStatusTag(record.sync_status === "synced" ? "已归档" : record.sync_status === "failed" ? "归档失败" : record.sync_status === "syncing" ? "归档中" : "待归档", record.sync_status === "failed" ? "danger" : ""));
      header.append(copy, tags);
      var content = document.createElement("p");
      content.textContent = record.content || "[空公告]";
      var footer = document.createElement("footer");
      var sources = Array.isArray(record.source_bot_ids_json) ? record.source_bot_ids_json : [record.bot_id].filter(Boolean);
      var noticeIds = Array.isArray(record.source_announcement_ids_json) ? record.source_announcement_ids_json : [record.announcement_id].filter(Boolean);
      footer.textContent = "发布者 " + (record.author_id || "未知") + " · 来源 Bot " + (sources.join("、") || "未知") + (noticeIds.length > 1 ? " · 已合并公告 ID " + noticeIds.join("、") : "") + " · 最后发现 " + displayRecordTime(record.last_seen_at);
      card.append(header, content, footer);
      target.appendChild(card);
    });
  }

  function renderGroupJoins(records, target) {
    records.forEach(function (record) {
      var card = document.createElement("article");
      card.className = "group-join-card";
      var avatar = document.createElement("span");
      avatar.className = "group-join-avatar";
      avatar.textContent = (record.user_id || "?").charAt(0).toUpperCase();
      var body = document.createElement("div");
      var heading = document.createElement("header");
      var title = document.createElement("div");
      var strong = document.createElement("strong");
      strong.textContent = "申请人 " + (record.user_id || "未知");
      var time = document.createElement("small");
      time.textContent = displayRecordTime(record.received_at) + " · 经 " + (record.bot_id || "未知 Bot") + " 收集";
      title.append(strong, time);
      var status = record.action_status || "received";
      heading.append(title, groupStatusTag(joinStatusLabel(status), status === "approved" ? "success" : status === "rejected" || status === "action_failed" ? "danger" : ""));
      var comment = document.createElement("p");
      comment.textContent = record.comment || "未填写入群说明";
      var result = document.createElement("footer");
      result.textContent = record.decision ? "判断：" + joinDecisionLabel(record.decision) + " · 置信度 " + (record.confidence === null ? "-" : record.confidence) + " · " + (record.reason || "无补充原因") : "尚未形成审核结论";
      body.append(heading, comment, result);
      card.append(avatar, body);
      target.appendChild(card);
    });
  }

  function renderGroupModeration(records, target) {
    records.forEach(function (record) {
      var result = record.result_json || {};
      var card = document.createElement("article");
      card.className = "group-moderation-card";
      var header = document.createElement("header");
      var title = document.createElement("div");
      var strong = document.createElement("strong");
      strong.textContent = result.summary || "群消息分析";
      var time = document.createElement("small");
      time.textContent = displayRecordTime(record.window_start) + " — " + displayRecordTime(record.window_end);
      title.append(strong, time);
      header.append(title, groupStatusTag("风险 " + Number(record.max_risk || 0).toFixed(2), Number(record.max_risk || 0) >= 0.8 ? "danger" : ""));
      var overview = document.createElement("p");
      overview.textContent = "分析 " + (record.message_count || 0) + " 条消息 · " + (record.alerted ? "已通知管理员" : "未触发通知");
      var findings = document.createElement("div");
      findings.className = "group-finding-list";
      (result.findings || []).forEach(function (finding) {
        var item = document.createElement("article");
        var findingTitle = document.createElement("strong");
        findingTitle.textContent = (finding.severity || "-") + " · " + (finding.category || "未分类") + " · " + Number(finding.risk_score || 0).toFixed(2);
        var reason = document.createElement("p");
        reason.textContent = finding.reason || "无说明";
        item.append(findingTitle, reason);
        findings.appendChild(item);
      });
      if (!findings.children.length) {
        var safe = document.createElement("p");
        safe.className = "muted";
        safe.textContent = "本次分析没有风险发现。";
        findings.appendChild(safe);
      }
      card.append(header, overview, findings);
      target.appendChild(card);
    });
  }

  function renderGroupWorkspaceRecords(data) {
    var target = $("group-record-content");
    target.replaceChildren();
    target.className = "group-record-content " + state.groupWorkspaceKind;
    var records = data.records || [];
    if (!records.length) {
      var empty = document.createElement("div");
      empty.className = "group-record-empty";
      empty.innerHTML = "<span>⌁</span><strong></strong><p></p>";
      empty.querySelector("strong").textContent = "还没有" + groupRecordKindLabel(state.groupWorkspaceKind);
      empty.querySelector("p").textContent = state.groupWorkspaceOffset ? "当前页没有记录，请返回上一页。" : "Bot 收集到的数据会按时间显示在这里。";
      target.appendChild(empty);
    } else if (state.groupWorkspaceKind === "messages") renderGroupMessages(records, target);
    else if (state.groupWorkspaceKind === "announcements") renderGroupAnnouncements(records, target);
    else if (state.groupWorkspaceKind === "joins") renderGroupJoins(records, target);
    else renderGroupModeration(records, target);
    state.groupWorkspaceHasMore = Boolean(data.has_more);
    var start = records.length ? state.groupWorkspaceOffset + 1 : 0;
    var end = state.groupWorkspaceOffset + records.length;
    $("group-record-page-state").textContent = records.length ? "显示第 " + start + "–" + end + " 条" : "0 条记录";
    $("group-record-prev").disabled = state.groupWorkspaceOffset <= 0;
    $("group-record-next").disabled = !state.groupWorkspaceHasMore;
  }

  async function loadGroupWorkspaceMetadata() {
    var data = await api("/api/gui/orchestration/group?" + groupWorkspaceParameters({ limit: 8 }).toString());
    if (!$("group-workspace-dialog").open) return;
    renderGroupWorkspaceMetadata(data);
  }

  async function loadGroupWorkspaceRecords(resetOffset) {
    if (resetOffset) state.groupWorkspaceOffset = 0;
    var kind = state.groupWorkspaceKind;
    var offset = state.groupWorkspaceOffset;
    var target = $("group-record-content");
    target.className = "group-record-content loading";
    target.innerHTML = '<div class="group-record-loading"><span></span><p>正在读取' + groupRecordKindLabel(kind) + '…</p></div>';
    var parameters = groupWorkspaceParameters({
      kind: kind,
      limit: 50,
      offset: offset,
      search: $("group-record-search").value.trim()
    });
    var data = await api("/api/gui/orchestration/group/records?" + parameters.toString());
    if (!$("group-workspace-dialog").open || kind !== state.groupWorkspaceKind || offset !== state.groupWorkspaceOffset) return;
    renderGroupWorkspaceRecords(data);
  }

  async function refreshGroupWorkspace(resetOffset) {
    try {
      await Promise.all([loadGroupWorkspaceMetadata(), loadGroupWorkspaceRecords(resetOffset)]);
    } catch (error) {
      $("group-record-content").className = "group-record-content";
      $("group-record-content").textContent = error.status === 404 && state.orchestrationDirty ? "这个群尚未保存到资源编排。请先保存编排，再打开群详情。" : error.message;
    }
  }

  function openGroupWorkspace(node) {
    if (!node || node.kind !== "qq_group") return;
    if (!node.ref.external_id) {
      showToast("请先为群节点填写 QQ 群号", true);
      return;
    }
    state.groupWorkspaceNodeId = node.id;
    state.groupWorkspaceKind = "messages";
    state.groupWorkspaceOffset = 0;
    state.groupWorkspaceHasMore = false;
    $("group-record-search").value = "";
    $("group-record-range").value = "all";
    document.querySelectorAll("[data-group-tab]").forEach(function (button) {
      button.classList.toggle("active", button.dataset.groupTab === "messages");
    });
    $("group-workspace-title").textContent = node.name;
    $("group-workspace-subtitle").textContent = "群号 " + node.ref.external_id;
    $("group-workspace-avatar").textContent = (node.name || "群").charAt(0).toUpperCase();
    $("group-workspace-dialog").showModal();
    refreshGroupWorkspace(true);
  }

  async function downloadGroupWorkspaceRecords(kind) {
    var range = $("group-record-range").value;
    var parameters = groupWorkspaceParameters({
      kind: kind,
      range: range,
      timezone_offset_minutes: new Date().getTimezoneOffset()
    });
    var response = await fetch("/api/gui/orchestration/group/export?" + parameters.toString());
    if (!response.ok) {
      var detail = {};
      try { detail = await response.json(); } catch (_) {}
      if (response.status === 401) showLogin();
      throw new Error(errorText(detail.detail || "导出失败"));
    }
    var blob = await response.blob();
    var disposition = response.headers.get("Content-Disposition") || "";
    var match = disposition.match(/filename="([^"]+)"/i);
    var link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = match ? match[1] : "neoqbot-group-records.json";
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(function () { URL.revokeObjectURL(link.href); }, 1000);
    showToast("已导出" + groupRecordRangeLabel(range) + "的" + (kind === "all" ? "全部群记录" : groupRecordKindLabel(kind)));
  }

  async function clearCurrentGroupRecords() {
    var kind = state.groupWorkspaceKind;
    var range = $("group-record-range").value;
    var warning = kind === "announcements" ? "当前仍存在于 QQ 的公告可能在下次同步时重新归档。" : "";
    if (!await confirmAction({
      title: "清除" + groupRecordKindLabel(kind) + "？",
      message: "将永久清除这个群“" + groupRecordRangeLabel(range) + "”范围内的" + groupRecordKindLabel(kind) + "。" + warning + " 此操作会写入审计日志，但记录本身无法恢复。",
      confirmText: "确认清除",
      danger: true
    })) return;
    var parameters = groupWorkspaceParameters({
      range: range,
      timezone_offset_minutes: new Date().getTimezoneOffset()
    });
    var result = await api("/api/gui/orchestration/group/records/" + encodeURIComponent(kind) + "?" + parameters.toString(), { method: "DELETE" });
    var archiveCount = result.message_archive ? result.message_archive.records : 0;
    showToast("已清除 " + result.database_records + " 条数据库记录" + (archiveCount ? "，并移除 " + archiveCount + " 条 JSONL 归档" : ""));
    await refreshGroupWorkspace(true);
  }

  document.querySelectorAll("[data-group-tab]").forEach(function (button) {
    button.addEventListener("click", function () {
      state.groupWorkspaceKind = button.dataset.groupTab;
      state.groupWorkspaceOffset = 0;
      $("group-record-search").value = "";
      document.querySelectorAll("[data-group-tab]").forEach(function (item) { item.classList.toggle("active", item === button); });
      loadGroupWorkspaceRecords(true).catch(function (error) { showToast(error.message, true); });
    });
  });
  $("group-workspace-close").addEventListener("click", function () { $("group-workspace-dialog").close(); });
  $("group-workspace-dialog").addEventListener("cancel", function () { state.groupWorkspaceNodeId = ""; });
  $("group-workspace-dialog").addEventListener("close", function () { state.groupWorkspaceNodeId = ""; });
  $("group-workspace-refresh").addEventListener("click", function () { refreshGroupWorkspace(false); });
  $("group-record-search").addEventListener("input", function () {
    window.clearTimeout(state.groupWorkspaceSearchTimer);
    state.groupWorkspaceSearchTimer = window.setTimeout(function () {
      loadGroupWorkspaceRecords(true).catch(function (error) { showToast(error.message, true); });
    }, 280);
  });
  $("group-record-prev").addEventListener("click", function () {
    state.groupWorkspaceOffset = Math.max(0, state.groupWorkspaceOffset - 50);
    loadGroupWorkspaceRecords(false).catch(function (error) { showToast(error.message, true); });
  });
  $("group-record-next").addEventListener("click", function () {
    if (!state.groupWorkspaceHasMore) return;
    state.groupWorkspaceOffset += 50;
    loadGroupWorkspaceRecords(false).catch(function (error) { showToast(error.message, true); });
  });
  $("group-record-export").addEventListener("click", function () {
    downloadGroupWorkspaceRecords(state.groupWorkspaceKind).catch(function (error) { showToast(error.message, true); });
  });
  $("group-workspace-export-all").addEventListener("click", function () {
    downloadGroupWorkspaceRecords("all").catch(function (error) { showToast(error.message, true); });
  });
  $("group-record-clear").addEventListener("click", function () {
    clearCurrentGroupRecords().catch(function (error) { showToast(error.message, true); });
  });

  function createAssignmentTaskEditor(edge, onCommit) {
    edge.tasks = edge.tasks || defaultQQTasks();
    var tasks = edge.tasks;
    var editor = document.createElement("article");
    editor.className = "assignment-task-editor";
    var source = graphNode(edge.source);
    var target = graphNode(edge.target);
    editor.innerHTML = [
      '<header><div><strong></strong><small></small></div><span class="pill"></span></header>',
      '<div class="workflow-list">',
      '<section class="task-card"><div class="task-master"><div><strong>入群管理</strong><small>检测、审核与平台动作</small></div><input data-role="master" data-field="join.enabled" type="checkbox"></div><div class="task-detail">',
      '<label class="switch-field"><span><strong>检测申请</strong></span><input data-field="join.detect_requests" type="checkbox"></label>',
      '<label class="switch-field"><span><strong>执行审核</strong></span><input data-field="join.execute_management" type="checkbox"></label>',
      '<label class="switch-field"><span><strong>自动同意</strong></span><input data-field="join.auto_approve" type="checkbox"></label>',
      '<label class="switch-field"><span><strong>自动拒绝</strong></span><input data-field="join.auto_reject" type="checkbox"></label>',
      '<label>最低置信度<input data-field="join.minimum_confidence" type="number" min="0" max="1" step="0.01"></label></div></section>',
      '<section class="task-card active"><div class="task-master"><div><strong>消息记录与分析</strong><small>记录与定时分析可独立启用；同时开启时只采集一次</small></div><span class="pill">2 个开关</span></div><div class="task-detail">',
      '<label class="switch-field"><span><strong>记录</strong><small>实时备份发言人、时间和文本</small></span><input data-field="message.record" type="checkbox"></label>',
      '<label class="switch-field"><span><strong>定时分析</strong><small>每半小时分析最近 5 分钟记录</small></span><input data-field="message.scheduled_analysis" type="checkbox"></label>',
      '<label>分析周期（分钟）<input data-field="message.interval_minutes" type="number" min="1"></label>',
      '<label>分析窗口（分钟）<input data-field="message.window_minutes" type="number" min="1"></label>',
      '<label>风险阈值<input data-field="message.risk_threshold" type="number" min="0" max="1" step="0.01"></label>',
      '<label>单次最大消息数<input data-field="message.max_messages_per_run" type="number" min="1"></label></div></section>',
      '<section class="task-card"><div class="task-master"><div><strong>公告同步</strong><small>启用即全量归档已有公告，并持续同步新公告</small></div><input data-role="master" data-field="announcement.enabled" type="checkbox"></div><div class="task-detail">',
      '<label>同步间隔（分钟）<input data-field="announcement.sync_interval_minutes" type="number" min="1"></label>',
      '<label>飞书 Bot ID<input data-field="announcement.feishu_bot_id" placeholder="留空使用默认账号"></label></div></section>',
      '</div>'
    ].join("");
    editor.querySelector("header strong").textContent = (source ? source.name : edge.source) + " → " + (target ? target.name : edge.target);
    editor.querySelector("header small").textContent = edge.id;
    editor.querySelector("header .pill").textContent = relationLabel(edge.relation);
    var values = {
      "join.enabled": tasks.join_management.enabled,
      "join.detect_requests": tasks.join_management.detect_requests,
      "join.execute_management": tasks.join_management.execute_management,
      "join.auto_approve": tasks.join_management.auto_approve,
      "join.auto_reject": tasks.join_management.auto_reject,
      "join.minimum_confidence": tasks.join_management.minimum_confidence,
      "message.record": tasks.message_detection.record,
      "message.scheduled_analysis": tasks.message_detection.scheduled_analysis,
      "message.interval_minutes": tasks.message_detection.interval_minutes,
      "message.window_minutes": tasks.message_detection.window_minutes,
      "message.risk_threshold": tasks.message_detection.risk_threshold,
      "message.max_messages_per_run": tasks.message_detection.max_messages_per_run,
      "announcement.enabled": tasks.announcement_sync.enabled,
      "announcement.sync_interval_minutes": tasks.announcement_sync.sync_interval_minutes,
      "announcement.feishu_bot_id": tasks.announcement_sync.feishu_bot_id || ""
    };
    Object.keys(values).forEach(function (name) { setField(editor, name, values[name]); });
    editor.querySelectorAll(".task-card").forEach(bindTaskCard);
    var joinMaster = field(editor, "join.enabled");
    field(editor, "join.execute_management").addEventListener("change", function () {
      if (this.checked) { joinMaster.checked = true; field(editor, "join.detect_requests").checked = true; }
      joinMaster.dispatchEvent(new Event("change"));
    });
    field(editor, "join.detect_requests").addEventListener("change", function () {
      if (this.checked) { joinMaster.checked = true; joinMaster.dispatchEvent(new Event("change")); }
    });
    function commit() {
      var joinEnabled = field(editor, "join.enabled").checked;
      var recordEnabled = field(editor, "message.record").checked;
      var analysisEnabled = field(editor, "message.scheduled_analysis").checked;
      var announcementEnabled = field(editor, "announcement.enabled").checked;
      edge.tasks = {
        join_management: {
          enabled: joinEnabled,
          detect_requests: joinEnabled && field(editor, "join.detect_requests").checked,
          execute_management: joinEnabled && field(editor, "join.execute_management").checked,
          auto_approve: joinEnabled && field(editor, "join.auto_approve").checked,
          auto_reject: joinEnabled && field(editor, "join.auto_reject").checked,
          minimum_confidence: numberValue(field(editor, "join.minimum_confidence").value, 0.88)
        },
        message_detection: {
          enabled: recordEnabled || analysisEnabled,
          record: recordEnabled,
          scheduled_analysis: analysisEnabled,
          interval_minutes: numberValue(field(editor, "message.interval_minutes").value, 30),
          window_minutes: numberValue(field(editor, "message.window_minutes").value, 5),
          risk_threshold: numberValue(field(editor, "message.risk_threshold").value, 0.7),
          max_messages_per_run: numberValue(field(editor, "message.max_messages_per_run").value, 300)
        },
        announcement_sync: {
          enabled: announcementEnabled,
          sync_interval_minutes: numberValue(field(editor, "announcement.sync_interval_minutes").value, 30),
          feishu_bot_id: field(editor, "announcement.feishu_bot_id").value.trim()
        }
      };
      if (onCommit) onCommit(edge);
      else markOrchestrationDirty();
    }
    editor.querySelectorAll("input").forEach(function (input) {
      input.addEventListener(input.type === "checkbox" ? "change" : "input", commit);
    });
    return editor;
  }

  function createAssignmentSwitches(edge) {
    edge.tasks = edge.tasks || defaultQQTasks();
    var tasks = edge.tasks;
    var source = graphNode(edge.source);
    var target = graphNode(edge.target);
    var editor = document.createElement("article");
    editor.className = "assignment-task-editor assignment-switches";
    editor.innerHTML = [
      '<header><div><strong></strong><small>详细策略、阈值和周期请右键对应群节点编辑</small></div><span class="pill"></span></header>',
      '<div class="task-detail">',
      '<label class="switch-field"><span><strong>检测申请</strong><small>接收申请并留档，不做模型判断</small></span><input data-field="join.detect_requests" type="checkbox"></label>',
      '<label class="switch-field"><span><strong>执行审核</strong><small>按本群策略调用模型并通知管理员</small></span><input data-field="join.execute_management" type="checkbox"></label>',
      '<label class="switch-field"><span><strong>自动同意</strong><small>高置信度同意建议直接执行</small></span><input data-field="join.auto_approve" type="checkbox"></label>',
      '<label class="switch-field"><span><strong>自动拒绝</strong><small>高置信度拒绝建议直接执行</small></span><input data-field="join.auto_reject" type="checkbox"></label>',
      '<label class="switch-field"><span><strong>记录消息</strong></span><input data-field="message.record" type="checkbox"></label>',
      '<label class="switch-field"><span><strong>定时分析</strong></span><input data-field="message.scheduled_analysis" type="checkbox"></label>',
      '<label class="switch-field"><span><strong>同步公告</strong></span><input data-field="announcement.enabled" type="checkbox"></label>',
      '</div>'
    ].join("");
    editor.querySelector("header strong").textContent = (source ? source.name : edge.source) + " → " + (target ? target.name : edge.target);
    editor.querySelector("header .pill").textContent = relationLabel(edge.relation);
    setField(editor, "join.detect_requests", tasks.join_management.detect_requests);
    setField(editor, "join.execute_management", tasks.join_management.execute_management);
    setField(editor, "join.auto_approve", tasks.join_management.auto_approve);
    setField(editor, "join.auto_reject", tasks.join_management.auto_reject);
    setField(editor, "message.record", tasks.message_detection.record);
    setField(editor, "message.scheduled_analysis", tasks.message_detection.scheduled_analysis);
    setField(editor, "announcement.enabled", tasks.announcement_sync.enabled);
    function commit() {
      var detectRequests = field(editor, "join.detect_requests").checked;
      var executeManagement = field(editor, "join.execute_management").checked;
      if (executeManagement) {
        detectRequests = true;
        field(editor, "join.detect_requests").checked = true;
      }
      tasks.join_management.enabled = detectRequests || executeManagement;
      tasks.join_management.detect_requests = detectRequests;
      tasks.join_management.execute_management = executeManagement;
      tasks.join_management.auto_approve = field(editor, "join.auto_approve").checked;
      tasks.join_management.auto_reject = field(editor, "join.auto_reject").checked;
      tasks.message_detection.record = field(editor, "message.record").checked;
      tasks.message_detection.scheduled_analysis = field(editor, "message.scheduled_analysis").checked;
      tasks.message_detection.enabled = tasks.message_detection.record || tasks.message_detection.scheduled_analysis;
      tasks.announcement_sync.enabled = field(editor, "announcement.enabled").checked;
      markOrchestrationDirty();
    }
    editor.querySelectorAll("input").forEach(function (input) { input.addEventListener("change", commit); });
    return editor;
  }

  function appendAssignmentEditors(node, content) {
    var assignments = state.orchestrationEdges.filter(function (edge) {
      return isQQGroupAssignment(edge) && (edge.source === node.id || edge.target === node.id);
    });
    if (!assignments.length) return;
    var section = inspectorSection("功能开关");
    var hint = document.createElement("p");
    hint.className = "inspector-muted";
    hint.textContent = "开关属于 Bot→群连接；本群的审核判断策略和历史记录在右键群节点的详细编辑中维护。";
    section.appendChild(hint);
    assignments.forEach(function (edge) { section.appendChild(createAssignmentSwitches(edge)); });
    content.appendChild(section);
  }

  function renderOrchestrationInspector(nodeId) {
    var node = graphNode(nodeId);
    var empty = $("inspector-empty");
    var content = $("inspector-content");
    if (!node) {
      empty.classList.remove("hidden");
      content.classList.add("hidden");
      return;
    }
    empty.classList.add("hidden");
    content.classList.remove("hidden");
    content.replaceChildren();
    var header = document.createElement("header");
    header.className = "inspector-header";
    header.innerHTML = '<span class="node-glyph"></span><div><small></small><h3></h3></div><button class="inspector-close" type="button">×</button>';
    header.querySelector(".node-glyph").textContent = nodeGlyph(node.kind);
    header.querySelector("small").textContent = nodeKindLabel(node.kind);
    header.querySelector("h3").textContent = node.name;
    header.querySelector(".inspector-close").addEventListener("click", function () {
      state.orchestrationSelected = "";
      renderOrchestration();
      renderOrchestrationInspector("");
    });
    content.appendChild(header);

    var form = document.createElement("div");
    form.className = "inspector-form";
    form.innerHTML = '<label>显示名称<input data-inspector="name"></label>' +
      (node.kind === "qq_bot" || node.kind === "feishu_bot" ? '<label>内部 ID<input data-inspector="id" disabled></label>' : '<label>平台标识<input data-inspector="external_id" placeholder="群号、chat_id 或知识库 ID"></label>') +
      '<label class="switch-field"><span><strong>启用资源</strong><small>停用后保留配置与连接</small></span><input data-inspector="enabled" type="checkbox"></label>';
    form.querySelector('[data-inspector="name"]').value = node.ref.name;
    var idField = form.querySelector('[data-inspector="id"]');
    if (idField) idField.value = node.ref.id;
    var externalField = form.querySelector('[data-inspector="external_id"]');
    if (externalField) externalField.value = node.ref.external_id || "";
    form.querySelector('[data-inspector="enabled"]').checked = node.ref.enabled;
    form.querySelectorAll("input").forEach(function (input) {
      if (input.disabled) return;
      input.addEventListener("input", function () {
        if (input.dataset.inspector === "enabled") node.ref.enabled = input.checked;
        else node.ref[input.dataset.inspector] = input.value;
        node.name = node.ref.name;
        node.enabled = node.ref.enabled;
        markOrchestrationDirty();
        renderOrchestration();
      });
      input.addEventListener("change", function () {
        if (input.dataset.inspector === "enabled") {
          node.ref.enabled = input.checked;
          node.enabled = input.checked;
          markOrchestrationDirty();
          renderOrchestration();
        }
      });
    });
    content.appendChild(form);

    if (node.kind === "qq_group") {
      var overview = inspectorSection("群数据看板");
      var overviewHost = document.createElement("div");
      overviewHost.className = "group-activity";
      overview.appendChild(overviewHost);
      content.appendChild(overview);
      renderGroupActivity(node, overviewHost);
    }

    if (node.kind === "qq_bot" || node.kind === "feishu_bot") {
      var status = statusForNode(node);
      var statusSection = inspectorSection("运行状态");
      var output = document.createElement("pre");
      output.className = "inspector-status";
      output.textContent = node.kind === "qq_bot" ? (status ? status.connection_message : "状态读取中…") : (status ? statusText(status.status) : "状态读取中…");
      var actions = document.createElement("div");
      actions.className = "button-row";
      if (node.kind === "qq_bot") {
        var login = integrationButton("扫码登录", function () { if (status) openQQLogin(status); });
        actions.append(login);
      } else {
        actions.append(
          integrationButton("登录", function () { feishuAction(node.ref.id, "login", this, output); }),
          integrationButton("退出", function () { feishuAction(node.ref.id, "logout", this, output); })
        );
      }
      var configure = integrationButton("详细配置", function () { openNodeDetail(node.id); });
      actions.append(configure);
      statusSection.append(output, actions);
      content.appendChild(statusSection);
    } else {
      var detailSection = inspectorSection("详细配置");
      var detailHint = document.createElement("p");
      detailHint.className = "inspector-muted";
      detailHint.textContent = node.kind === "qq_group"
        ? "复杂策略、周期与阈值在“详细编辑”中配置；群消息、公告、申请与分析记录可从右键“查看详细”打开。"
        : "资源说明与元数据在节点详细编辑中维护。";
      detailSection.append(detailHint, integrationButton("打开详细配置", function () { openNodeDetail(node.id); }));
      content.appendChild(detailSection);
    }

    appendAssignmentEditors(node, content);

    var connections = inspectorSection("连接");
    var connectionList = document.createElement("div");
    connectionList.className = "connection-list";
    state.orchestrationEdges.filter(function (edge) { return edge.source === node.id || edge.target === node.id; }).forEach(function (edge) {
      var other = graphNode(edge.source === node.id ? edge.target : edge.source);
      var row = document.createElement("div");
      row.innerHTML = '<span></span><select><option value="manages">管理</option><option value="observes">监听</option><option value="archives_to">归档</option><option value="searches">检索</option><option value="syncs">同步</option></select><button type="button" title="删除连接">×</button>';
      row.querySelector("span").textContent = other ? other.name : "未知节点";
      row.querySelector("select").value = edge.relation;
      row.querySelector("select").addEventListener("change", function () {
        edge.relation = this.value;
        if (isQQGroupAssignment(edge) && !edge.tasks) edge.tasks = defaultQQTasks();
        if (!isQQGroupAssignment(edge) && edge.tasks) delete edge.tasks;
        markOrchestrationDirty();
        renderOrchestrationEdges();
        renderOrchestrationInspector(node.id);
      });
      row.querySelector("button").addEventListener("click", function () {
        state.orchestrationEdges.splice(state.orchestrationEdges.indexOf(edge), 1);
        markOrchestrationDirty();
        renderOrchestration();
        renderOrchestrationInspector(node.id);
      });
      connectionList.appendChild(row);
    });
    if (!connectionList.children.length) {
      var hint = document.createElement("p");
      hint.className = "inspector-muted";
      hint.textContent = "从节点右侧端口拖到另一个节点即可创建连接。";
      connectionList.appendChild(hint);
    }
    connections.appendChild(connectionList);
    content.appendChild(connections);

    var danger = document.createElement("button");
    danger.type = "button";
    danger.className = "button danger full";
    danger.textContent = "从编排中删除";
    danger.addEventListener("click", deleteSelectedOrchestrationNode);
    content.appendChild(danger);
  }

  async function deleteSelectedOrchestrationNode() {
    var node = graphNode(state.orchestrationSelected);
    if (!node) return;
    if (!await confirmAction({
      title: "删除编排资源？",
      message: "将删除“" + node.name + "”及其所有连接。更改将在保存编排后生效。",
      confirmText: "删除资源",
      danger: true
    })) return;
    if (node.kind === "qq_bot") {
      recordBotIdentityChange("qq", "deleted", node.ref.id);
      state.config.qq.bots = state.config.qq.bots.filter(function (bot) { return "qq-bot:" + bot.id !== node.id; });
    }
    else if (node.kind === "feishu_bot") {
      recordBotIdentityChange("feishu", "deleted", node.ref.id);
      state.config.feishu.bots = state.config.feishu.bots.filter(function (bot) { return "feishu-bot:" + bot.id !== node.id; });
      state.config.qq.bots.forEach(function (bot) {
        if (bot.search_feishu_bot_id === node.ref.id) bot.search_feishu_bot_id = "";
      });
      state.config.orchestration.edges.forEach(function (edge) {
        if (edge.tasks && edge.tasks.announcement_sync && edge.tasks.announcement_sync.feishu_bot_id === node.ref.id) edge.tasks.announcement_sync.feishu_bot_id = "";
      });
    }
    else state.config.orchestration.resources = state.config.orchestration.resources.filter(function (resource) { return resource.id !== node.id; });
    state.config.orchestration.edges = state.config.orchestration.edges.filter(function (edge) { return edge.source !== node.id && edge.target !== node.id; });
    delete state.config.orchestration.layout[node.id];
    state.orchestrationSelected = "";
    normalizeOrchestration(state.config);
    markOrchestrationDirty();
    renderOrchestration();
    renderOrchestrationInspector("");
  }

  function createOrchestrationNode(kind) {
    $("orchestration-menu").classList.add("hidden");
    openNodeDetail("", kind, state.orchestrationMenuPoint);
  }

  function autoLayoutOrchestration() {
    var lanes = { qq_bot: [], feishu_bot: [], qq_group: [], feishu_group: [], knowledge_base: [] };
    state.orchestrationNodes.forEach(function (node) { lanes[node.kind].push(node); });
    Object.keys(lanes).forEach(function (kind) {
      lanes[kind].forEach(function (node, index) { state.config.orchestration.layout[node.id] = defaultNodePosition(kind, index); });
    });
    markOrchestrationDirty();
    renderOrchestration();
  }

  function fitOrchestration() {
    var viewport = $("orchestration-viewport");
    var positions = Object.values(state.config.orchestration.layout || {});
    if (!positions.length) return;
    var minX = Math.max(0, Math.min.apply(null, positions.map(function (position) { return position.x; })) - 50);
    var minY = Math.max(0, Math.min.apply(null, positions.map(function (position) { return position.y; })) - 50);
    viewport.scrollTo({ left: minX, top: minY, behavior: "smooth" });
  }

  async function saveOrchestration() {
    var button = $("orchestration-save");
    button.disabled = true;
    $("orchestration-state").textContent = "正在校验并热加载…";
    try {
      syncOrchestrationToBots();
      var result = await api("/api/gui/orchestration", {
        method: "PUT",
        body: {
          config: state.config,
          revision: state.settingsRevision,
          identity_changes: clone(state.orchestrationIdentityChanges)
        }
      });
      state.settingsRevision = result.revision || state.settingsRevision;
      state.orchestrationDirty = false;
      $("orchestration-state").textContent = result.restart_required.length ? "已保存，部分连接参数需重启" : "配置已同步";
      showToast("编排已保存并应用");
      await loadOrchestration();
      return true;
    } catch (error) {
      $("orchestration-state").textContent = error.status === 409 ? "检测到配置冲突，请刷新后重试" : "保存失败";
      showToast(error.message, true);
      button.disabled = false;
      return false;
    }
  }

  async function loadOrchestration() {
    var settings = await api("/api/gui/orchestration");
    state.config = settings.config;
    state.settingsRevision = settings.revision || "";
    state.orchestrationSensitiveEditsAllowed = Boolean(settings.sensitive_edits_allowed);
    state.orchestrationIdentityChanges = { qq_created: [], qq_deleted: [], feishu_created: [], feishu_deleted: [] };
    state.orchestrationStatuses = { qq: [], feishu: [] };
    normalizeOrchestration(state.config);
    state.orchestrationDirty = false;
    $("orchestration-state").textContent = "配置已同步";
    $("orchestration-save").disabled = true;
    renderOrchestration();
    if (state.orchestrationSelected && graphNode(state.orchestrationSelected)) renderOrchestrationInspector(state.orchestrationSelected);
    refreshOrchestrationStatuses();
  }

  async function refreshOrchestrationStatuses() {
    var statuses = await Promise.allSettled([api("/api/gui/integrations/qq"), api("/api/gui/integrations/feishu")]);
    state.orchestrationStatuses.qq = statuses[0].status === "fulfilled" ? statuses[0].value.bots || [] : [];
    state.orchestrationStatuses.feishu = statuses[1].status === "fulfilled" ? statuses[1].value.bots || [] : [];
    if (state.activeView !== "integrations" || !state.config || !state.config.orchestration) return;
    renderOrchestration();
    if (state.orchestrationSelected) renderOrchestrationInspector(state.orchestrationSelected);
  }

  function showNodeContextMenu(node, clientX, clientY) {
    var menu = $("orchestration-node-menu");
    $("orchestration-menu").classList.add("hidden");
    $("orchestration-node-menu-label").textContent = nodeKindLabel(node.kind) + " · " + node.name;
    menu.querySelector('[data-node-action="view"]').classList.toggle("hidden", node.kind !== "qq_group");
    menu.style.left = Math.min(clientX, window.innerWidth - 250) + "px";
    menu.style.top = Math.min(clientY, window.innerHeight - 300) + "px";
    menu.classList.remove("hidden");
  }

  $("orchestration-canvas").addEventListener("contextmenu", function (event) {
    if (event.target.closest(".graph-node")) return;
    event.preventDefault();
    state.orchestrationMenuPoint = canvasPoint(event.clientX, event.clientY);
    var menu = $("orchestration-menu");
    $("orchestration-node-menu").classList.add("hidden");
    menu.style.left = Math.min(event.clientX, window.innerWidth - 250) + "px";
    menu.style.top = Math.min(event.clientY, window.innerHeight - 340) + "px";
    menu.classList.remove("hidden");
  });
  $("orchestration-add").addEventListener("click", function () {
    var viewport = $("orchestration-viewport");
    state.orchestrationMenuPoint = { x: viewport.scrollLeft + 150, y: viewport.scrollTop + 120 };
    var rect = this.getBoundingClientRect();
    var menu = $("orchestration-menu");
    menu.style.left = rect.left + "px";
    menu.style.top = (rect.bottom + 8) + "px";
    menu.classList.toggle("hidden");
  });
  document.querySelectorAll("[data-create-kind]").forEach(function (button) {
    button.addEventListener("click", function () { createOrchestrationNode(button.dataset.createKind); });
  });
  document.addEventListener("pointerdown", function (event) {
    if (!event.target.closest("#orchestration-menu, #orchestration-add")) $("orchestration-menu").classList.add("hidden");
    if (!event.target.closest("#orchestration-node-menu")) $("orchestration-node-menu").classList.add("hidden");
  });
  document.querySelectorAll("[data-node-action]").forEach(function (button) {
    button.addEventListener("click", function () {
      var node = graphNode(state.orchestrationContextNodeId);
      $("orchestration-node-menu").classList.add("hidden");
      if (!node) return;
      if (button.dataset.nodeAction === "view") openGroupWorkspace(node);
      if (button.dataset.nodeAction === "edit") openNodeDetail(node.id);
      if (button.dataset.nodeAction === "focus") focusOrchestrationNode(node.id);
      if (button.dataset.nodeAction === "toggle") {
        node.ref.enabled = !node.ref.enabled;
        node.enabled = node.ref.enabled;
        markOrchestrationDirty();
        renderOrchestration();
        renderOrchestrationInspector(node.id);
      }
      if (button.dataset.nodeAction === "delete") deleteSelectedOrchestrationNode();
    });
  });
  window.addEventListener("pointermove", function (event) {
    if (state.orchestrationDrag) {
      var drag = state.orchestrationDrag;
      var position = state.config.orchestration.layout[drag.id];
      position.x = Math.max(20, drag.x + event.clientX - drag.startX);
      position.y = Math.max(20, drag.y + event.clientY - drag.startY);
      drag.moved = true;
      var card = document.querySelector('.graph-node[data-node-id="' + CSS.escape(drag.id) + '"]');
      if (card) { card.style.left = position.x + "px"; card.style.top = position.y + "px"; }
      renderOrchestrationEdges();
    }
    if (state.orchestrationConnect) {
      var point = canvasPoint(event.clientX, event.clientY);
      $("orchestration-temp-edge").setAttribute("d", graphPath(state.orchestrationConnect.startX, state.orchestrationConnect.startY, point.x, point.y));
    }
  });
  window.addEventListener("pointerup", function (event) {
    if (state.orchestrationDrag) {
      if (state.orchestrationDrag.moved) markOrchestrationDirty();
      state.orchestrationDrag = null;
    }
    if (state.orchestrationConnect) {
      var targetElement = document.elementFromPoint(event.clientX, event.clientY);
      var targetCard = targetElement && targetElement.closest(".graph-node");
      var sourceId = state.orchestrationConnect.source;
      if (targetCard && targetCard.dataset.nodeId !== sourceId) {
        var targetId = targetCard.dataset.nodeId;
        var duplicate = state.orchestrationEdges.some(function (edge) { return edge.source === sourceId && edge.target === targetId; });
        if (!duplicate) {
          var relation = defaultRelation(graphNode(sourceId), graphNode(targetId));
          var edge = { id: nextEdgeId(sourceId, targetId, relation), source: sourceId, target: targetId, relation: relation, enabled: true };
          state.orchestrationEdges.push(edge);
          if (isQQGroupAssignment(edge)) edge.tasks = defaultQQTasks();
          markOrchestrationDirty();
        } else showToast("这两个节点已经连接", true);
      }
      state.orchestrationConnect = null;
      $("orchestration-temp-edge").setAttribute("d", "");
      renderOrchestration();
      if (state.orchestrationSelected) renderOrchestrationInspector(state.orchestrationSelected);
    }
  });
  window.addEventListener("resize", function () { if (state.activeView === "integrations") renderOrchestrationEdges(); });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      $("orchestration-menu").classList.add("hidden");
      $("orchestration-node-menu").classList.add("hidden");
    }
    if (state.activeView !== "integrations" || !state.orchestrationSelected || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
    if (event.key === "Delete" || event.key === "Backspace") deleteSelectedOrchestrationNode();
  });
  $("orchestration-auto-layout").addEventListener("click", autoLayoutOrchestration);
  $("orchestration-fit").addEventListener("click", fitOrchestration);
  $("orchestration-save").addEventListener("click", saveOrchestration);
  $("orchestration-search").addEventListener("input", function () {
    state.orchestrationSearch = this.value;
    renderOrchestration();
  });
  $("orchestration-search").addEventListener("keydown", function (event) {
    if (event.key !== "Enter") return;
    var query = state.orchestrationSearch.trim().toLowerCase();
    if (!query) return;
    var match = state.orchestrationNodes.find(function (node) {
      return [node.name, node.id, node.ref.external_id, nodeKindLabel(node.kind)].join(" ").toLowerCase().indexOf(query) >= 0;
    });
    if (match) focusOrchestrationNode(match.id);
    else showToast("没有找到匹配的编排节点", true);
  });
  $("qq-login-close").addEventListener("click", closeQQLogin);
  $("qq-qrcode-refresh").addEventListener("click", function () { refreshQQCode(true); });
  $("qq-token-copy").addEventListener("click", async function () {
    if (!state.qqLoginBotId) return;
    try {
      var data = await api("/api/gui/integrations/qq/webui-token?bot_id=" + encodeURIComponent(state.qqLoginBotId), { method: "POST" });
      try {
        await navigator.clipboard.writeText(data.token);
        showToast("NapCat Token 已复制");
      } catch (_) {
        showValueDialog({
          title: "NapCat Token",
          message: "浏览器未允许自动复制。请在此复制 Token，并避免发送到聊天、截图或日志中。",
          value: data.token
        });
      }
    } catch (error) {
      showToast(error.message, true);
    }
  });
  $("qq-login-new-window").addEventListener("click", function () {
    if (!state.qqLoginBotId || !state.qqUrls[state.qqLoginBotId]) {
      showToast("NapCat WebUI 默认未发布到宿主机；扫码登录不受影响", true);
      return;
    }
    window.open(state.qqUrls[state.qqLoginBotId], "_blank", "noopener");
  });
  $("qq-login-dialog").addEventListener("cancel", function (event) {
    event.preventDefault();
    closeQQLogin();
  });

  var settingsSteps = ["llm", "policy", "storage", "system", "review"];

  function markSettingsDirty() {
    state.settingsDirty = true;
    $("settings-state").textContent = "有未保存的更改";
  }

  $("settings-form").addEventListener("input", markSettingsDirty);
  $("settings-form").addEventListener("change", markSettingsDirty);

  function activateSettingsStep(name) {
    var index = settingsSteps.indexOf(name);
    if (index < 0) return;
    state.settingsStep = name;
    document.querySelectorAll(".settings-step").forEach(function (button) {
      var buttonIndex = settingsSteps.indexOf(button.dataset.section);
      button.classList.toggle("active", buttonIndex === index);
      button.classList.toggle("completed", buttonIndex < index);
    });
    document.querySelectorAll(".settings-section").forEach(function (section) {
      section.classList.toggle("active", section.dataset.settingsSection === name);
    });
    $("settings-prev").disabled = index === 0;
    $("settings-next").classList.toggle("hidden", index === settingsSteps.length - 1);
    if (name === "review") updateSettingsReview();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  document.querySelectorAll(".settings-step").forEach(function (button) {
    button.addEventListener("click", function () { activateSettingsStep(button.dataset.section); });
  });
  $("settings-prev").addEventListener("click", function () {
    var index = settingsSteps.indexOf(state.settingsStep);
    activateSettingsStep(settingsSteps[Math.max(0, index - 1)]);
  });
  $("settings-next").addEventListener("click", function () {
    var index = settingsSteps.indexOf(state.settingsStep);
    activateSettingsStep(settingsSteps[Math.min(settingsSteps.length - 1, index + 1)]);
  });

  function defaultExternalQQConnection(botId) {
    var safeBotId = botId || "new-bot";
    var secretRoot = "data/secrets/qq/" + safeBotId;
    return {
      onebot_base_url: "http://127.0.0.1:3000",
      access_token: "",
      access_token_file: secretRoot + "/onebot.token",
      webhook_secret: "",
      webui_base_url: "http://127.0.0.1:6099",
      webui_token: "",
      webui_token_file: secretRoot + "/webui.token",
      qrcode_path: "data/napcat-cache/" + safeBotId + "/qrcode.png"
    };
  }

  function defaultQQBot(index, requestedId) {
    var botId = requestedId || "qq-bot-" + (index + 1);
    var bundled = index === 0;
    var externalConnection = defaultExternalQQConnection(botId);
    return {
      id: botId,
      name: "QQ Bot " + (index + 1),
      enabled: false,
      connection_mode: bundled ? "bundled_napcat" : "external",
      onebot_base_url: bundled ? "http://qq-bridge:3000" : externalConnection.onebot_base_url,
      access_token: "",
      access_token_file: bundled ? "data/secrets/napcat-onebot.token" : externalConnection.access_token_file,
      webhook_secret: "",
      request_timeout_seconds: 15,
      webui_base_url: bundled ? "http://qq-bridge:6099" : externalConnection.webui_base_url,
      webui_token: "",
      webui_token_file: bundled ? "data/secrets/napcat-webui.token" : externalConnection.webui_token_file,
      webui_public_url: "",
      webui_public_port: 6099,
      qrcode_path: bundled ? "/app/napcat-cache/qrcode.png" : externalConnection.qrcode_path,
      administrator_qq_ids: [],
      management_group_ids: [],
      announcement_actions: ["get_group_notice", "_get_group_notice"],
      search_feishu_bot_id: ""
    };
  }

  function defaultFeishuBot(index) {
    return {
      id: "feishu-bot-" + (index + 1),
      name: "飞书 Bot " + (index + 1),
      enabled: false,
      driver: "disabled",
      executable: "feishu",
      timeout_seconds: 60,
      search_prefixes: ["搜索 ", "查询 ", "/search "],
      max_search_results: 5,
      command_templates: {},
      archive_payload_stdin: false,
      extra_environment: {}
    };
  }

  function normalizeBotConfig(c) {
    c.qq.bots = c.qq.bots || [];
    c.feishu.bots = c.feishu.bots || [];
    c.orchestration = c.orchestration || { resources: [], edges: [], layout: {} };
    c.orchestration.resources = c.orchestration.resources || [];
    c.orchestration.edges = c.orchestration.edges || [];
    var resources = new Map(c.orchestration.resources.map(function (resource) { return [resource.id, resource]; }));
    c.orchestration.edges.forEach(function (edge) {
      var target = resources.get(edge.target);
      var assignment = edge.source.indexOf("qq-bot:") === 0 && target && target.kind === "qq_group" && ["manages", "observes"].indexOf(edge.relation) >= 0;
      if (assignment && !edge.tasks) edge.tasks = defaultQQTasks();
      if (!assignment && edge.tasks) delete edge.tasks;
    });
    if (c === state.config) state.orchestrationEdges = c.orchestration.edges;
  }

  function nodeDetailControl(name) {
    return $("node-detail-body").querySelector('[data-node-field="' + name + '"]');
  }

  function setNodeDetailValue(name, value) {
    var control = nodeDetailControl(name);
    if (!control) return;
    if (control.type === "checkbox") control.checked = Boolean(value);
    else control.value = value == null ? "" : value;
  }

  function syncNewQQSecretPaths(nextBotId) {
    var dialogState = state.nodeDialog;
    if (!dialogState || dialogState.mode !== "create" || dialogState.kind !== "qq_bot") return;
    var previousBotId = dialogState.autoSecretBotId || dialogState.draft.id;
    var previousRoot = "data/secrets/qq/" + previousBotId;
    var nextRoot = "data/secrets/qq/" + nextBotId;
    var accessTokenFile = nodeDetailControl("access_token_file");
    var webuiTokenFile = nodeDetailControl("webui_token_file");
    var qrcodePath = nodeDetailControl("qrcode_path");
    if (accessTokenFile.value.trim() === previousRoot + "/onebot.token") {
      accessTokenFile.value = nextRoot + "/onebot.token";
    }
    if (webuiTokenFile.value.trim() === previousRoot + "/webui.token") {
      webuiTokenFile.value = nextRoot + "/webui.token";
    }
    if (qrcodePath.value.trim() === "data/napcat-cache/" + previousBotId + "/qrcode.png") {
      qrcodePath.value = "data/napcat-cache/" + nextBotId + "/qrcode.png";
    }
    dialogState.autoSecretBotId = nextBotId;
  }

  function applyQQConnectionMode(preserveExternalValues) {
    var dialogState = state.nodeDialog;
    if (!dialogState || dialogState.kind !== "qq_bot") return;
    var mode = nodeDetailControl("connection_mode").value;
    var connectionFields = [
      "onebot_base_url", "access_token", "access_token_file", "webhook_secret",
      "webui_base_url", "webui_token", "webui_token_file", "qrcode_path"
    ];
    if (preserveExternalValues && dialogState.lastConnectionMode === "external") {
      dialogState.externalConnectionValues = {};
      connectionFields.forEach(function (name) {
        dialogState.externalConnectionValues[name] = nodeDetailControl(name).value;
      });
    }
    if (mode === "bundled_napcat") {
      setNodeDetailValue("onebot_base_url", "http://qq-bridge:3000");
      setNodeDetailValue("access_token", "");
      setNodeDetailValue("access_token_file", "data/secrets/napcat-onebot.token");
      setNodeDetailValue("webhook_secret", "");
      setNodeDetailValue("webui_base_url", "http://qq-bridge:6099");
      setNodeDetailValue("webui_token", "");
      setNodeDetailValue("webui_token_file", "data/secrets/napcat-webui.token");
      setNodeDetailValue("qrcode_path", "/app/napcat-cache/qrcode.png");
    } else if (preserveExternalValues && dialogState.externalConnectionValues) {
      Object.keys(dialogState.externalConnectionValues).forEach(function (name) {
        setNodeDetailValue(name, dialogState.externalConnectionValues[name]);
      });
    } else if (mode === "external" && dialogState.lastConnectionMode === "bundled_napcat") {
      var externalValues = defaultExternalQQConnection(nodeDetailControl("id").value.trim());
      Object.keys(externalValues).forEach(function (name) {
        setNodeDetailValue(name, externalValues[name]);
      });
    }
    connectionFields.forEach(function (name) {
      var control = nodeDetailControl(name);
      control.disabled = mode === "bundled_napcat" || !state.orchestrationSensitiveEditsAllowed;
      control.title = mode === "bundled_napcat"
        ? "内置 NapCat 的地址、Token 文件和二维码路径由 Compose 自动维护"
        : "";
    });
    dialogState.lastConnectionMode = mode;
  }

  function newNodeDraft(kind) {
    if (kind === "qq_bot") {
      var qqId = uniqueIdentifier("qq-bot-" + (state.config.qq.bots.length + 1), state.config.qq.bots.map(function (bot) { return bot.id; }));
      var qq = defaultQQBot(state.config.qq.bots.length, qqId);
      qq.name = "新 QQ Bot";
      return qq;
    }
    if (kind === "feishu_bot") {
      var fs = defaultFeishuBot(state.config.feishu.bots.length);
      fs.id = uniqueIdentifier("feishu-bot-" + (state.config.feishu.bots.length + 1), state.config.feishu.bots.map(function (bot) { return bot.id; }));
      fs.name = "新飞书 Bot";
      return fs;
    }
    var labels = { qq_group: "新 QQ 群", feishu_group: "新飞书群", knowledge_base: "新知识库" };
    return {
      id: uniqueIdentifier(kind.replace("_", "-") + "-1", state.config.orchestration.resources.map(function (item) { return item.id; })),
      kind: kind,
      name: labels[kind],
      external_id: "",
      description: "",
      enabled: true,
      metadata: {}
    };
  }

  function nodeDetailCommonHtml(existing) {
    return [
      '<section class="node-detail-section"><div class="node-detail-section-title"><span>Identity</span><h3>节点身份</h3></div><div class="form-grid">',
      '<label>内部 ID<input data-node-field="id" pattern="[A-Za-z0-9_-]+" maxlength="64" required ' + (existing ? "readonly" : "") + '><small>' + (existing ? "已创建节点的内部 ID 不可修改，它绑定 Webhook、Secret 与登录身份。" : "创建后不可修改；仅允许字母、数字、下划线和连字符。") + '</small></label>',
      '<label>显示名称<input data-node-field="name" maxlength="80" required></label>',
      '<label class="switch-field span-2"><span><strong>启用节点</strong><small>停用会保留配置和连接，但不会执行任务。</small></span><input data-node-field="enabled" type="checkbox"></label>',
      '</div></section>'
    ].join("");
  }

  function qqNodeDetailHtml(existing) {
    return nodeDetailCommonHtml(existing) + [
      '<section class="node-detail-section"><div class="node-detail-section-title"><span>OneBot 11</span><h3>QQ 连接</h3></div><div class="form-grid">',
      '<label class="span-2">连接方式<select data-node-field="connection_mode"><option value="bundled_napcat">内置 NapCat（Compose）</option><option value="external">外部 OneBot / NapCat</option></select><small>Compose 内置侧车只支持一个真实 QQ 账号；其他 Bot 必须使用独立外部实例。</small></label>',
      '<label class="span-2">OneBot Base URL<input data-node-field="onebot_base_url" data-sensitive></label>',
      '<label>Access Token<input data-node-field="access_token" data-sensitive type="password" placeholder="留空保持原值"></label>',
      '<label>Token 文件<input data-node-field="access_token_file" data-sensitive></label>',
      '<label>Webhook Secret<input data-node-field="webhook_secret" data-sensitive type="password" placeholder="留空保持原值"></label>',
      '<label>请求超时（秒）<input data-node-field="request_timeout_seconds" data-sensitive type="number" min="0.1" step="0.1"></label>',
      '<label class="span-2">管理员 QQ（每行一个）<textarea data-node-field="administrator_qq_ids" rows="3"></textarea></label>',
      '<label class="span-2">管理群（每行一个群号）<textarea data-node-field="management_group_ids" rows="3"></textarea></label>',
      '</div></section>',
      '<section class="node-detail-section"><div class="node-detail-section-title"><span>NapCat</span><h3>登录与 WebUI</h3></div><div class="form-grid">',
      '<label class="span-2">WebUI Base URL<input data-node-field="webui_base_url" data-sensitive></label>',
      '<label>WebUI Token<input data-node-field="webui_token" data-sensitive type="password" placeholder="留空保持原值"></label>',
      '<label>WebUI Token 文件<input data-node-field="webui_token_file" data-sensitive></label>',
      '<label>公开访问地址<input data-node-field="webui_public_url" data-sensitive placeholder="https://..."></label>',
      '<label>公开端口<input data-node-field="webui_public_port" data-sensitive type="number" min="1" max="65535"></label>',
      '<label class="span-2">二维码路径<input data-node-field="qrcode_path" data-sensitive></label>',
      '</div></section>',
      '<section class="node-detail-section"><div class="node-detail-section-title"><span>Capabilities</span><h3>账号能力</h3></div><div class="form-grid">',
      '<label class="span-2">公告动作（每行一个）<textarea data-node-field="announcement_actions" data-sensitive rows="3"></textarea></label>',
      '<label class="span-2">默认搜索飞书 Bot<select data-node-field="search_feishu_bot_id"><option value="">不指定</option></select><small>群级公告归档目标仍以 Bot→群连接上的事务设置为准。</small></label>',
      '</div></section>'
    ].join("");
  }

  function feishuNodeDetailHtml(existing) {
    return nodeDetailCommonHtml(existing) + [
      '<section class="node-detail-section"><div class="node-detail-section-title"><span>Feishu CLI</span><h3>账号连接</h3></div><div class="form-grid">',
      '<label>驱动<select data-node-field="driver" data-sensitive><option value="cli">CLI</option><option value="disabled">Disabled</option></select></label>',
      '<label>可执行文件<input data-node-field="executable" data-sensitive></label>',
      '<label>超时（秒）<input data-node-field="timeout_seconds" type="number" min="1" step="0.1"></label>',
      '<label>最大搜索结果<input data-node-field="max_search_results" type="number" min="1" max="20"></label>',
      '<label class="switch-field span-2"><span><strong>公告 JSON 走 stdin</strong><small>避免超长命令行。</small></span><input data-node-field="archive_payload_stdin" data-sensitive type="checkbox"></label>',
      '<label class="span-2">管理员搜索前缀（每行一个）<textarea data-node-field="search_prefixes" rows="3"></textarea></label>',
      '<label class="span-2">命令模板（JSON）<textarea data-node-field="command_templates" data-sensitive class="code-input" rows="9"></textarea></label>',
      '<label class="span-2">额外环境变量（JSON）<textarea data-node-field="extra_environment" data-sensitive class="code-input" rows="6"></textarea></label>',
      '</div></section>'
    ].join("");
  }

  function resourceNodeDetailHtml(existing, kind) {
    var groupPolicy = kind === "qq_group" ? [
      '<section class="node-detail-section"><div class="node-detail-section-title"><span>Join review</span><h3>本群入群审核策略</h3></div><div class="node-detail-section-content"><p class="muted">这里的已填写字段只应用于本群；留空的字段继承“系统设置 → 判断策略”。功能开关仍按各 Bot→群连接独立控制。</p><div class="form-grid">',
      '<label class="span-2">本群审核政策<textarea data-node-field="join_policy" maxlength="4000" rows="5" placeholder="留空继承平台审核政策"></textarea></label>',
      '<label>本群必要关键词（每行一个）<textarea data-node-field="join_required_keywords" rows="4" placeholder="留空继承平台规则"></textarea></label>',
      '<label>本群禁止关键词（每行一个）<textarea data-node-field="join_forbidden_keywords" rows="4" placeholder="留空继承平台规则"></textarea></label>',
      '</div></div></section>'
    ].join("") : "";
    return nodeDetailCommonHtml(existing) + [
      '<section class="node-detail-section"><div class="node-detail-section-title"><span>Resource</span><h3>资源信息</h3></div><div class="form-grid">',
      '<label class="span-2">平台标识<input data-node-field="external_id" maxlength="160" placeholder="群号、chat_id 或知识库 ID"></label>',
      '<label class="span-2">说明<textarea data-node-field="description" maxlength="1000" rows="4" placeholder="用途、负责人和数据边界"></textarea></label>',
      '<label class="span-2">元数据（JSON）<textarea data-node-field="metadata" class="code-input" rows="6"></textarea></label>',
      '</div></section>', groupPolicy
    ].join("");
  }

  function openNodeDetail(nodeId, kind, point) {
    var node = nodeId ? graphNode(nodeId) : null;
    var existing = Boolean(node);
    var nodeKind = existing ? node.kind : kind;
    if (!nodeKind) return;
    var draft = clone(existing ? node.ref : newNodeDraft(nodeKind));
    state.nodeDialog = {
      mode: existing ? "edit" : "create",
      kind: nodeKind,
      originalNodeId: existing ? node.id : "",
      point: clone(point || state.orchestrationMenuPoint),
      draft: draft,
      autoSecretBotId: !existing && nodeKind === "qq_bot" ? draft.id : ""
    };
    $("node-detail-kicker").textContent = existing ? "Edit one node" : "Create one node";
    $("node-detail-title").textContent = (existing ? "配置 " : "新建 ") + nodeKindLabel(nodeKind);
    $("node-detail-intro").textContent = existing
      ? "当前只编辑“" + draft.name + "”。Bot 与群、群与群之间的配置彼此独立。"
      : "保存后才会创建节点；请先确认内部 ID，它在创建后保持不变。";
    var body = $("node-detail-body");
    body.innerHTML = nodeKind === "qq_bot" ? qqNodeDetailHtml(existing) : nodeKind === "feishu_bot" ? feishuNodeDetailHtml(existing) : resourceNodeDetailHtml(existing, nodeKind);
    setNodeDetailValue("id", draft.id);
    setNodeDetailValue("name", draft.name);
    setNodeDetailValue("enabled", draft.enabled);
    if (nodeKind === "qq_bot") {
      setNodeDetailValue("connection_mode", draft.connection_mode || "external");
      ["onebot_base_url", "access_token_file", "request_timeout_seconds", "webui_base_url", "webui_token_file", "webui_public_url", "webui_public_port", "qrcode_path"].forEach(function (name) { setNodeDetailValue(name, draft[name]); });
      setNodeDetailValue("access_token", "");
      setNodeDetailValue("webui_token", "");
      setNodeDetailValue("webhook_secret", "");
      setNodeDetailValue("administrator_qq_ids", listValue(draft.administrator_qq_ids));
      setNodeDetailValue("management_group_ids", listValue(draft.management_group_ids));
      setNodeDetailValue("announcement_actions", listValue(draft.announcement_actions));
      if (!existing) {
        nodeDetailControl("id").addEventListener("input", function () {
          syncNewQQSecretPaths(this.value.trim());
        });
      }
      var bundledOwner = state.config.qq.bots.find(function (bot) {
        return bot.connection_mode === "bundled_napcat" && (!existing || bot.id !== draft.id);
      });
      var bundledOption = nodeDetailControl("connection_mode").querySelector('option[value="bundled_napcat"]');
      bundledOption.disabled = Boolean(bundledOwner);
      if (bundledOwner) {
        bundledOption.textContent = "内置 NapCat（已由 " + bundledOwner.name + " 使用）";
      }
      nodeDetailControl("connection_mode").addEventListener("change", function () {
        applyQQConnectionMode(true);
      });
      var searchSelect = nodeDetailControl("search_feishu_bot_id");
      state.config.feishu.bots.forEach(function (bot) {
        var option = document.createElement("option");
        option.value = bot.id;
        option.textContent = bot.name + " (" + bot.id + ")";
        searchSelect.appendChild(option);
      });
      setNodeDetailValue("search_feishu_bot_id", draft.search_feishu_bot_id || "");
      applyQQConnectionMode(false);
    } else if (nodeKind === "feishu_bot") {
      ["driver", "executable", "timeout_seconds", "max_search_results", "archive_payload_stdin"].forEach(function (name) { setNodeDetailValue(name, draft[name]); });
      setNodeDetailValue("search_prefixes", listValue(draft.search_prefixes));
      setNodeDetailValue("command_templates", pretty(draft.command_templates));
      setNodeDetailValue("extra_environment", pretty(draft.extra_environment));
    } else {
      setNodeDetailValue("external_id", draft.external_id || "");
      setNodeDetailValue("description", draft.description || "");
      setNodeDetailValue("metadata", pretty(draft.metadata));
      if (nodeKind === "qq_group") {
        var joinPolicy = draft.join_approval || {};
        setNodeDetailValue("join_policy", joinPolicy.policy || "");
        setNodeDetailValue("join_required_keywords", joinPolicy.required_keywords === undefined || joinPolicy.required_keywords === null ? "" : listValue(joinPolicy.required_keywords));
        setNodeDetailValue("join_forbidden_keywords", joinPolicy.forbidden_keywords === undefined || joinPolicy.forbidden_keywords === null ? "" : listValue(joinPolicy.forbidden_keywords));
      }
    }
    if (nodeKind === "qq_group" && existing) {
      state.nodeDialog.assignmentDrafts = state.orchestrationEdges.filter(function (edge) {
        return isQQGroupAssignment(edge) && edge.target === node.id;
      }).map(clone);
      var assignmentsSection = document.createElement("section");
      assignmentsSection.className = "node-detail-section node-feature-settings";
      assignmentsSection.innerHTML = '<div class="node-detail-section-heading"><div class="node-detail-section-title"><span>Group features</span><h3>各 Bot 在本群的功能设置</h3></div><p class="muted">“检测申请”仅留档；“执行审核”才会依据本群策略给出判断；自动同意和自动拒绝只会执行达到该连接最低置信度的建议。</p></div>';
      var assignmentList = document.createElement("div");
      assignmentList.className = "node-feature-list";
      if (!state.nodeDialog.assignmentDrafts.length) {
        var emptyAssignments = document.createElement("p");
        emptyAssignments.className = "muted";
        emptyAssignments.textContent = "尚未连接 QQ Bot。先在画布上连接 Bot 与本群，才能配置群功能。";
        assignmentList.appendChild(emptyAssignments);
      } else {
        state.nodeDialog.assignmentDrafts.forEach(function (edge) {
          assignmentList.appendChild(createAssignmentTaskEditor(edge, function () {}));
        });
      }
      assignmentsSection.appendChild(assignmentList);
      body.appendChild(assignmentsSection);
    }
    if (!state.orchestrationSensitiveEditsAllowed) {
      body.querySelectorAll("[data-sensitive]").forEach(function (control) {
        control.disabled = true;
        control.title = "该部署的敏感连接字段已锁定，请在服务器安全配置中开启后编辑";
      });
      var notice = document.createElement("div");
      notice.className = "node-detail-lock-notice";
      notice.textContent = "部署安全策略已锁定连接、Token 与命令执行字段；昵称、启用状态、管理员和非敏感能力仍可修改。";
      body.prepend(notice);
    }
    $("node-detail-dialog").showModal();
    window.setTimeout(function () { nodeDetailControl(existing ? "name" : "id").focus(); }, 20);
  }

  function closeNodeDetail() {
    state.nodeDialog = null;
    if ($("node-detail-dialog").open) $("node-detail-dialog").close();
  }

  function readNodeDetailDraft() {
    var dialogState = state.nodeDialog;
    var draft = clone(dialogState.draft);
    draft.id = nodeDetailControl("id").value.trim();
    draft.name = nodeDetailControl("name").value.trim();
    draft.enabled = nodeDetailControl("enabled").checked;
    var prefix = dialogState.kind === "qq_bot" ? "qq-bot:" : dialogState.kind === "feishu_bot" ? "feishu-bot:" : "";
    var nextNodeId = prefix + draft.id;
    if (nextNodeId !== dialogState.originalNodeId && graphNode(nextNodeId)) throw new Error("内部 ID 已被其他节点使用");
    if (dialogState.kind === "qq_bot") {
      draft.connection_mode = nodeDetailControl("connection_mode").value;
      syncNewQQSecretPaths(draft.id);
      draft.onebot_base_url = nodeDetailControl("onebot_base_url").value.trim();
      draft.access_token = nodeDetailControl("access_token").value || draft.access_token || "";
      draft.access_token_file = nodeDetailControl("access_token_file").value.trim();
      draft.webhook_secret = nodeDetailControl("webhook_secret").value || draft.webhook_secret || "";
      draft.request_timeout_seconds = numberValue(nodeDetailControl("request_timeout_seconds").value, 15);
      draft.webui_base_url = nodeDetailControl("webui_base_url").value.trim();
      draft.webui_token = nodeDetailControl("webui_token").value || draft.webui_token || "";
      draft.webui_token_file = nodeDetailControl("webui_token_file").value.trim();
      draft.webui_public_url = nodeDetailControl("webui_public_url").value.trim();
      draft.webui_public_port = numberValue(nodeDetailControl("webui_public_port").value, 6099);
      draft.qrcode_path = nodeDetailControl("qrcode_path").value.trim();
      draft.administrator_qq_ids = splitList(nodeDetailControl("administrator_qq_ids").value);
      draft.management_group_ids = splitList(nodeDetailControl("management_group_ids").value);
      draft.announcement_actions = splitList(nodeDetailControl("announcement_actions").value);
      draft.search_feishu_bot_id = nodeDetailControl("search_feishu_bot_id").value;
    } else if (dialogState.kind === "feishu_bot") {
      draft.driver = draft.enabled ? nodeDetailControl("driver").value : "disabled";
      draft.executable = nodeDetailControl("executable").value.trim();
      draft.timeout_seconds = numberValue(nodeDetailControl("timeout_seconds").value, 60);
      draft.search_prefixes = splitList(nodeDetailControl("search_prefixes").value);
      draft.max_search_results = numberValue(nodeDetailControl("max_search_results").value, 5);
      draft.command_templates = parseJsonText(nodeDetailControl("command_templates").value, "飞书命令模板");
      draft.archive_payload_stdin = nodeDetailControl("archive_payload_stdin").checked;
      draft.extra_environment = parseJsonText(nodeDetailControl("extra_environment").value, "飞书环境变量");
    } else {
      draft.external_id = nodeDetailControl("external_id").value.trim();
      draft.description = nodeDetailControl("description").value.trim();
      draft.metadata = parseJsonText(nodeDetailControl("metadata").value, "资源元数据");
      if (dialogState.kind === "qq_group") {
        var policy = nodeDetailControl("join_policy").value.trim();
        var requiredText = nodeDetailControl("join_required_keywords").value.trim();
        var forbiddenText = nodeDetailControl("join_forbidden_keywords").value.trim();
        draft.join_approval = policy || requiredText || forbiddenText ? {
          policy: policy || null,
          required_keywords: requiredText ? splitList(requiredText) : null,
          forbidden_keywords: forbiddenText ? splitList(forbiddenText) : null
        } : null;
      }
    }
    return draft;
  }

  function commitNodeDetailDraft(draft) {
    var dialogState = state.nodeDialog;
    var nodeId = (dialogState.kind === "qq_bot" ? "qq-bot:" : dialogState.kind === "feishu_bot" ? "feishu-bot:" : "") + draft.id;
    if (dialogState.mode === "create") {
      if (dialogState.kind === "qq_bot") {
        recordBotIdentityChange("qq", "created", draft.id);
        state.config.qq.bots.push(draft);
      }
      else if (dialogState.kind === "feishu_bot") {
        recordBotIdentityChange("feishu", "created", draft.id);
        state.config.feishu.bots.push(draft);
      }
      else state.config.orchestration.resources.push(draft);
      state.config.orchestration.layout[nodeId] = dialogState.point;
    } else {
      var node = graphNode(dialogState.originalNodeId);
      if (!node) throw new Error("节点已不存在，请刷新编排");
      Object.assign(node.ref, draft);
      (dialogState.assignmentDrafts || []).forEach(function (assignmentDraft) {
        var edge = state.config.orchestration.edges.find(function (item) {
          return item.id === assignmentDraft.id;
        });
        if (edge) Object.assign(edge, clone(assignmentDraft));
      });
    }
    dialogState.mode = "edit";
    dialogState.originalNodeId = nodeId;
    dialogState.draft = clone(draft);
    nodeDetailControl("id").readOnly = true;
    state.orchestrationSelected = nodeId;
    normalizeOrchestration(state.config);
    markOrchestrationDirty();
    renderOrchestration();
    renderOrchestrationInspector(nodeId);
  }

  $("node-detail-close").addEventListener("click", closeNodeDetail);
  $("node-detail-cancel").addEventListener("click", closeNodeDetail);
  $("node-detail-dialog").addEventListener("cancel", function (event) {
    event.preventDefault();
    closeNodeDetail();
  });
  $("node-detail-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    if (!state.nodeDialog) return;
    var button = $("node-detail-save");
    button.disabled = true;
    try {
      var draft = readNodeDetailDraft();
      commitNodeDetailDraft(draft);
      if (await saveOrchestration()) closeNodeDetail();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  function setField(card, name, value) {
    var input = card.querySelector('[data-field="' + name + '"]');
    if (!input) return;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = value == null ? "" : value;
  }

  function field(card, name) { return card.querySelector('[data-field="' + name + '"]'); }

  function sensitiveSettingsLocked() {
    return !state.config || !state.config.gui || !state.config.gui.allow_sensitive_settings_edits;
  }

  function lockSensitiveControl(control) {
    if (!control || !sensitiveSettingsLocked()) return;
    if (control.id === "cfg-llm-url" || control.id === "cfg-llm-key") return;
    control.disabled = true;
    control.title = "该部署安全字段已锁定，请在服务器配置文件中修改";
  }

  function bindTaskCard(taskCard) {
    var master = taskCard.querySelector('[data-role="master"]');
    if (!master) return;
    function refresh() { taskCard.classList.toggle("active", master.checked); }
    master.addEventListener("change", function () {
      if (!master.checked) {
        taskCard.querySelectorAll('.task-detail input[type="checkbox"]').forEach(function (input) { input.checked = false; });
      }
      refresh();
    });
    refresh();
  }

  function populateSettings(c) {
    $("cfg-environment").value = c.app.environment;
    $("cfg-log-level").value = c.app.log_level;
    $("cfg-dry-run").checked = c.app.dry_run;
    $("cfg-admin-token").value = "";
    $("cfg-secure-cookie").checked = c.gui.secure_cookie;
    $("cfg-message-archive-path").value = c.app.message_archive_path || "data/group-message-records";
    $("cfg-llm-driver").value = c.llm.driver;
    $("cfg-llm-model").value = c.llm.model;
    $("cfg-llm-url").value = c.llm.base_url;
    $("cfg-llm-key").value = "";
    $("cfg-llm-key").placeholder = c.llm.api_key === "***"
      ? "已保存的 Key 不会显示；填写新 Key 可覆盖"
      : "填写 API Key（保存后不会显示）";
    $("cfg-llm-test-result").textContent = "";
    $("cfg-llm-timeout").value = c.llm.timeout_seconds;
    $("cfg-llm-retries").value = c.llm.max_retries;
    $("cfg-llm-json").checked = c.llm.json_response_format;
    $("cfg-required-keywords").value = listValue(c.join_approval.required_keywords);
    $("cfg-forbidden-keywords").value = listValue(c.join_approval.forbidden_keywords);
    $("cfg-join-policy").value = c.join_approval.policy;
    $("cfg-mod-policy").value = c.moderation.policy;
    $("cfg-rule-keywords").value = pretty(c.moderation.rule_keywords);
    $("cfg-retention-enabled").checked = c.retention.enabled;
    $("cfg-message-days").value = c.retention.message_days;
    $("cfg-join-days").value = c.retention.join_request_days;
    $("cfg-mod-days").value = c.retention.moderation_run_days;
    $("cfg-audit-days").value = c.retention.audit_days;
    [
      "cfg-environment", "cfg-log-level", "cfg-secure-cookie", "cfg-admin-token",
      "cfg-message-archive-path", "cfg-llm-url", "cfg-llm-key"
    ].forEach(function (id) { lockSensitiveControl($(id)); });
    activateSettingsStep(settingsSteps.indexOf(state.settingsStep) >= 0 ? state.settingsStep : "llm");
  }

  $("cfg-llm-test").addEventListener("click", async function () {
    var button = this;
    button.disabled = true;
    $("cfg-llm-test-result").textContent = "正在请求模型…";
    try {
      var result = await api("/api/gui/settings/llm/test", {
        method: "POST",
        body: {
          driver: $("cfg-llm-driver").value,
          base_url: $("cfg-llm-url").value.trim(),
          api_key: $("cfg-llm-key").value,
          model: $("cfg-llm-model").value.trim(),
          timeout_seconds: numberValue($("cfg-llm-timeout").value, 30),
          json_response_format: $("cfg-llm-json").checked
        }
      });
      $("cfg-llm-test-result").textContent = result.message || "模型连接正常";
      showToast("模型连接测试通过");
    } catch (error) {
      $("cfg-llm-test-result").textContent = "测试失败：" + error.message;
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  function updateSettingsReview() {
    if (!state.config) return;
    var target = $("settings-review");
    target.replaceChildren();
    [
      ["配置边界", "仅平台设置", "Bot、群、连接与群级事务由资源编排独立保存"],
      ["模型驱动", $("cfg-llm-driver").value, $("cfg-llm-model").value || "未指定模型"],
      ["消息归档", $("cfg-message-archive-path").value || "data/group-message-records", "是否记录由每条 Bot→群连接决定"],
      ["共享策略", "入群与风控", "所有 Bot 共用平台级判断文本"],
      ["安全模式", $("cfg-dry-run").checked ? "安全演练已开启" : "真实出站动作已允许", "保存后立即生效"],
      ["数据保留", $("cfg-retention-enabled").checked ? $("cfg-message-days").value + " 天" : "不自动清理", "SQLite 与按日 JSONL 使用同一期限"]
    ].forEach(function (item) {
      var card = document.createElement("article");
      card.innerHTML = "<span></span><strong></strong><small></small>";
      card.querySelector("span").textContent = item[0];
      card.querySelector("strong").textContent = item[1];
      card.querySelector("small").textContent = item[2];
      target.appendChild(card);
    });
  }

  async function loadSettings() {
    var data = await api("/api/gui/settings");
    state.config = data.config;
    state.settingsRevision = data.revision || "";
    populateSettings(state.config);
    state.settingsDirty = false;
    $("settings-state").textContent = sensitiveSettingsLocked()
      ? "配置已同步；部署安全字段已锁定"
      : "配置已同步";
  }

  $("settings-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    var button = event.submitter;
    button.disabled = true;
    $("settings-state").textContent = "正在校验并热加载…";
    try {
      var c = clone(state.config);
      c.app.environment = $("cfg-environment").value.trim();
      c.app.log_level = $("cfg-log-level").value;
      c.app.dry_run = $("cfg-dry-run").checked;
      c.app.message_archive_path = $("cfg-message-archive-path").value.trim() || "data/group-message-records";
      c.app.admin_api_token = $("cfg-admin-token").value;
      c.gui.secure_cookie = $("cfg-secure-cookie").checked;
      c.llm.driver = $("cfg-llm-driver").value;
      c.llm.model = $("cfg-llm-model").value.trim();
      c.llm.base_url = $("cfg-llm-url").value.trim();
      c.llm.api_key = $("cfg-llm-key").value;
      c.llm.timeout_seconds = numberValue($("cfg-llm-timeout").value, 60);
      c.llm.max_retries = numberValue($("cfg-llm-retries").value, 2);
      c.llm.json_response_format = $("cfg-llm-json").checked;
      c.join_approval.required_keywords = splitList($("cfg-required-keywords").value);
      c.join_approval.forbidden_keywords = splitList($("cfg-forbidden-keywords").value);
      c.join_approval.policy = $("cfg-join-policy").value;
      c.moderation.policy = $("cfg-mod-policy").value;
      c.moderation.rule_keywords = parseJsonText($("cfg-rule-keywords").value, "离线关键词规则");
      c.retention.enabled = $("cfg-retention-enabled").checked;
      c.retention.message_days = numberValue($("cfg-message-days").value, 30);
      c.retention.join_request_days = numberValue($("cfg-join-days").value, 180);
      c.retention.moderation_run_days = numberValue($("cfg-mod-days").value, 365);
      c.retention.audit_days = numberValue($("cfg-audit-days").value, 365);
      var result = await api("/api/gui/settings", {
        method: "PUT",
        body: { config: c, revision: state.settingsRevision }
      });
      state.settingsRevision = result.revision || state.settingsRevision;
      state.settingsDirty = false;
      $("settings-state").textContent = result.restart_required.length ? "已保存；这些字段需重启：" + result.restart_required.join(", ") : "已保存并热加载";
      showToast("配置已保存");
      await loadSettings();
    } catch (error) {
      $("settings-state").textContent = error.status === 409 ? "其他会话已更新配置，请刷新后重新应用" : "保存失败";
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  function recordTitle(kind, item) {
    var bot = item.bot_id ? "[" + item.bot_id + "] " : "";
    if (kind === "joins") return bot + "群 " + item.group_id + " · 申请人 " + item.user_id;
    if (kind === "messages") return bot + "群 " + item.group_id + " · 发言人 " + (item.sender_name || item.user_id);
    if (kind === "moderation") return bot + "群 " + item.group_id + " · 风险 " + item.max_risk;
    if (kind === "announcements") return bot + "群 " + item.group_id + " · " + (item.title || item.announcement_id);
    return item.action + " · " + item.status;
  }

  function recordTime(item) { return item.received_at || item.created_at || item.last_seen_at || item.window_end || ""; }

  function recordPreview(kind, item) {
    if (kind === "joins") return item.comment || item.reason || item.decision || "等待处理";
    if (kind === "messages") return item.text || "空消息";
    if (kind === "moderation") {
      var result = item.result_json || {};
      return result.summary || item.status || (item.max_risk != null ? "最高风险 " + item.max_risk : "分析记录");
    }
    if (kind === "announcements") return item.content || item.sync_status || "公告记录";
    return [item.subject_type, item.subject_id].filter(Boolean).join(" / ") || "审计事件";
  }

  async function populateRecordBotFilter() {
    var select = $("record-bot");
    var currentValue = select.value;
    var settings = await api("/api/gui/orchestration");
    var config = settings.config;
    normalizeBotConfig(config);
    select.replaceChildren();
    var all = document.createElement("option");
    all.value = "";
    all.textContent = "全部 Bot";
    select.appendChild(all);
    config.qq.bots.forEach(function (bot) {
      var option = document.createElement("option");
      option.value = bot.id;
      option.textContent = bot.name + " · " + bot.id;
      select.appendChild(option);
    });
    if (Array.from(select.options).some(function (option) { return option.value === currentValue; })) select.value = currentValue;
  }

  function updateRecordFilterAvailability() {
    var audit = $("record-kind").value === "audit";
    $("record-bot").disabled = audit;
    $("record-group").disabled = audit;
  }

  function updateRecordPagination(data) {
    var count = data.records.length;
    var start = count ? data.offset + 1 : 0;
    var end = data.offset + count;
    state.recordsHasMore = Boolean(data.has_more);
    $("records-range").textContent = count ? "显示第 " + start + "–" + end + " 条" : "0 条记录";
    $("records-page").textContent = "第 " + (Math.floor(data.offset / data.limit) + 1) + " 页";
    $("records-prev").disabled = data.offset <= 0;
    $("records-next").disabled = !data.has_more;
  }

  async function loadRecords(resetOffset) {
    if (resetOffset) state.recordsOffset = 0;
    var kind = $("record-kind").value;
    updateRecordFilterAvailability();
    if ($("record-bot").options.length <= 1) await populateRecordBotFilter();
    var limit = numberValue($("record-limit").value, 50);
    var parameters = new URLSearchParams({
      limit: String(limit),
      offset: String(state.recordsOffset)
    });
    if (kind !== "audit" && $("record-bot").value) parameters.set("bot_id", $("record-bot").value);
    if (kind !== "audit" && $("record-group").value.trim()) parameters.set("group_id", $("record-group").value.trim());
    if ($("record-query").value.trim()) parameters.set("search", $("record-query").value.trim());
    var data = await api("/api/gui/records/" + kind + "?" + parameters.toString());
    var target = $("records-table");
    target.replaceChildren();
    updateRecordPagination(data);
    if (!data.records.length) {
      var empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = state.recordsOffset ? "这一页没有记录，请返回上一页。" : "没有符合当前筛选条件的记录。";
      target.appendChild(empty);
      return;
    }
    var list = document.createElement("div");
    list.className = "record-list";
    data.records.forEach(function (item) {
      var card = document.createElement("details");
      card.className = "record-item";
      var summary = document.createElement("summary");
      summary.className = "record-summary";
      var summaryText = document.createElement("div");
      var title = document.createElement("strong");
      var preview = document.createElement("small");
      title.textContent = recordTitle(kind, item);
      preview.textContent = recordPreview(kind, item);
      summaryText.append(title, preview);
      var timeNode = document.createElement("span");
      timeNode.textContent = recordTime(item);
      var body = document.createElement("pre");
      body.textContent = JSON.stringify(item, null, 2);
      summary.append(summaryText, timeNode);
      card.append(summary, body);
      list.appendChild(card);
    });
    target.appendChild(list);
  }

  function reloadRecordsFromStart() {
    loadRecords(true).catch(function (error) { showToast(error.message, true); });
  }

  ["record-kind", "record-bot", "record-limit"].forEach(function (id) {
    $(id).addEventListener("change", reloadRecordsFromStart);
  });
  $("record-group").addEventListener("change", reloadRecordsFromStart);
  $("record-query").addEventListener("input", function () {
    window.clearTimeout(state.recordsSearchTimer);
    state.recordsSearchTimer = window.setTimeout(reloadRecordsFromStart, 280);
  });
  $("records-refresh").addEventListener("click", function () {
    loadRecords(false).catch(function (error) { showToast(error.message, true); });
  });
  $("records-reset").addEventListener("click", function () {
    $("record-bot").value = "";
    $("record-group").value = "";
    $("record-query").value = "";
    reloadRecordsFromStart();
  });
  $("records-prev").addEventListener("click", function () {
    var limit = numberValue($("record-limit").value, 50);
    state.recordsOffset = Math.max(0, state.recordsOffset - limit);
    loadRecords(false).catch(function (error) { showToast(error.message, true); });
  });
  $("records-next").addEventListener("click", function () {
    if (!state.recordsHasMore) return;
    state.recordsOffset += numberValue($("record-limit").value, 50);
    loadRecords(false).catch(function (error) { showToast(error.message, true); });
  });

  function userTimestamp(value) {
    var date = new Date(value);
    return Number.isNaN(date.getTime()) ? "未知" : date.toLocaleString("zh-CN", { hour12: false });
  }

  function userAction(label, className, action) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "button " + className;
    button.textContent = label;
    button.addEventListener("click", action);
    return button;
  }

  function openUserPasswordDialog(username) {
    $("user-password-target").value = username;
    $("user-password-name").textContent = username;
    $("user-password-form").reset();
    $("user-password-target").value = username;
    $("user-password-dialog").showModal();
    window.setTimeout(function () { $("user-reset-password").focus(); }, 30);
  }

  function closeUserPasswordDialog() {
    if ($("user-password-dialog").open) $("user-password-dialog").close();
  }

  function renderUsers(users) {
    var target = $("user-list");
    target.replaceChildren();
    users.forEach(function (user) {
      var card = document.createElement("article");
      card.className = "user-card";

      var identity = document.createElement("div");
      identity.className = "user-card-identity";
      var avatar = document.createElement("span");
      avatar.className = "avatar user-card-avatar";
      avatar.textContent = (user.username.charAt(0) || "U").toUpperCase();
      var identityText = document.createElement("div");
      var name = document.createElement("strong");
      name.textContent = user.username;
      var role = document.createElement("small");
      role.textContent = user.role === "admin" ? "管理员 · 完整权限" : "子用户 · Bot 管理权限";
      identityText.append(name, role);
      identity.append(avatar, identityText);

      var status = document.createElement("div");
      status.className = "user-card-status";
      var passwordState = document.createElement("span");
      passwordState.className = "pill" + (user.must_change_password ? " attention" : "");
      passwordState.textContent = user.must_change_password ? "等待首次改密" : "密码已设置";
      var sessions = document.createElement("small");
      sessions.textContent = user.active_sessions + " 个活动会话 · 更新于 " + userTimestamp(user.updated_at);
      status.append(passwordState, sessions);

      var actions = document.createElement("div");
      actions.className = "button-row user-card-actions";
      if (user.role === "operator") {
        actions.append(
          userAction("重置密码", "secondary compact", function () { openUserPasswordDialog(user.username); }),
          userAction("删除", "danger compact", async function () {
            if (!await confirmAction({
              title: "删除子用户？",
              message: "删除“" + user.username + "”后，其所有登录会话会立即失效。",
              confirmText: "删除用户",
              danger: true
            })) return;
            try {
              await api("/api/gui/users/" + encodeURIComponent(user.username), { method: "DELETE" });
              showToast("已删除子用户 " + user.username);
              await loadUsers();
            } catch (error) {
              showToast(error.message, true);
            }
          })
        );
      } else {
        var protectedLabel = document.createElement("span");
        protectedLabel.className = "protected-user-label";
        protectedLabel.textContent = "受保护账号";
        actions.appendChild(protectedLabel);
      }

      card.append(identity, status, actions);
      target.appendChild(card);
    });
  }

  async function loadUsers() {
    var data = await api("/api/gui/users");
    renderUsers(data.users || []);
  }

  $("user-create-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    var password = $("user-create-password").value;
    if (password !== $("user-create-confirm").value) {
      showToast("两次输入的初始密码不一致", true);
      return;
    }
    var button = event.submitter;
    button.disabled = true;
    try {
      var username = $("user-create-username").value.trim();
      await api("/api/gui/users", { method: "POST", body: { username: username, password: password } });
      $("user-create-form").reset();
      showToast("子用户 " + username + " 已创建");
      await loadUsers();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  $("users-refresh").addEventListener("click", function () {
    loadUsers().catch(function (error) { showToast(error.message, true); });
  });
  $("user-password-close").addEventListener("click", closeUserPasswordDialog);
  $("user-password-cancel").addEventListener("click", closeUserPasswordDialog);
  $("user-password-dialog").addEventListener("cancel", function (event) {
    event.preventDefault();
    closeUserPasswordDialog();
  });
  $("user-password-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    var username = $("user-password-target").value;
    var password = $("user-reset-password").value;
    if (password !== $("user-reset-confirm").value) {
      showToast("两次输入的新密码不一致", true);
      return;
    }
    var button = event.submitter;
    button.disabled = true;
    try {
      await api("/api/gui/users/" + encodeURIComponent(username) + "/password", {
        method: "PUT",
        body: { password: password }
      });
      closeUserPasswordDialog();
      showToast("已重置 " + username + " 的密码");
      await loadUsers();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  async function commandCreateResource(kind) {
    await switchView("integrations");
    if (state.activeView !== "integrations") return;
    var viewport = $("orchestration-viewport");
    state.orchestrationMenuPoint = {
      x: viewport.scrollLeft + Math.max(140, viewport.clientWidth / 2 - 110),
      y: viewport.scrollTop + Math.max(90, viewport.clientHeight / 2 - 55)
    };
    createOrchestrationNode(kind);
  }

  async function commandRunJob(job) {
    await switchView("dashboard");
    if (state.activeView === "dashboard") await runJob(job, "", null);
  }

  function commandDefinitions() {
    var commands = [
      { label: "打开运行概览", detail: "查看计数、诊断和手动任务", category: "导航", action: function () { return switchView("dashboard"); } },
      { label: "打开资源编排", detail: "管理 Bot、群、知识库和连接", category: "导航", action: function () { return switchView("integrations"); } },
      { label: "打开系统设置", detail: "模型、策略、存储和系统安全", category: "导航", action: function () { return switchView("settings"); } },
      { label: "打开记录与审计", detail: "搜索消息、公告、分析和审计事件", category: "导航", action: function () { return switchView("records"); } },
      { label: "新建 QQ Bot", detail: "在资源编排中创建 OneBot 账号", category: "编排", action: function () { return commandCreateResource("qq_bot"); } },
      { label: "新建飞书 Bot", detail: "在资源编排中创建飞书 CLI 账号", category: "编排", action: function () { return commandCreateResource("feishu_bot"); } },
      { label: "新建 QQ 群", detail: "创建可管理或监听的 QQ 群节点", category: "编排", action: function () { return commandCreateResource("qq_group"); } },
      { label: "新建飞书群", detail: "创建飞书协作目标", category: "编排", action: function () { return commandCreateResource("feishu_group"); } },
      { label: "新建知识库", detail: "创建飞书、Notion 或本地知识资源", category: "编排", action: function () { return commandCreateResource("knowledge_base"); } },
      { label: "运行消息分析", detail: "立即执行所有已启用的分析事务", category: "任务", action: function () { return commandRunJob("moderation"); } },
      { label: "同步全部公告", detail: "立即抓取并归档所有启用群的公告", category: "任务", action: function () { return commandRunJob("announcements"); } },
      { label: "清理过期数据", detail: "立即应用消息和审计保留策略", category: "任务", action: function () { return commandRunJob("maintenance"); } },
      { label: "切换深浅主题", detail: "在纯黑和纯白工作台之间切换", category: "界面", action: function () { applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true); } },
      { label: "刷新当前页面", detail: "重新读取当前页面的最新状态", category: "界面", action: function () { return switchView(state.activeView); } }
    ];
    if (state.role === "admin") {
      commands.splice(4, 0, {
        label: "打开用户管理",
        detail: "创建、重置或删除平台子用户",
        category: "导航",
        action: function () { return switchView("users"); }
      });
    }
    return commands;
  }

  function filteredCommands() {
    var query = $("command-query").value.trim().toLowerCase();
    return commandDefinitions().filter(function (command) {
      return !query || [command.label, command.detail, command.category].join(" ").toLowerCase().indexOf(query) >= 0;
    });
  }

  function renderCommandPalette() {
    var commands = filteredCommands();
    state.commandIndex = Math.max(0, Math.min(state.commandIndex, Math.max(0, commands.length - 1)));
    var target = $("command-results");
    target.replaceChildren();
    commands.forEach(function (command, index) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "command-item" + (index === state.commandIndex ? " active" : "");
      button.innerHTML = '<span class="command-category"></span><div><strong></strong><small></small></div><span class="command-arrow">↵</span>';
      button.querySelector(".command-category").textContent = command.category;
      button.querySelector("strong").textContent = command.label;
      button.querySelector("small").textContent = command.detail;
      button.addEventListener("pointerenter", function () {
        state.commandIndex = index;
        target.querySelectorAll(".command-item").forEach(function (item, itemIndex) {
          item.classList.toggle("active", itemIndex === index);
        });
      });
      button.addEventListener("click", function () { executeCommand(command); });
      target.appendChild(button);
    });
    if (!commands.length) {
      var empty = document.createElement("div");
      empty.className = "command-empty";
      empty.textContent = "没有匹配的命令";
      target.appendChild(empty);
    }
  }

  function openCommandPalette() {
    if ($("app-view").classList.contains("hidden")) return;
    var openDialog = document.querySelector("dialog[open]");
    if (openDialog && openDialog !== $("command-dialog")) return;
    state.commandIndex = 0;
    $("command-query").value = "";
    renderCommandPalette();
    $("command-dialog").showModal();
    window.setTimeout(function () { $("command-query").focus(); }, 20);
  }

  function closeCommandPalette() {
    if ($("command-dialog").open) $("command-dialog").close();
  }

  async function executeCommand(command) {
    closeCommandPalette();
    try {
      await command.action();
    } catch (error) {
      showToast(error.message, true);
    }
  }

  $("command-button").addEventListener("click", openCommandPalette);
  $("command-close").addEventListener("click", closeCommandPalette);
  $("command-query").addEventListener("input", function () { state.commandIndex = 0; renderCommandPalette(); });
  $("command-query").addEventListener("keydown", function (event) {
    var commands = filteredCommands();
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (!commands.length) return;
      event.preventDefault();
      var direction = event.key === "ArrowDown" ? 1 : -1;
      state.commandIndex = (state.commandIndex + direction + commands.length) % commands.length;
      renderCommandPalette();
    } else if (event.key === "Enter" && commands[state.commandIndex]) {
      event.preventDefault();
      executeCommand(commands[state.commandIndex]);
    }
  });
  $("command-dialog").addEventListener("cancel", function (event) { event.preventDefault(); closeCommandPalette(); });
  document.addEventListener("keydown", function (event) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      if ($("command-dialog").open) closeCommandPalette();
      else openCommandPalette();
    }
  });

  initialize();
})();
