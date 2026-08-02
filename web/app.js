(() => {
  "use strict";

  const match = window.location.pathname.match(/\/room\/([A-Za-z0-9_-]{24,80})\/?$/);
  const accessToken = match ? match[1] : "";
  const storageKey = `game-companion:${accessToken}:visitor`;
  const board = document.getElementById("board");
  const context = board.getContext("2d");
  const toast = document.getElementById("toast");
  let visitorToken = accessToken ? window.localStorage.getItem(storageKey) || "" : "";
  let room = null;
  let selectedSide = "human_black";
  let pollTimer = 0;
  let toastTimer = 0;
  let busy = false;
  let pendingMove = null;

  function icons() {
    if (window.lucide?.createIcons) window.lucide.createIcons();
  }

  function endpoint(action, query = "") {
    return new URL(`../../api/room/${accessToken}/${action}${query}`, window.location.href).toString();
  }

  function showToast(message) {
    window.clearTimeout(toastTimer);
    toast.textContent = message;
    toast.hidden = false;
    toastTimer = window.setTimeout(() => { toast.hidden = true; }, 2600);
  }

  async function request(method, action, payload = {}) {
    const response = await window.fetch(endpoint(action), {
      method,
      headers: method === "POST" ? { "Content-Type": "application/json" } : {},
      body: method === "POST" ? JSON.stringify(payload) : undefined,
      cache: "no-store",
    });
    let data = null;
    try { data = await response.json(); } catch (_error) { data = null; }
    if (!response.ok || data?.status === "error") {
      throw new Error(data?.message || data?.error || (await response.text().catch(() => "")) || "请求失败");
    }
    return data?.data ?? data;
  }

  async function join() {
    if (!accessToken) throw new Error("房间链接无效");
    const data = await request("POST", "join", { visitor_token: visitorToken });
    visitorToken = String(data.visitor_token || "");
    window.localStorage.setItem(storageKey, visitorToken);
    room = data.room;
    render();
  }

  async function poll() {
    window.clearTimeout(pollTimer);
    if (!visitorToken) return;
    try {
      await loadState();
      setConnection("online", "已连接");
      pollTimer = window.setTimeout(poll, 1000);
    } catch (error) {
      setConnection("error", "连接中断");
      document.getElementById("overlayTitle").textContent = "房间不可用";
      document.getElementById("overlayText").textContent = error?.message || "链接已失效";
      document.getElementById("boardOverlay").hidden = false;
      pollTimer = window.setTimeout(poll, 3000);
    }
  }

  async function loadState() {
    const response = await window.fetch(
      endpoint("state", `?visitor_token=${encodeURIComponent(visitorToken)}`),
      { cache: "no-store" },
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data?.message || "房间状态不可用");
    room = data?.data?.room;
    render();
  }

  function notifyLeave() {
    if (!visitorToken) return;
    const body = JSON.stringify({ visitor_token: visitorToken });
    if (typeof window.navigator.sendBeacon === "function") {
      const payload = new Blob([body], { type: "application/json" });
      if (window.navigator.sendBeacon(endpoint("leave"), payload)) return;
    }
    window.fetch(endpoint("leave"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      keepalive: true,
    }).catch(() => {});
  }

  function setConnection(mode, label) {
    document.getElementById("connectionDot").className = `connection-dot ${mode}`;
    document.getElementById("roomStatus").textContent = label;
  }

  function statusLabel(status) {
    return {
      waiting: "等待玩家",
      setup: "等待开局",
      active: "对局中",
      paused: "已暂停",
      finished: "本局结束",
      rematch_pending: "等待 Bot 回应",
      closed: "房间已结束",
    }[status] || "等待中";
  }

  function difficultyLabel(value) {
    return { easy: "简单", normal: "普通", hard: "困难" }[value] || "普通";
  }

  function render() {
    if (!room) return;
    document.getElementById("roomId").textContent = room.room_id || "";
    document.getElementById("roomStatus").textContent = statusLabel(room.status);
    document.getElementById("visitorLabel").textContent = room.visitor_number ? `${room.visitor_number} 号` : "访客";
    document.getElementById("difficulty").textContent = difficultyLabel(room.difficulty);
    document.getElementById("humanScore").textContent = room.score?.human ?? 0;
    document.getElementById("botScore").textContent = room.score?.bot ?? 0;
    document.getElementById("drawScore").textContent = room.score?.draws ?? 0;
    renderSeat();
    renderPeople();
    renderMessages();
    drawBoard();
    renderTurn();
    icons();
  }

  function renderSeat() {
    const badge = document.getElementById("seatBadge");
    const action = document.getElementById("seatAction");
    const note = document.getElementById("seatNote");
    const sideChoice = document.getElementById("sideChoice");
    badge.textContent = room.is_player ? "玩家席" : "观众席";
    badge.className = `seat-badge ${room.is_player ? "player" : ""}`;
    sideChoice.hidden = !(["waiting", "setup", "finished"].includes(room.status));
    action.hidden = false;
    action.disabled = busy;
    if (!room.is_player && room.admin_room) {
      action.innerHTML = '<i data-lucide="clock-3"></i><span>等待管理员安排</span>';
      action.disabled = true;
      note.textContent = "管理员将在游戏管理台绑定玩家序号与 QQ。";
    } else if (!room.is_player) {
      action.innerHTML = '<i data-lucide="log-in"></i><span>加入对局</span>';
      action.disabled = busy || Boolean(room.player_number);
      note.textContent = room.player_number ? `${room.player_number} 号正在玩家席。` : "第一个加入玩家席的人开始对局。";
    } else if (room.status === "setup") {
      action.innerHTML = '<i data-lucide="play"></i><span>开始新一局</span>';
      note.textContent = room.player_confirmed ? "身份已确认。" : "身份尚未通过 QQ 确认，暂不写入长期记忆。";
    } else if (room.status === "finished") {
      action.innerHTML = '<i data-lucide="rotate-ccw"></i><span>申请再来一局</span>';
      note.textContent = "Bot 会结合当前人格决定是否接受。";
    } else if (room.status === "rematch_pending") {
      action.innerHTML = '<i data-lucide="loader-circle"></i><span>等待 Bot 回应</span>';
      action.disabled = true;
      note.textContent = "";
    } else {
      action.hidden = true;
      note.textContent = room.player_confirmed ? "身份已确认。" : "可在 QQ 中确认当前玩家身份。";
    }
  }

  function renderPeople() {
    const list = document.getElementById("peopleList");
    list.replaceChildren();
    const visitors = Array.isArray(room.visitors) ? room.visitors : [];
    document.getElementById("peopleCount").textContent = `${visitors.length} 人`;
    visitors.forEach((visitor) => {
      const chip = document.createElement("span");
      chip.className = `person-chip ${visitor.online ? "online" : ""} ${visitor.is_player ? "player" : ""}`;
      chip.textContent = `${visitor.number} 号${visitor.is_player ? " · 玩家" : ""}`;
      list.appendChild(chip);
    });
  }

  function renderMessages() {
    const list = document.getElementById("messages");
    list.replaceChildren();
    const messages = Array.isArray(room.messages) ? room.messages : [];
    if (!messages.length) {
      const empty = document.createElement("span");
      empty.className = "empty-message";
      empty.textContent = "对局开始后，这里会显示关键回应。";
      list.appendChild(empty);
      return;
    }
    messages.slice(-12).forEach((message) => {
      const item = document.createElement("p");
      item.className = `message ${message.role || "system"}`;
      item.textContent = String(message.content || "");
      list.appendChild(item);
    });
    list.scrollTop = list.scrollHeight;
  }

  function renderTurn() {
    const stone = document.getElementById("turnStone");
    const label = document.getElementById("turnLabel");
    stone.className = "turn-stone";
    if (!room.game) {
      label.textContent = statusLabel(room.status);
      return;
    }
    if (pendingMove) {
      label.textContent = "Bot 思考中";
      stone.classList.add(room.game.human_color === 1 ? "white" : "black");
      return;
    }
    const humanTurn = room.game.turn === room.game.human_color;
    label.textContent = room.game.winner
      ? (room.game.winner === room.game.human_color ? "玩家获胜" : "Bot 获胜")
      : (room.game.draw ? "平局" : (humanTurn ? "玩家落子" : "Bot 思考中"));
    stone.classList.add(room.game.turn === 1 ? "black" : "white");
  }

  function drawBoard() {
    const size = board.width;
    const margin = 48;
    const gap = (size - margin * 2) / 14;
    context.clearRect(0, 0, size, size);
    context.fillStyle = "#d4a85f";
    context.fillRect(0, 0, size, size);
    context.strokeStyle = "#5d472c";
    context.lineWidth = 1.6;
    for (let index = 0; index < 15; index += 1) {
      const point = margin + index * gap;
      context.beginPath(); context.moveTo(margin, point); context.lineTo(size - margin, point); context.stroke();
      context.beginPath(); context.moveTo(point, margin); context.lineTo(point, size - margin); context.stroke();
    }
    context.fillStyle = "#4a3823";
    [[3,3],[3,11],[7,7],[11,3],[11,11]].forEach(([row, column]) => {
      context.beginPath();
      context.arc(margin + column * gap, margin + row * gap, 4.2, 0, Math.PI * 2);
      context.fill();
    });
    const cells = room?.game?.board || [];
    cells.forEach((row, rowIndex) => row.forEach((color, columnIndex) => {
      if (!color) return;
      const x = margin + columnIndex * gap;
      const y = margin + rowIndex * gap;
      context.beginPath();
      context.arc(x, y, gap * .41, 0, Math.PI * 2);
      context.fillStyle = color === 1 ? "#242724" : "#f7f8f5";
      context.fill();
      context.strokeStyle = color === 1 ? "#121412" : "#9da39e";
      context.lineWidth = 1.5;
      context.stroke();
    }));
    if (pendingMove && room?.game?.board?.[pendingMove.row]?.[pendingMove.column] === 0) {
      const x = margin + pendingMove.column * gap;
      const y = margin + pendingMove.row * gap;
      context.beginPath();
      context.arc(x, y, gap * .41, 0, Math.PI * 2);
      context.fillStyle = pendingMove.color === 1 ? "#242724" : "#f7f8f5";
      context.fill();
      context.strokeStyle = pendingMove.color === 1 ? "#121412" : "#9da39e";
      context.lineWidth = 1.5;
      context.stroke();
    }
    const lastMove = pendingMove
      ? [pendingMove.row, pendingMove.column]
      : room?.game?.last_move;
    if (Array.isArray(lastMove)) {
      context.beginPath();
      context.arc(margin + lastMove[1] * gap, margin + lastMove[0] * gap, 5, 0, Math.PI * 2);
      context.fillStyle = "#b8483c";
      context.fill();
    }
  }

  async function seatAction() {
    if (busy || !room) return;
    busy = true;
    renderSeat();
    try {
      if (!room.is_player) {
        await request("POST", "claim", { visitor_token: visitorToken, side: selectedSide });
      } else if (room.status === "setup") {
        await request("POST", "start", { visitor_token: visitorToken, side: selectedSide });
      } else if (room.status === "finished") {
        await request("POST", "rematch", { visitor_token: visitorToken });
      }
      await loadState();
    } catch (error) {
      showToast(error?.message || "操作失败");
    } finally {
      busy = false;
      renderSeat();
    }
  }

  async function moveAt(event) {
    if (busy || !room?.is_player || room.status !== "active" || !room.game) return;
    if (room.game.turn !== room.game.human_color) return;
    const rect = board.getBoundingClientRect();
    const scale = board.width / rect.width;
    const x = (event.clientX - rect.left) * scale;
    const y = (event.clientY - rect.top) * scale;
    const margin = 48;
    const gap = (board.width - margin * 2) / 14;
    const column = Math.round((x - margin) / gap);
    const row = Math.round((y - margin) / gap);
    if (row < 0 || row > 14 || column < 0 || column > 14) return;
    if (room.game.board?.[row]?.[column]) {
      showToast("这个位置已经有棋子了");
      return;
    }
    busy = true;
    pendingMove = { row, column, color: room.game.human_color };
    render();
    try {
      const data = await request("POST", "move", { visitor_token: visitorToken, row, column });
      pendingMove = null;
      room = data.room;
      render();
    } catch (error) {
      pendingMove = null;
      try { await loadState(); } catch (_syncError) { /* poll will retry */ }
      showToast(error?.message || "无法落子");
    } finally {
      busy = false;
      render();
    }
  }

  document.querySelectorAll("[data-side]").forEach((button) => {
    button.addEventListener("click", () => {
      selectedSide = button.dataset.side;
      document.querySelectorAll("[data-side]").forEach((item) => {
        item.classList.toggle("is-active", item === button);
      });
    });
  });
  document.getElementById("seatAction").addEventListener("click", seatAction);
  board.addEventListener("click", moveAt);
  window.addEventListener("pagehide", (event) => {
    if (!event.persisted) notifyLeave();
  });
  icons();
  join()
    .then(() => { setConnection("online", "已连接"); pollTimer = window.setTimeout(poll, 1000); })
    .catch((error) => {
      setConnection("error", "无法进入");
      document.getElementById("boardOverlay").hidden = false;
      document.getElementById("overlayTitle").textContent = "无法进入房间";
      document.getElementById("overlayText").textContent = error?.message || "链接已经失效";
    });
})();
