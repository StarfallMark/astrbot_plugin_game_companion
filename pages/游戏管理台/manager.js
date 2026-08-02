(() => {
  "use strict";

  const body = document.getElementById("roomsBody");
  const emptyState = document.getElementById("emptyState");
  const assignDialog = document.getElementById("assignDialog");
  const confirmDialog = document.getElementById("confirmDialog");
  const toast = document.getElementById("toast");
  let rooms = [];
  let tunnel = {};
  let xiangqiEngine = {};
  let toastTimer = 0;
  let refreshTimer = 0;
  let confirmResolver = null;

  function icons() {
    if (window.lucide?.createIcons) window.lucide.createIcons();
  }

  function showToast(message) {
    window.clearTimeout(toastTimer);
    toast.textContent = message;
    toast.hidden = false;
    toastTimer = window.setTimeout(() => { toast.hidden = true; }, 2600);
  }

  function resolveConfirmation(accepted) {
    const resolver = confirmResolver;
    confirmResolver = null;
    if (confirmDialog.open) confirmDialog.close();
    if (resolver) resolver(Boolean(accepted));
  }

  function confirmAction({ title, message, label = "确认", danger = false }) {
    if (confirmResolver) resolveConfirmation(false);
    document.getElementById("confirmTitle").textContent = title;
    document.getElementById("confirmMessage").textContent = message;
    const proceed = document.getElementById("confirmProceed");
    proceed.querySelector("span").textContent = label;
    proceed.classList.toggle("danger", danger);
    confirmDialog.showModal();
    icons();
    return new Promise((resolve) => { confirmResolver = resolve; });
  }

  async function bridge() {
    for (let index = 0; index < 60; index += 1) {
      const candidate = window.AstrBotPluginPage;
      if (candidate?.apiGet && candidate?.apiPost) {
        if (candidate.ready) await candidate.ready();
        return candidate;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 100));
    }
    throw new Error("请从 AstrBot 插件拓展页打开游戏管理台");
  }

  async function endpoint(method, path, payload = {}) {
    const api = await bridge();
    const result = method === "GET"
      ? await api.apiGet(`page/${path}`)
      : await api.apiPost(`page/${path}`, payload);
    if (result?.status === "error") throw new Error(result.message || "请求失败");
    return result?.data ?? result;
  }

  function statusLabel(status) {
    return {
      waiting: "等待玩家", setup: "等待开局", active: "对局中", paused: "已暂停",
      finished: "本局结束", rematch_pending: "等待回应", closed: "已关闭",
    }[status] || status || "未知";
  }

  function limitLabel(value) {
    return Number(value) === 0 ? "无限制" : `上限 ${value}`;
  }

  function createText(tag, text, className = "") {
    const node = document.createElement(tag);
    node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function actionButton(icon, title, action, room, className = "") {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `row-button ${className}`;
    button.title = title;
    button.setAttribute("aria-label", title);
    button.innerHTML = `<i data-lucide="${icon}"></i>`;
    button.addEventListener("click", () => runRoomAction(room.room_id, action));
    return button;
  }

  function renderRooms() {
    body.replaceChildren();
    emptyState.hidden = rooms.length > 0;
    rooms.forEach((room) => {
      const row = document.createElement("tr");
      const identity = document.createElement("td");
      identity.append(createText("strong", room.room_id, "room-code"));
      identity.append(createText("small", room.admin_room ? "管理员房间" : "普通房间"));

      const game = document.createElement("td");
      const gameSelect = document.createElement("select");
      gameSelect.className = "game-select";
      gameSelect.title = "切换游戏";
      [["gomoku", "五子棋"], ["xiangqi", "中国象棋"]].forEach(([value, label]) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        option.selected = room.game_type === value;
        gameSelect.appendChild(option);
      });
      gameSelect.addEventListener("change", async () => {
        const target = gameSelect.value;
        const active = ["active", "paused"].includes(room.status);
        if (active && !await confirmAction({
          title: "放弃当前对局",
          message: "当前对局尚未结束。切换游戏将放弃本局，但会保留房间、玩家和比分。",
          label: "放弃并切换",
          danger: true,
        })) {
          gameSelect.value = room.game_type;
          return;
        }
        gameSelect.disabled = true;
        try {
          await endpoint("POST", "room/action", {
            room_id: room.room_id,
            action: "switch_game",
            game_type: target,
            confirm_abandon: active,
          });
          showToast("房间游戏已切换");
          await loadRooms();
        } catch (error) {
          gameSelect.value = room.game_type;
          gameSelect.disabled = false;
          showToast(error?.message || "切换失败");
        }
      });
      game.appendChild(gameSelect);

      const origin = document.createElement("td");
      origin.append(createText("strong", room.source === "group" ? `群聊 ${room.group_id || ""}` : "私聊"));
      origin.append(createText("small", `${room.creator_name || "创建者"} · ${room.creator_qq || "未知 QQ"}`));

      const members = document.createElement("td");
      const visitorList = document.createElement("div");
      visitorList.className = "visitor-list";
      (room.visitors || []).forEach((visitor) => {
        const chip = document.createElement("span");
        chip.className = `visitor ${visitor.online ? "online" : ""} ${visitor.is_player ? "player" : ""}`;
        const dot = document.createElement("i");
        chip.append(dot, document.createTextNode(`${visitor.number} 号${visitor.is_player ? " · 玩家" : ""}`));
        const kick = document.createElement("button");
        kick.type = "button";
        kick.className = "visitor-kick";
        kick.title = `移出 ${visitor.number} 号`;
        kick.setAttribute("aria-label", `移出 ${visitor.number} 号`);
        kick.innerHTML = '<i data-lucide="x"></i>';
        kick.addEventListener("click", async () => {
          if (!await confirmAction({
            title: "移出访客",
            message: `确认将 ${visitor.number} 号移出房间？如果该访客正在玩家席，本局将被重置。`,
            label: "确认移出",
            danger: true,
          })) return;
          try {
            await endpoint("POST", "room/action", {
              room_id: room.room_id, action: "kick", visitor_number: visitor.number,
            });
            await loadRooms();
          } catch (error) {
            showToast(error?.message || "移出失败");
          }
        });
        chip.appendChild(kick);
        visitorList.appendChild(chip);
      });
      members.appendChild(visitorList);

      const player = document.createElement("td");
      player.append(createText("strong", room.player_number ? `${room.player_number} 号` : "未安排"));
      player.append(createText("small", room.player_qq ? `QQ ${room.player_qq}` : "尚未绑定 QQ"));

      const state = document.createElement("td");
      state.append(createText("span", statusLabel(room.status), `status ${room.status}`));
      state.append(createText("small", `棋力：${{ easy: "简单", normal: "普通", hard: "困难" }[room.difficulty] || "普通"}`));

      const actions = document.createElement("td");
      const actionList = document.createElement("div");
      actionList.className = "row-actions";
      const assign = document.createElement("button");
      assign.type = "button";
      assign.className = "row-button";
      assign.title = "安排玩家";
      assign.setAttribute("aria-label", "安排玩家");
      assign.innerHTML = '<i data-lucide="user-check"></i>';
      assign.addEventListener("click", () => openAssign(room));
      actionList.appendChild(assign);
      if (room.player_number) actionList.appendChild(actionButton("user-round-minus", "移到观众席", "demote", room));
      if (room.status === "active") actionList.appendChild(actionButton("pause", "暂停", "pause", room));
      if (room.status === "paused") actionList.appendChild(actionButton("play", "继续", "resume", room));
      actionList.appendChild(actionButton("x", "关闭房间", "close", room, "danger"));
      actions.appendChild(actionList);
      row.append(identity, game, origin, members, player, state, actions);
      body.appendChild(row);
    });
    icons();
  }

  function renderMetrics(data) {
    const groupRooms = rooms.filter((room) => room.source === "group");
    const privateRooms = rooms.filter((room) => room.source === "private");
    const online = rooms.reduce(
      (count, room) => count + (room.visitors || []).filter((visitor) => visitor.online).length,
      0,
    );
    document.getElementById("roomCount").textContent = rooms.length;
    document.getElementById("groupCount").textContent = groupRooms.length;
    document.getElementById("privateCount").textContent = privateRooms.length;
    document.getElementById("visitorCount").textContent = online;
    document.getElementById("groupLimit").textContent = limitLabel(data.limits?.group);
    document.getElementById("privateLimit").textContent = limitLabel(data.limits?.private);
  }

  function renderService(data) {
    const badge = document.getElementById("serviceBadge");
    const action = document.getElementById("tunnelAction");
    tunnel = data.tunnel || {};
    if (data.server?.public_base_url) {
      badge.textContent = "固定 HTTPS";
      badge.className = "service-badge online";
      action.disabled = true;
      action.querySelector("span").textContent = "固定地址已配置";
      document.getElementById("tunnelUrl").textContent = data.server.public_base_url;
    } else if (tunnel.running) {
      badge.textContent = "临时公网已开启";
      badge.className = "service-badge online";
      action.disabled = rooms.length > 0;
      action.dataset.running = "true";
      action.querySelector("span").textContent = rooms.length ? "活动房间使用中" : "停止临时访问";
      document.getElementById("tunnelUrl").textContent = tunnel.url || "";
    } else {
      badge.textContent = data.server?.running ? "仅本机" : "按需启动";
      badge.className = "service-badge";
      action.disabled = !tunnel.installed;
      action.dataset.running = "false";
      action.querySelector("span").textContent = tunnel.installed ? "启动临时访问" : "未安装 cloudflared";
      document.getElementById("tunnelUrl").textContent = "";
    }
  }

  function renderEngine(data) {
    xiangqiEngine = data.xiangqi_engine || {};
    const badge = document.getElementById("engineBadge");
    const detail = document.getElementById("engineDetail");
    const action = document.getElementById("engineAction");
    if (xiangqiEngine.available) {
      badge.textContent = xiangqiEngine.running ? "运行中" : "已安装";
      badge.className = "service-badge online";
      const version = xiangqiEngine.version || "版本未知";
      detail.textContent = `${version} · ${xiangqiEngine.path || xiangqiEngine.platform || ""}`;
    } else {
      badge.textContent = "未安装";
      badge.className = xiangqiEngine.error ? "service-badge error" : "service-badge";
      detail.textContent = xiangqiEngine.error || `适用版本：${xiangqiEngine.platform || "自动检测"}`;
    }
    action.disabled = !xiangqiEngine.allow_download || xiangqiEngine.configured;
    action.title = xiangqiEngine.configured
      ? "当前使用插件配置中指定的引擎"
      : (xiangqiEngine.allow_download ? "从 Pikafish 官方发行版安装或更新" : "插件配置已禁止下载");
  }

  async function loadRooms() {
    window.clearTimeout(refreshTimer);
    try {
      const data = await endpoint("GET", "rooms");
      rooms = Array.isArray(data.rooms) ? data.rooms : [];
      renderMetrics(data);
      renderService(data);
      renderEngine(data);
      renderRooms();
      document.getElementById("lastUpdated").textContent = `更新于 ${new Date().toLocaleTimeString()}`;
      refreshTimer = window.setTimeout(loadRooms, 2500);
    } catch (error) {
      document.getElementById("serviceBadge").textContent = "读取失败";
      document.getElementById("serviceBadge").className = "service-badge error";
      showToast(error?.message || "无法读取房间状态");
      refreshTimer = window.setTimeout(loadRooms, 5000);
    }
  }

  function openAssign(room) {
    const select = document.getElementById("assignVisitor");
    select.replaceChildren();
    (room.visitors || []).forEach((visitor) => {
      const option = document.createElement("option");
      option.value = visitor.number;
      option.textContent = `${visitor.number} 号${visitor.online ? " · 在线" : " · 离线"}`;
      select.appendChild(option);
    });
    document.getElementById("assignRoomId").value = room.room_id;
    document.getElementById("assignRoomLabel").textContent = `房间 ${room.room_id}`;
    document.getElementById("assignQq").value = room.player_qq || "";
    document.getElementById("confirmAssign").disabled = !(room.visitors || []).length;
    assignDialog.showModal();
    icons();
  }

  async function confirmAssign() {
    const roomId = document.getElementById("assignRoomId").value;
    const visitorNumber = Number(document.getElementById("assignVisitor").value);
    const playerQq = document.getElementById("assignQq").value.trim();
    if (!/^\d+$/.test(playerQq)) {
      showToast("请输入有效的玩家 QQ 号");
      return;
    }
    try {
      await endpoint("POST", "room/action", {
        action: "assign", room_id: roomId, visitor_number: visitorNumber, player_qq: playerQq,
      });
      assignDialog.close();
      showToast("玩家已安排");
      await loadRooms();
    } catch (error) {
      showToast(error?.message || "安排失败");
    }
  }

  async function runRoomAction(roomId, action) {
    if (action === "close" && !await confirmAction({
      title: "关闭房间",
      message: "确认关闭并销毁这个房间？房间链接会立即失效，未完成的对局无法恢复。",
      label: "关闭房间",
      danger: true,
    })) return;
    try {
      await endpoint("POST", "room/action", { room_id: roomId, action });
      showToast("操作已完成");
      await loadRooms();
    } catch (error) {
      showToast(error?.message || "操作失败");
    }
  }

  async function toggleTunnel() {
    const action = tunnel.running ? "tunnel/stop" : "tunnel/start";
    try {
      await endpoint("POST", action);
      showToast(tunnel.running ? "临时公网访问已停止" : "临时公网访问已启动");
      await loadRooms();
    } catch (error) {
      showToast(error?.message || "无法切换访问通道");
    }
  }

  async function installEngine() {
    if (!await confirmAction({
      title: "安装中国象棋引擎",
      message: "将通过已配置的代理下载 Pikafish 官方发行包，校验后安装到插件数据目录。",
      label: "开始安装",
    })) return;
    const action = document.getElementById("engineAction");
    action.disabled = true;
    action.querySelector("span").textContent = "正在安装";
    try {
      await endpoint("POST", "xiangqi/install");
      showToast("Pikafish 已安装并通过启动检查");
      await loadRooms();
    } catch (error) {
      showToast(error?.message || "引擎安装失败");
    } finally {
      action.querySelector("span").textContent = "安装 / 更新";
      action.disabled = !xiangqiEngine.allow_download || xiangqiEngine.configured;
    }
  }

  document.getElementById("refreshAction").addEventListener("click", loadRooms);
  document.getElementById("tunnelAction").addEventListener("click", toggleTunnel);
  document.getElementById("engineAction").addEventListener("click", installEngine);
  document.getElementById("confirmAssign").addEventListener("click", confirmAssign);
  document.getElementById("confirmProceed").addEventListener("click", () => resolveConfirmation(true));
  confirmDialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    resolveConfirmation(false);
  });
  confirmDialog.addEventListener("close", () => {
    if (confirmResolver) resolveConfirmation(false);
  });
  icons();
  loadRooms();
})();
