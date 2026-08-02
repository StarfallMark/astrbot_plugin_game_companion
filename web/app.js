(() => {
  "use strict";

  const match = window.location.pathname.match(/\/room\/([A-Za-z0-9_-]{24,80})\/?$/);
  const accessToken = match ? match[1] : "";
  const storageKey = `game-companion:${accessToken}:visitor`;
  const board = document.getElementById("board");
  const boardStage = document.querySelector(".board-stage");
  const context = board.getContext("2d");
  const toast = document.getElementById("toast");
  let visitorToken = accessToken ? window.localStorage.getItem(storageKey) || "" : "";
  let room = null;
  let selectedSide = "human_black";
  let selectedPiece = null;
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
      throw new Error(data?.message || data?.error || "请求失败");
    }
    return data?.data ?? data;
  }

  async function join() {
    if (!accessToken) throw new Error("房间链接无效");
    const data = await request("POST", "join", { visitor_token: visitorToken });
    visitorToken = String(data.visitor_token || "");
    window.localStorage.setItem(storageKey, visitorToken);
    room = data.room;
    syncGameUi();
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
    const previousType = room?.game_type;
    room = data?.data?.room;
    if (previousType !== room?.game_type) {
      selectedPiece = null;
      pendingMove = null;
      syncGameUi();
    }
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
      method: "POST", headers: { "Content-Type": "application/json" }, body, keepalive: true,
    }).catch(() => {});
  }

  function setConnection(mode, label) {
    document.getElementById("connectionDot").className = `connection-dot ${mode}`;
    document.getElementById("roomStatus").textContent = label;
  }

  function statusLabel(status) {
    return {
      waiting: "等待玩家", setup: "等待开局", active: "对局中", paused: "已暂停",
      finished: "本局结束", rematch_pending: "等待 Bot 回应", closed: "房间已结束",
    }[status] || "等待中";
  }

  function difficultyLabel(value) {
    return { easy: "简单", normal: "普通", hard: "困难" }[value] || "普通";
  }

  function gameLabel() {
    return room?.game_type === "xiangqi" ? "中国象棋" : "五子棋";
  }

  function syncGameUi() {
    if (!room) return;
    const xiangqi = room.game_type === "xiangqi";
    document.title = `游戏伴侣 · ${gameLabel()}`;
    document.getElementById("gameTitle").textContent = gameLabel();
    document.getElementById("brandIcon").setAttribute("data-lucide", xiangqi ? "circle-dot" : "grid-3x3");
    boardStage.classList.toggle("xiangqi", xiangqi);
    board.width = xiangqi ? 720 : 760;
    board.height = xiangqi ? 800 : 760;
    board.setAttribute("aria-label", xiangqi ? "九乘十中国象棋棋盘" : "十五乘十五五子棋棋盘");
    const buttons = Array.from(document.querySelectorAll("[data-side]"));
    const values = xiangqi
      ? [["human_red", "我执红"], ["human_black", "我执黑"], ["random", "随机"]]
      : [["human_black", "我先手"], ["bot_black", "Bot先手"], ["random", "随机"]];
    selectedSide = values[0][0];
    buttons.forEach((button, index) => {
      button.dataset.side = values[index][0];
      button.textContent = values[index][1];
      button.classList.toggle("is-active", index === 0);
    });
    icons();
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
    const xiangqi = room.game_type === "xiangqi";
    const humanSide = xiangqi ? room.game.human_side : room.game.human_color;
    const humanTurn = room.game.turn === humanSide;
    if (pendingMove) {
      label.textContent = "Bot 思考中";
    } else {
      label.textContent = room.game.winner
        ? (room.game.winner === humanSide ? "玩家获胜" : "Bot 获胜")
        : (room.game.draw ? "平局" : (humanTurn ? "玩家走棋" : "Bot 思考中"));
    }
    if (xiangqi) {
      stone.classList.add(room.game.turn === "red" ? "red" : "black");
    } else {
      stone.classList.add(room.game.turn === 1 ? "black" : "white");
    }
  }

  function drawBoard() {
    if (room?.game_type === "xiangqi") drawXiangqi();
    else drawGomoku();
  }

  function drawGomoku() {
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
    [[3, 3], [3, 11], [7, 7], [11, 3], [11, 11]].forEach(([row, column]) => {
      context.beginPath();
      context.arc(margin + column * gap, margin + row * gap, 4, 0, Math.PI * 2);
      context.fill();
    });
    const cells = room?.game?.board || [];
    cells.forEach((row, rowIndex) => row.forEach((color, columnIndex) => {
      if (color) drawGomokuStone(rowIndex, columnIndex, color, margin, gap);
    }));
    if (pendingMove?.kind === "gomoku" && !room?.game?.board?.[pendingMove.row]?.[pendingMove.column]) {
      drawGomokuStone(pendingMove.row, pendingMove.column, pendingMove.color, margin, gap);
    }
    const lastMove = pendingMove?.kind === "gomoku"
      ? [pendingMove.row, pendingMove.column]
      : room?.game?.last_move;
    if (Array.isArray(lastMove)) {
      context.beginPath();
      context.arc(margin + lastMove[1] * gap, margin + lastMove[0] * gap, 5, 0, Math.PI * 2);
      context.fillStyle = "#b8483c";
      context.fill();
    }
  }

  function drawGomokuStone(row, column, color, margin, gap) {
    context.beginPath();
    context.arc(margin + column * gap, margin + row * gap, gap * 0.41, 0, Math.PI * 2);
    context.fillStyle = color === 1 ? "#242724" : "#f7f8f5";
    context.fill();
    context.strokeStyle = color === 1 ? "#121412" : "#9da39e";
    context.lineWidth = 1.5;
    context.stroke();
  }

  function xiangqiFlipped() {
    return room?.game?.human_side === "black";
  }

  function displayPoint(row, column) {
    return xiangqiFlipped() ? [9 - row, 8 - column] : [row, column];
  }

  function modelPoint(displayRow, displayColumn) {
    return xiangqiFlipped() ? [9 - displayRow, 8 - displayColumn] : [displayRow, displayColumn];
  }

  function drawXiangqi() {
    const width = board.width;
    const height = board.height;
    const marginX = 54;
    const marginY = 48;
    const gapX = (width - marginX * 2) / 8;
    const gapY = (height - marginY * 2) / 9;
    context.clearRect(0, 0, width, height);
    context.fillStyle = "#d7a85d";
    context.fillRect(0, 0, width, height);
    context.strokeStyle = "#563c23";
    context.lineWidth = 1.7;
    for (let row = 0; row < 10; row += 1) {
      const y = marginY + row * gapY;
      context.beginPath(); context.moveTo(marginX, y); context.lineTo(width - marginX, y); context.stroke();
    }
    for (let column = 0; column < 9; column += 1) {
      const x = marginX + column * gapX;
      context.beginPath();
      context.moveTo(x, marginY);
      if (column === 0 || column === 8) {
        context.lineTo(x, height - marginY);
      } else {
        context.lineTo(x, marginY + 4 * gapY);
        context.moveTo(x, marginY + 5 * gapY);
        context.lineTo(x, height - marginY);
      }
      context.stroke();
    }
    [[0, 3, 2, 5], [0, 5, 2, 3], [7, 3, 9, 5], [7, 5, 9, 3]].forEach(([r1, c1, r2, c2]) => {
      const first = displayPoint(r1, c1);
      const second = displayPoint(r2, c2);
      context.beginPath();
      context.moveTo(marginX + first[1] * gapX, marginY + first[0] * gapY);
      context.lineTo(marginX + second[1] * gapX, marginY + second[0] * gapY);
      context.stroke();
    });
    context.save();
    context.fillStyle = "#654528";
    context.font = '600 29px "Noto Serif SC", "Songti SC", serif';
    context.textAlign = "center";
    context.textBaseline = "middle";
    context.fillText(xiangqiFlipped() ? "漢界" : "楚河", width * 0.29, height / 2);
    context.fillText(xiangqiFlipped() ? "楚河" : "漢界", width * 0.71, height / 2);
    context.restore();

    const legal = Array.isArray(room?.game?.legal_moves) ? room.game.legal_moves : [];
    if (selectedPiece) {
      legal.filter((move) => move[0] === selectedPiece[0] && move[1] === selectedPiece[1]).forEach((move) => {
        const [row, column] = displayPoint(move[2], move[3]);
        context.beginPath();
        context.arc(marginX + column * gapX, marginY + row * gapY, 10, 0, Math.PI * 2);
        context.fillStyle = "rgba(28, 104, 70, .72)";
        context.fill();
      });
    }

    const cells = (room?.game?.board || []).map((row) => row.slice());
    if (pendingMove?.kind === "xiangqi") {
      cells[pendingMove.to_row][pendingMove.to_column] = cells[pendingMove.from_row][pendingMove.from_column];
      cells[pendingMove.from_row][pendingMove.from_column] = ".";
    }
    cells.forEach((row, modelRow) => row.forEach((piece, modelColumn) => {
      if (piece !== ".") drawXiangqiPiece(modelRow, modelColumn, piece, marginX, marginY, gapX, gapY);
    }));
    const lastMove = pendingMove?.kind === "xiangqi"
      ? [pendingMove.from_row, pendingMove.from_column, pendingMove.to_row, pendingMove.to_column]
      : room?.game?.last_move;
    if (Array.isArray(lastMove)) {
      [[lastMove[0], lastMove[1]], [lastMove[2], lastMove[3]]].forEach(([modelRow, modelColumn]) => {
        const [row, column] = displayPoint(modelRow, modelColumn);
        context.strokeStyle = "#b43e35";
        context.lineWidth = 3;
        context.strokeRect(
          marginX + column * gapX - gapX * 0.31,
          marginY + row * gapY - gapY * 0.31,
          gapX * 0.62,
          gapY * 0.62,
        );
      });
    }
  }

  function drawXiangqiPiece(modelRow, modelColumn, piece, marginX, marginY, gapX, gapY) {
    const [row, column] = displayPoint(modelRow, modelColumn);
    const x = marginX + column * gapX;
    const y = marginY + row * gapY;
    const red = piece === piece.toUpperCase();
    const labels = {
      K: "帅", A: "仕", B: "相", N: "马", R: "车", C: "炮", P: "兵",
      k: "将", a: "士", b: "象", n: "马", r: "车", c: "炮", p: "卒",
    };
    context.beginPath();
    context.arc(x, y, Math.min(gapX, gapY) * 0.39, 0, Math.PI * 2);
    context.fillStyle = "#f1d49a";
    context.fill();
    context.strokeStyle = red ? "#a3332e" : "#242724";
    context.lineWidth = 2.5;
    context.stroke();
    context.fillStyle = red ? "#a3332e" : "#242724";
    context.font = `700 ${Math.floor(Math.min(gapX, gapY) * 0.43)}px "Noto Serif SC", "Songti SC", serif`;
    context.textAlign = "center";
    context.textBaseline = "middle";
    context.fillText(labels[piece] || piece, x, y + 1);
    if (selectedPiece?.[0] === modelRow && selectedPiece?.[1] === modelColumn) {
      context.strokeStyle = "#176347";
      context.lineWidth = 4;
      context.stroke();
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
    if (room.game_type === "xiangqi") await moveXiangqi(event);
    else await moveGomoku(event);
  }

  async function moveGomoku(event) {
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
    pendingMove = { kind: "gomoku", row, column, color: room.game.human_color };
    render();
    try {
      const data = await request("POST", "move", { visitor_token: visitorToken, row, column });
      pendingMove = null;
      room = data.room;
      render();
    } catch (error) {
      pendingMove = null;
      try { await loadState(); } catch (_syncError) { /* polling will retry */ }
      showToast(error?.message || "无法落子");
    } finally {
      busy = false;
      render();
    }
  }

  async function moveXiangqi(event) {
    if (room.game.turn !== room.game.human_side) return;
    const rect = board.getBoundingClientRect();
    const scaleX = board.width / rect.width;
    const scaleY = board.height / rect.height;
    const x = (event.clientX - rect.left) * scaleX;
    const y = (event.clientY - rect.top) * scaleY;
    const marginX = 54;
    const marginY = 48;
    const gapX = (board.width - marginX * 2) / 8;
    const gapY = (board.height - marginY * 2) / 9;
    const displayColumn = Math.round((x - marginX) / gapX);
    const displayRow = Math.round((y - marginY) / gapY);
    if (displayRow < 0 || displayRow > 9 || displayColumn < 0 || displayColumn > 8) return;
    const [row, column] = modelPoint(displayRow, displayColumn);
    const legal = Array.isArray(room.game.legal_moves) ? room.game.legal_moves : [];
    if (selectedPiece) {
      const move = legal.find((item) => (
        item[0] === selectedPiece[0] && item[1] === selectedPiece[1]
        && item[2] === row && item[3] === column
      ));
      if (move) {
        busy = true;
        pendingMove = {
          kind: "xiangqi", from_row: move[0], from_column: move[1], to_row: move[2], to_column: move[3],
        };
        selectedPiece = null;
        render();
        try {
          const data = await request("POST", "move", { visitor_token: visitorToken, ...pendingMove });
          pendingMove = null;
          room = data.room;
          render();
        } catch (error) {
          pendingMove = null;
          try { await loadState(); } catch (_syncError) { /* polling will retry */ }
          showToast(error?.message || "无法走棋");
        } finally {
          busy = false;
          render();
        }
        return;
      }
    }
    const selectable = legal.some((item) => item[0] === row && item[1] === column);
    selectedPiece = selectable ? [row, column] : null;
    if (!selectable && room.game.board?.[row]?.[column] !== ".") showToast("当前不能移动这枚棋子");
    drawBoard();
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
