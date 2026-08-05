(() => {
  "use strict";

  const match = window.location.pathname.match(/\/room\/([A-Za-z0-9_-]{24,80})\/?$/);
  const accessToken = match ? match[1] : "";
  const storageKey = `game-companion:${accessToken}:visitor`;
  const board = document.getElementById("board");
  const boardStage = document.querySelector(".board-stage");
  const soupStage = document.getElementById("soupStage");
  const diceStage = document.getElementById("diceStage");
  const drawStage = document.getElementById("drawStage");
  const drawCanvas = document.getElementById("drawCanvas");
  const drawContext = drawCanvas.getContext("2d");
  const chatInput = document.getElementById("chatInput");
  const context = board.getContext("2d");
  const toast = document.getElementById("toast");
  let visitorToken = accessToken ? window.localStorage.getItem(storageKey) || "" : "";
  let room = null;
  let selectedSide = "human_black";
  let selectedPiece = null;
  let pollTimer = 0;
  let toastTimer = 0;
  let busy = false;
  let chatBusy = false;
  let pendingMove = null;
  let activeRoomView = "game";
  let lastSeenMessageId = 0;
  let renderedDiceSequence = 0;
  let drawStrokes = [];
  let activeDrawStroke = null;
  let drawSyncBusy = false;
  let drawRevision = -1;

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
    lastSeenMessageId = latestMessageId(room);
    syncGameUi();
    render();
  }

  async function poll() {
    window.clearTimeout(pollTimer);
    if (!visitorToken) return;
    try {
      await loadState();
      setConnection("online", "已连接");
      const interval = room?.game_type === "pig_dice" && room.game?.turn === "bot" ? 300 : 1000;
      pollTimer = window.setTimeout(poll, interval);
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
    const previousMessageId = latestMessageId(room);
    room = data?.data?.room;
    if (previousType !== room?.game_type) {
      selectedPiece = null;
      pendingMove = null;
      drawStrokes = [];
      drawRevision = -1;
      syncGameUi();
    }
    syncDrawState();
    if (
      activeRoomView !== "chat"
      && latestMessageId(room) > Math.max(previousMessageId, lastSeenMessageId)
    ) {
      document.getElementById("chatUnread").hidden = false;
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

  function latestMessageId(currentRoom) {
    const messages = Array.isArray(currentRoom?.messages) ? currentRoom.messages : [];
    return messages.reduce((latest, message) => {
      const value = Number(message.id || 0);
      return Number.isFinite(value) ? Math.max(latest, value) : latest;
    }, 0);
  }

  function setRoomView(view) {
    activeRoomView = view === "chat" ? "chat" : "game";
    document.getElementById("gameWorkspace").classList.toggle("room-view-hidden", activeRoomView !== "game");
    document.getElementById("chatPanel").classList.toggle("room-view-hidden", activeRoomView !== "chat");
    document.querySelectorAll("[data-room-view]").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.roomView === activeRoomView);
    });
    if (activeRoomView === "chat") {
      lastSeenMessageId = latestMessageId(room);
      document.getElementById("chatUnread").hidden = true;
      window.setTimeout(() => chatInput.focus(), 0);
    }
  }

  function difficultyLabel(value) {
    return { easy: "简单", normal: "普通", hard: "困难" }[value] || "普通";
  }

  function gameLabel() {
    return {
      gomoku: "五子棋",
      xiangqi: "中国象棋",
      tictactoe: "井字棋",
      turtle_soup: "海龟汤",
      pig_dice: "贪心骰子",
      draw_guess: "你画我猜",
    }[room?.game_type] || "棋类游戏";
  }

  function syncGameUi() {
    if (!room) return;
    const xiangqi = room.game_type === "xiangqi";
    const tictactoe = room.game_type === "tictactoe";
    const turtleSoup = room.game_type === "turtle_soup";
    const pigDice = room.game_type === "pig_dice";
    const drawGuess = room.game_type === "draw_guess";
    document.title = `游戏伴侣 · ${gameLabel()}`;
    document.getElementById("gameTitle").textContent = gameLabel();
    document.getElementById("brandIcon").setAttribute(
      "data-lucide",
      turtleSoup ? "shell" : (pigDice ? "dice-5" : (drawGuess ? "paintbrush" : (xiangqi ? "circle-dot" : (tictactoe ? "badge-x" : "grid-3x3")))),
    );
    boardStage.hidden = turtleSoup || pigDice || drawGuess;
    soupStage.hidden = !turtleSoup;
    diceStage.hidden = !pigDice;
    drawStage.hidden = !drawGuess;
    boardStage.classList.toggle("xiangqi", xiangqi);
    boardStage.classList.toggle("tictactoe", tictactoe);
    board.width = xiangqi ? 720 : 760;
    board.height = xiangqi ? 800 : 760;
    board.setAttribute(
      "aria-label",
      turtleSoup
        ? "海龟汤问答区"
        : pigDice
        ? "贪心骰子操作区"
        : xiangqi
        ? "九乘十中国象棋棋盘"
        : drawGuess
        ? "你画我猜作画区"
        : (tictactoe ? "三乘三井字棋棋盘" : "十五乘十五五子棋棋盘"),
    );
    const buttons = Array.from(document.querySelectorAll("[data-side]"));
    let values = [["human_black", "我先手"], ["bot_black", "Bot先手"], ["random", "随机"]];
    if (xiangqi) {
      values = [["human_red", "我执红"], ["human_black", "我执黑"], ["random", "随机"]];
    } else if (tictactoe) {
      values = [["human_x", "我执 X"], ["human_o", "我执 O"], ["random", "随机"]];
    }
    selectedSide = values[0][0];
    buttons.forEach((button, index) => {
      button.dataset.side = values[index][0];
      button.textContent = values[index][1];
      button.classList.toggle("is-active", index === 0);
    });
    icons();
    syncDrawState();
  }

  function render() {
    if (!room) return;
    document.getElementById("roomId").textContent = room.room_id || "";
    document.getElementById("roomStatus").textContent = statusLabel(room.status);
    document.getElementById("visitorLabel").textContent = room.visitor_number
      ? (room.visitor_display_name
        ? `${room.visitor_display_name}（${room.visitor_number}号）`
        : `${room.visitor_number} 号`)
      : "访客";
    document.getElementById("chatIdentity").textContent = room.is_player
      ? (room.visitor_display_name
        ? `${room.visitor_display_name}（玩家）`
        : `${room.visitor_number || "?"}号玩家`)
      : room.player_confirmed
      ? (room.visitor_display_name
        ? `${room.visitor_display_name}（观众）`
        : `${room.visitor_number || "?"}号观众`)
      : `匿名观众（${room.visitor_number || "?"}号）`;
    chatInput.placeholder = room.game_type === "turtle_soup"
      ? "提问、给线索，或和 Bot 聊天"
      : "和 Bot 说点什么";
    document.getElementById("chatSend").disabled = chatBusy;
    const pigDice = room.game_type === "pig_dice";
    const drawGuess = room.game_type === "draw_guess";
    document.getElementById("difficulty").textContent = pigDice
      ? ({ easy: "稳健", normal: "均衡", hard: "大胆" }[room.difficulty] || "均衡")
      : difficultyLabel(room.difficulty);
    document.getElementById("humanScore").textContent = room.score?.human ?? 0;
    document.getElementById("botScore").textContent = room.score?.bot ?? 0;
    document.getElementById("drawScore").textContent = room.score?.draws ?? 0;
    const turtleSoup = room.game_type === "turtle_soup";
    const playerHostedSoup = turtleSoup && room.turtle_soup_mode === "player_host";
    document.getElementById("humanScoreLabel").textContent = drawGuess ? "猜中" : turtleSoup ? (playerHostedSoup ? "玩家" : "解开") : "玩家";
    document.getElementById("drawScoreLabel").textContent = drawGuess ? "总轮数" : turtleSoup ? "总题数" : (pigDice ? "总局数" : "平局");
    document.getElementById("botScoreLabel").textContent = drawGuess ? "未猜中" : turtleSoup ? (playerHostedSoup ? "Bot 猜中" : "放弃") : "Bot";
    if (turtleSoup || pigDice || drawGuess) document.getElementById("drawScore").textContent = room.score?.games ?? 0;
    renderSeat();
    renderPeople();
    renderMessages();
    renderTurtleSoup();
    renderPigDice();
    renderDrawGuess();
    drawBoard();
    renderTurn();
    icons();
  }

  function renderSeat() {
    const badge = document.getElementById("seatBadge");
    const action = document.getElementById("seatAction");
    const note = document.getElementById("seatNote");
    const identityChallenge = document.getElementById("identityChallenge");
    const identityToken = document.getElementById("identityToken");
    const identityTokenNote = document.getElementById("identityTokenNote");
    const sideChoice = document.getElementById("sideChoice");
    badge.textContent = room.is_player ? "玩家席" : "观众席";
    badge.className = `seat-badge ${room.is_player ? "player" : ""}`;
    sideChoice.hidden = ["turtle_soup", "pig_dice", "draw_guess"].includes(room.game_type) || !(["waiting", "setup", "finished"].includes(room.status));
    action.hidden = false;
    action.disabled = busy;
    const identityRequired = !room.admin_room && !room.player_confirmed;
    identityChallenge.hidden = !identityRequired;
    if (identityRequired) {
      identityToken.textContent = room.identity_token || "--------";
      identityTokenNote.textContent = room.identity_token
        ? (room.source === "group" ? "请在原群聊中 @Bot 直接发送令牌，或发送：" : "请在原私聊中发送令牌，或发送：")
          + "绑定玩家 " + room.identity_token
        : "令牌已过期，刷新页面后重新获取";
    }
    if (!room.is_player && room.admin_room) {
      action.innerHTML = '<i data-lucide="clock-3"></i><span>等待管理员安排</span>';
      action.disabled = true;
      note.textContent = "管理员将在游戏管理台绑定玩家序号与 QQ。";
    } else if (!room.is_player) {
      action.innerHTML = '<i data-lucide="log-in"></i><span>加入对局</span>';
      const capacity = Number(room.player_capacity || 1);
      const full = capacity > 0 && (room.player_numbers || []).length >= capacity;
      action.disabled = busy || full || identityRequired;
      note.textContent = full
        ? "玩家席已满，可向席内玩家申请交换。"
        : identityRequired
        ? "请先用页面令牌在 QQ 中绑定身份。"
        : room.multiplayer_enabled
        ? `玩家席 ${room.player_numbers?.length || 0} / ${capacity || "不限"}，加入后按顺序轮流操作。`
        : room.player_number ? `${room.player_number} 号正在玩家席。` : "第一个加入玩家席的人开始对局。";
    } else if (room.status === "setup") {
      action.innerHTML = room.game_type === "turtle_soup"
        ? room.turtle_soup_mode === "player_host"
          ? '<i data-lucide="message-circle-question"></i><span>开始让 Bot 猜</span>'
          : '<i data-lucide="sparkles"></i><span>开始出题</span>'
        : room.game_type === "pig_dice"
        ? '<i data-lucide="dice-5"></i><span>开始掷骰</span>'
        : room.game_type === "draw_guess"
        ? '<i data-lucide="paintbrush"></i><span>开始作画</span>'
        : '<i data-lucide="play"></i><span>开始新一局</span>';
      note.textContent = room.player_confirmed ? "身份已确认。" : "身份尚未通过 QQ 确认，暂不允许进入玩家席。";
    } else if (room.status === "finished") {
      action.innerHTML = room.game_type === "turtle_soup"
        ? `<i data-lucide="rotate-ccw"></i><span>${room.turtle_soup_mode === "player_host" ? "申请再出一题" : "申请再来一道"}</span>`
        : '<i data-lucide="rotate-ccw"></i><span>申请再来一局</span>';
      note.textContent = "Bot 会结合当前人格决定是否接受。";
    } else if (room.status === "rematch_pending") {
      action.innerHTML = '<i data-lucide="loader-circle"></i><span>等待 Bot 回应</span>';
      action.disabled = true;
      note.textContent = "";
    } else {
      action.hidden = true;
      note.textContent = room.player_confirmed ? "身份已确认。" : "请先在 QQ 中绑定页面令牌。";
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
      chip.textContent = `${visitor.display_name ? `${visitor.display_name}（${visitor.number}号）` : `${visitor.number}号`}${visitor.is_player ? " · 玩家" : ""}`;
      if (room.multiplayer_enabled && !room.is_player && visitor.is_player) {
        const request = document.createElement("button");
        request.type = "button";
        request.textContent = "申请交换";
        const cooldown = Number(room.swap_cooldown_until || 0);
        request.disabled = !room.player_confirmed || Boolean(room.outgoing_swap_request) || (cooldown && cooldown > (room.server_time || Date.now() / 1000));
        request.addEventListener("click", () => requestSeatSwap(visitor.number));
        chip.appendChild(request);
      }
      if (room.multiplayer_enabled && visitor.number === room.visitor_number && room.is_player) {
        (room.incoming_swap_requests || []).forEach((swap) => {
          const accept = document.createElement("button");
          accept.type = "button";
          accept.textContent = `${swap.requester_number}号申请，接受`;
          accept.addEventListener("click", () => respondSeatSwap(swap.request_id, true));
          chip.appendChild(accept);
          const decline = document.createElement("button");
          decline.type = "button";
          decline.textContent = "拒绝";
          decline.addEventListener("click", () => respondSeatSwap(swap.request_id, false));
          chip.appendChild(decline);
        });
      }
      list.appendChild(chip);
    });
  }

  async function requestSeatSwap(targetNumber) {
    if (busy || room?.is_player) return;
    busy = true;
    try {
      const data = await request("POST", "seat/swap/request", {
        visitor_token: visitorToken,
        target_number: targetNumber,
      });
      room = data.room;
      showToast("交换申请已发送");
    } catch (error) {
      showToast(error?.message || "无法发送交换申请");
    } finally {
      busy = false;
      render();
    }
  }

  async function respondSeatSwap(requestId, accepted) {
    if (busy || !room?.is_player) return;
    busy = true;
    try {
      const data = await request("POST", "seat/swap/respond", {
        visitor_token: visitorToken,
        request_id: requestId,
        accepted,
      });
      room = data.room;
      showToast(accepted ? "席位已交换" : "已拒绝交换申请");
    } catch (error) {
      showToast(error?.message || "无法处理交换申请");
    } finally {
      busy = false;
      render();
    }
  }

  function renderMessages() {
    const list = document.getElementById("messages");
    const wasNearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 48;
    list.replaceChildren();
    const messages = Array.isArray(room.messages) ? room.messages : [];
    if (!messages.length) {
      const empty = document.createElement("span");
      empty.className = "empty-message";
      empty.textContent = "房间对话会显示在这里";
      list.appendChild(empty);
      return;
    }
    messages.slice(-60).forEach((message) => {
      const role = message.role || "system";
      const item = document.createElement("article");
      item.className = `message ${role} ${message.message_type || "chat"}`;
      if (role !== "system") {
        const meta = document.createElement("span");
        meta.className = "message-meta";
        if (role === "bot") {
          meta.textContent = "Bot";
        } else {
          const sender = String(message.sender_name || "匿名观众");
          meta.textContent = message.sender_number
            ? `${sender}（${message.sender_number}号）`
            : sender;
        }
        item.appendChild(meta);
      }
      const content = document.createElement("p");
      content.className = "message-content";
      content.textContent = String(message.content || "");
      item.appendChild(content);
      list.appendChild(item);
    });
    if (wasNearBottom || activeRoomView === "chat") list.scrollTop = list.scrollHeight;
    if (activeRoomView === "chat") lastSeenMessageId = latestMessageId(room);
  }

  function renderTurtleSoup() {
    if (room?.game_type !== "turtle_soup") return;
    const game = room.game;
    const puzzle = game?.puzzle;
    const playerHosted = game?.mode === "player_host" || room.turtle_soup_mode === "player_host";
    document.getElementById("soupTitle").textContent = playerHosted
      ? "玩家出题 · Bot 猜"
      : puzzle?.title || "正在准备题目";
    document.getElementById("soupSurface").textContent = playerHosted
      ? "玩家轮流提供公开线索或回答 Bot 的问题；Bot 不会提前看到隐藏汤底。"
      : puzzle?.surface || (game?.failure_reason ? "Bot 正在重新整理题目。" : "Bot 正在构思一道新的海龟汤。");
    document.getElementById("soupContentLevel").textContent = {
      all_ages: "全年龄", normal: "普通", unrestricted: "不限制",
    }[puzzle?.content_level || game?.content_level] || "普通";
    document.getElementById("soupQuestionCount").textContent = game?.question_count ?? 0;
    document.getElementById("soupHintCount").textContent = playerHosted
      ? `${room.player_numbers?.length || 0} 人`
      : `${game?.hints_used ?? 0} / ${game?.hint_limit ?? 0}`;
    document.getElementById("soupAnswerCount").textContent = game?.answer_attempts ?? 0;
    document.getElementById("soupFactCount").textContent = playerHosted
      ? game?.turn_count ?? 0
      : `${game?.discovered_fact_count ?? 0} / ${game?.key_fact_count ?? 0}`;
    const progressLabels = document.querySelectorAll(".soup-progress dt");
    if (progressLabels.length === 4) {
      progressLabels[0].textContent = playerHosted ? "Bot 提问" : "提问";
      progressLabels[1].textContent = playerHosted ? "参与玩家" : "提示";
      progressLabels[2].textContent = playerHosted ? "Bot 猜测" : "答案尝试";
      progressLabels[3].textContent = playerHosted ? "公开回合" : "关键事实";
    }

    const turn = document.getElementById("soupTurn");
    const remaining = room.turn_deadline
      ? Math.max(0, Math.ceil(room.turn_deadline - Number(room.server_time || 0)))
      : 0;
    const currentPlayerLabel = room.current_player_name
      ? `${room.current_player_name}（${room.current_player_number}号）`
      : (room.current_player_number ? `${room.current_player_number}号` : "未知玩家");
    turn.textContent = room.current_player_number
      ? `当前轮到 ${currentPlayerLabel}${remaining ? ` · 剩余 ${remaining} 秒` : ""}${room.is_current_player ? " · 轮到你" : ""}`
      : "等待玩家加入";

    const history = document.getElementById("soupHistory");
    history.replaceChildren();
    const entries = Array.isArray(game?.entries) ? game.entries.slice() : [];
    if (!entries.length) {
      const empty = document.createElement("span");
      empty.className = "soup-empty";
      empty.textContent = game?.preparing ? "题目生成并校验后会显示在这里" : "还没有公开问答";
      history.appendChild(empty);
    } else {
      entries.forEach((entry) => {
        const item = document.createElement("article");
        item.className = `soup-entry ${entry.kind || "question"} ${entry.pending ? "pending" : ""}`;
        const prompt = document.createElement("p");
        prompt.className = "prompt";
        prompt.textContent = entry.kind === "reverse"
          ? `${entry.player_number || "?"} 号线索/回答：${entry.prompt || ""}`
          : entry.kind === "hint"
          ? "玩家申请了提示"
          : `${entry.player_number ? `${entry.player_number} 号` : "玩家"}${entry.kind === "answer" ? "猜测" : "问题"}：${entry.prompt || ""}`;
        const response = document.createElement("p");
        response.className = "response";
        response.textContent = entry.kind === "reverse" && entry.pending
          ? entry.response || "Bot 推理中"
          : entry.kind === "reverse"
          ? `Bot ${entry.bot_action === "guess" ? "猜测" : "提问"}：${entry.response || ""}`
          : entry.response || "Bot 判断中";
        item.append(prompt, response);
        history.appendChild(item);
      });
      history.scrollTop = history.scrollHeight;
    }

    const solution = document.getElementById("soupSolution");
    solution.hidden = playerHosted || !puzzle?.solution;
    document.getElementById("soupSolutionText").textContent = puzzle?.solution || "";

  }

  function renderPigDice() {
    if (room?.game_type !== "pig_dice") return;
    const game = room.game;
    if (!game) renderedDiceSequence = 0;
    document.getElementById("diceTarget").textContent = game?.target_score ?? 50;
    document.getElementById("diceHumanScore").textContent = game?.human_score ?? 0;
    document.getElementById("diceBotScore").textContent = game?.bot_score ?? 0;
    document.getElementById("diceTurnTotal").textContent = game?.turn_total ?? 0;
    document.getElementById("diceRisk").textContent = `Bot 风格：${{
      cautious: "稳健", balanced: "均衡", bold: "大胆",
    }[game?.risk_style] || "均衡"}`;

    const cube = document.getElementById("diceCube");
    const value = Number(game?.last_roll || 0);
    cube.className = value ? `dice-cube value-${value}` : "dice-cube waiting";
    cube.setAttribute("aria-label", value ? `骰子点数 ${value}` : "尚未掷骰");
    if (game?.action_count && game.action_count !== renderedDiceSequence) {
      renderedDiceSequence = game.action_count;
      cube.classList.add("is-rolling");
      window.setTimeout(() => cube.classList.remove("is-rolling"), 420);
    }

    const status = document.getElementById("diceStatus");
    if (!game) status.textContent = "等待开局";
    else if (game.finished) status.textContent = game.winner === "human" ? "玩家获胜" : "Bot 获胜";
    else if (room.status === "paused") status.textContent = "对局已暂停";
    else status.textContent = game.turn === "human" ? "轮到玩家" : "Bot 正在掷骰";

    const history = document.getElementById("diceHistory");
    history.replaceChildren();
    const entries = Array.isArray(game?.history) ? game.history.slice(-10).reverse() : [];
    if (!entries.length) {
      const empty = document.createElement("p");
      empty.className = "dice-empty";
      empty.textContent = "开局后，每次掷骰和存分都会记录在这里。";
      history.appendChild(empty);
    } else {
      entries.forEach((entry) => {
        const item = document.createElement("div");
        item.className = `dice-event ${entry.actor || "human"} ${entry.action || "roll"}`;
        const actor = entry.actor === "human" ? "玩家" : "Bot";
        let text = `${actor} 掷出 ${entry.value}`;
        if (entry.action === "bust") text = `${actor} 掷出 1，损失 ${entry.lost || 0} 分`;
        if (entry.action === "hold") text = `${actor} 收手，存下 ${entry.banked || 0} 分`;
        if (entry.action === "win") text = `${actor} 存下 ${entry.banked || 0} 分并获胜`;
        if (entry.action === "resign") text = "玩家投降，本局结束";
        item.textContent = text;
        history.appendChild(item);
      });
    }

    const canAct = Boolean(
      room.is_player && room.status === "active" && game && !game.finished
      && game.turn === "human" && !busy
    );
    document.getElementById("diceRollAction").disabled = !canAct;
    document.getElementById("diceHoldAction").disabled = !canAct || !(game?.turn_total > 0);
  }

  function syncDrawState() {
    if (room?.game_type !== "draw_guess") return;
    const serverGame = room.game || {};
    if (!activeDrawStroke && !drawSyncBusy && Number(serverGame.revision ?? -1) >= drawRevision) {
      drawStrokes = Array.isArray(serverGame.strokes) ? serverGame.strokes : [];
      drawRevision = Number(serverGame.revision ?? 0);
    }
  }

  function renderDrawGuess() {
    if (room?.game_type !== "draw_guess") return;
    syncDrawState();
    const game = room.game || {};
    const remaining = room.status === "paused"
      ? Number(game.remaining_seconds || 0)
      : Math.max(0, Math.ceil(Number(game.deadline || 0) - Number(room.server_time || Date.now() / 1000)));
    document.getElementById("drawTimer").textContent = !room.game
      ? "等待开始"
      : game.finished
      ? (game.solved ? "已猜中" : game.timed_out ? "已超时" : "本轮结束")
      : `${remaining} 秒`;
    document.getElementById("drawGuessCount").textContent = `猜测 ${game.guess_count || 0} / ${game.max_guesses || 5}`;
    const prompt = document.getElementById("drawPrompt");
    if (game.finished && game.answer) {
      prompt.textContent = `答案是“${game.answer}”。${game.solved ? "这轮合作成功。" : "下一轮可以换个画法。"}`;
    } else if (game.answer && room.is_player) {
      prompt.textContent = `题目：${game.answer}。请把它画出来，观众和 Bot 不会看到答案。`;
    } else if (game.processing) {
      prompt.textContent = "Bot 正在看图，只会消耗一次猜测。";
    } else if (!room.game) {
      prompt.textContent = room.is_player ? "开始后，你会在这里看到题目。" : "等待玩家开始新一轮。";
    } else {
      prompt.textContent = room.is_player ? "画出题目后，点击“让 Bot 猜”；可以继续补画。" : "玩家正在作画，你可以在对话区和 Bot 聊天。";
    }
    drawContext.clearRect(0, 0, drawCanvas.width, drawCanvas.height);
    drawContext.fillStyle = "#fffdf8";
    drawContext.fillRect(0, 0, drawCanvas.width, drawCanvas.height);
    drawStrokes.forEach(drawStroke);
    const readonly = busy || !room.is_player || room.status !== "active" || game.processing || game.finished;
    document.getElementById("drawColor").disabled = readonly;
    document.getElementById("drawWidth").disabled = readonly;
    document.getElementById("drawUndo").disabled = readonly || !drawStrokes.length || drawSyncBusy;
    document.getElementById("drawClear").disabled = readonly || !drawStrokes.length || drawSyncBusy;
    document.getElementById("drawGuessAction").disabled = readonly || !drawStrokes.length || drawSyncBusy;
    const overlay = document.getElementById("drawCanvasOverlay");
    overlay.hidden = !(room.status === "waiting" || room.status === "setup") || !room.is_player;
    document.getElementById("drawOverlayTitle").textContent = room.status === "waiting" ? "先加入玩家席" : "准备开始作画";
    document.getElementById("drawOverlayText").textContent = room.status === "waiting" ? "绑定身份后点击加入对局" : "点击右侧开始作画";
    const history = document.getElementById("drawHistory");
    history.replaceChildren();
    const guesses = Array.isArray(game.guesses) ? game.guesses : [];
    if (!guesses.length) {
      const empty = document.createElement("p");
      empty.className = "draw-empty";
      empty.textContent = "Bot 的每次猜测会显示在这里";
      history.appendChild(empty);
    } else {
      guesses.forEach((item) => {
        const entry = document.createElement("div");
        entry.className = `draw-guess ${item.correct ? "correct" : "wrong"}`;
        entry.textContent = `第 ${item.number} 次：${item.guess}${item.correct ? " · 猜中" : " · 不对"}`;
        history.appendChild(entry);
      });
    }
  }

  function drawStroke(stroke) {
    const points = Array.isArray(stroke?.points) ? stroke.points : [];
    if (!points.length) return;
    drawContext.beginPath();
    drawContext.strokeStyle = stroke.color || "#202522";
    drawContext.lineWidth = Number(stroke.width || 5);
    drawContext.lineCap = "round";
    drawContext.lineJoin = "round";
    points.forEach(([x, y], index) => {
      const px = Number(x) * drawCanvas.width;
      const py = Number(y) * drawCanvas.height;
      if (index === 0) drawContext.moveTo(px, py);
      else drawContext.lineTo(px, py);
    });
    if (points.length === 1) drawContext.lineTo(Number(points[0][0]) * drawCanvas.width + .1, Number(points[0][1]) * drawCanvas.height + .1);
    drawContext.stroke();
  }

  function drawPoint(event) {
    const rect = drawCanvas.getBoundingClientRect();
    return [
      Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
      Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
    ];
  }

  function beginDrawing(event) {
    if (busy || !room?.is_player || room.status !== "active" || room.game?.processing || room.game?.finished) return;
    event.preventDefault();
    drawCanvas.setPointerCapture?.(event.pointerId);
    activeDrawStroke = {
      color: document.getElementById("drawColor").value || "#202522",
      width: Number(document.getElementById("drawWidth").value || 5),
      points: [drawPoint(event)],
    };
    drawStrokes.push(activeDrawStroke);
    renderDrawGuess();
  }

  function continueDrawing(event) {
    if (!activeDrawStroke) return;
    event.preventDefault();
    activeDrawStroke.points.push(drawPoint(event));
    renderDrawGuess();
  }

  async function finishDrawing(event) {
    if (!activeDrawStroke) return;
    event.preventDefault();
    activeDrawStroke = null;
    await syncDrawing();
  }

  async function syncDrawing() {
    if (!room?.is_player || drawSyncBusy) return false;
    drawSyncBusy = true;
    renderDrawGuess();
    try {
      const data = await request("POST", "draw/strokes", { visitor_token: visitorToken, strokes: drawStrokes });
      room = data.room;
      drawRevision = Number(room.game?.revision ?? drawRevision);
      return true;
    } catch (error) {
      showToast(error?.message || "画布同步失败");
      try { await loadState(); } catch (_syncError) { /* polling will retry */ }
      return false;
    } finally {
      drawSyncBusy = false;
      render();
    }
  }

  async function changeDrawing(nextStrokes) {
    if (busy || !room?.is_player || room.status !== "active" || room.game?.processing || room.game?.finished) return;
    drawStrokes = nextStrokes;
    await syncDrawing();
  }

  async function guessDrawing() {
    if (busy || drawSyncBusy || !room?.is_player || room.status !== "active" || !drawStrokes.length) return;
    busy = true;
    renderDrawGuess();
    try {
      if (activeDrawStroke) activeDrawStroke = null;
      if (!(await syncDrawing())) return;
      const format = drawCanvas.toDataURL("image/webp", 0.78);
      const data = await request("POST", "draw/guess", { visitor_token: visitorToken, image_data_url: format });
      room = data.room;
      showToast(data.correct ? "Bot 猜中了" : `Bot 猜：${data.guess}`);
    } catch (error) {
      try { await loadState(); } catch (_syncError) { /* polling will retry */ }
      showToast(error?.message || "Bot 暂时无法看图");
    } finally {
      busy = false;
      render();
    }
  }

  async function diceAction(action) {
    if (busy || room?.game_type !== "pig_dice") return;
    busy = true;
    renderPigDice();
    try {
      const data = await request("POST", "dice/action", { visitor_token: visitorToken, action });
      room = data.room;
      render();
    } catch (error) {
      try { await loadState(); } catch (_syncError) { /* polling will retry */ }
      showToast(error?.message || "骰子操作失败");
    } finally {
      busy = false;
      render();
    }
  }

  async function submitChat(event) {
    event.preventDefault();
    if (chatBusy || !room) return;
    const text = chatInput.value.trim();
    if (!text) return;
    chatBusy = true;
    const optimisticId = `pending-${Date.now()}`;
    if (!Array.isArray(room.messages)) room.messages = [];
    room.messages.push({
      id: optimisticId,
      role: "user",
      message_type: "chat",
      content: text,
      sender_name: room.player_confirmed ? (room.visitor_display_name || "已绑定观众") : "匿名观众",
      sender_number: room.visitor_number,
    });
    render();
    try {
      const data = await request("POST", "chat", { visitor_token: visitorToken, text });
      room = data.room;
      chatInput.value = "";
      chatInput.style.height = "";
    } catch (error) {
      room.messages = room.messages.filter((message) => message.id !== optimisticId);
      try { await loadState(); } catch (_syncError) { /* polling will retry */ }
      showToast(error?.message || "消息发送失败");
    } finally {
      chatBusy = false;
      render();
    }
  }

  function renderTurn() {
    const stone = document.getElementById("turnStone");
    const label = document.getElementById("turnLabel");
    stone.className = "turn-stone";
    stone.textContent = "";
    if (room.game_type === "turtle_soup") {
      stone.classList.add("o");
      stone.textContent = "?";
      const game = room.game;
      label.textContent = game?.preparing
        ? "Bot 出题中"
        : game?.processing
          ? "Bot 判断中"
          : room.status === "finished"
            ? (game?.mode === "player_host" ? (game?.bot_solved ? "Bot 已猜中" : "出题结束") : (game?.solved ? "已经解开" : "汤底揭晓"))
            : room.status === "paused"
              ? "已经暂停"
              : game?.phase === "ready"
                ? `轮到 ${room.current_player_name ? `${room.current_player_name}（${room.current_player_number}号）` : `${room.current_player_number || "?"}号`}${game?.mode === "player_host" ? "给线索" : "提问"}`
                : statusLabel(room.status);
      return;
    }
    if (room.game_type === "draw_guess") {
      stone.classList.add("o");
      stone.textContent = room.game?.solved ? "✓" : "✎";
      label.textContent = room.game?.processing
        ? "Bot 看图中"
        : room.status === "finished"
        ? (room.game?.solved ? "合作猜中" : "本轮结束")
        : room.is_player ? "轮到你作画" : "观看玩家作画";
      return;
    }
    if (room.game_type === "pig_dice") {
      stone.classList.add("o");
      stone.textContent = room.game?.last_roll || "?";
      label.textContent = room.game?.finished
        ? (room.game.winner === "human" ? "玩家获胜" : "Bot 获胜")
        : room.status === "paused"
          ? "已经暂停"
          : room.game?.turn === "human" ? "玩家回合" : "Bot 回合";
      return;
    }
    if (!room.game) {
      label.textContent = statusLabel(room.status);
      return;
    }
    const xiangqi = room.game_type === "xiangqi";
    const tictactoe = room.game_type === "tictactoe";
    const humanSide = xiangqi
      ? room.game.human_side
      : (tictactoe ? room.game.human_mark : room.game.human_color);
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
    } else if (tictactoe) {
      stone.classList.add(room.game.turn === 1 ? "x" : "o");
      stone.textContent = room.game.turn === 1 ? "X" : "O";
    } else {
      stone.classList.add(room.game.turn === 1 ? "black" : "white");
    }
  }

  function drawBoard() {
    if (["turtle_soup", "pig_dice", "draw_guess"].includes(room?.game_type)) return;
    if (room?.game_type === "xiangqi") drawXiangqi();
    else if (room?.game_type === "tictactoe") drawTicTacToe();
    else drawGomoku();
  }

  function drawTicTacToe() {
    const size = board.width;
    const inset = 58;
    const playSize = size - inset * 2;
    const cell = playSize / 3;
    context.clearRect(0, 0, size, size);
    context.fillStyle = "#f3f0e8";
    context.fillRect(0, 0, size, size);
    context.strokeStyle = "#3e4a44";
    context.lineWidth = 8;
    context.lineCap = "round";
    for (let index = 1; index < 3; index += 1) {
      const position = inset + index * cell;
      context.beginPath();
      context.moveTo(position, inset);
      context.lineTo(position, size - inset);
      context.stroke();
      context.beginPath();
      context.moveTo(inset, position);
      context.lineTo(size - inset, position);
      context.stroke();
    }
    const cells = (room?.game?.board || []).map((row) => row.slice());
    if (
      pendingMove?.kind === "tictactoe"
      && !cells?.[pendingMove.row]?.[pendingMove.column]
    ) {
      cells[pendingMove.row][pendingMove.column] = pendingMove.mark;
    }
    const lastMove = pendingMove?.kind === "tictactoe"
      ? [pendingMove.row, pendingMove.column]
      : room?.game?.last_move;
    if (Array.isArray(lastMove)) {
      context.fillStyle = "rgba(33, 92, 69, .09)";
      context.fillRect(
        inset + lastMove[1] * cell + 10,
        inset + lastMove[0] * cell + 10,
        cell - 20,
        cell - 20,
      );
    }
    cells.forEach((row, rowIndex) => row.forEach((mark, columnIndex) => {
      if (mark) drawTicTacToeMark(rowIndex, columnIndex, mark, inset, cell);
    }));
  }

  function drawTicTacToeMark(row, column, mark, inset, cell) {
    const centerX = inset + (column + 0.5) * cell;
    const centerY = inset + (row + 0.5) * cell;
    const radius = cell * 0.27;
    context.lineWidth = 15;
    context.lineCap = "round";
    if (mark === 1) {
      context.strokeStyle = "#a33d35";
      context.beginPath();
      context.moveTo(centerX - radius, centerY - radius);
      context.lineTo(centerX + radius, centerY + radius);
      context.moveTo(centerX + radius, centerY - radius);
      context.lineTo(centerX - radius, centerY + radius);
      context.stroke();
      return;
    }
    context.strokeStyle = "#236a72";
    context.beginPath();
    context.arc(centerX, centerY, radius, 0, Math.PI * 2);
    context.stroke();
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
    if (room.game_type === "pig_dice") return;
    if (room.game_type === "xiangqi") await moveXiangqi(event);
    else if (room.game_type === "tictactoe") await moveTicTacToe(event);
    else await moveGomoku(event);
  }

  async function moveTicTacToe(event) {
    if (room.game.turn !== room.game.human_mark) return;
    const rect = board.getBoundingClientRect();
    const scaleX = board.width / rect.width;
    const scaleY = board.height / rect.height;
    const x = (event.clientX - rect.left) * scaleX;
    const y = (event.clientY - rect.top) * scaleY;
    const inset = 58;
    const cell = (board.width - inset * 2) / 3;
    const column = Math.floor((x - inset) / cell);
    const row = Math.floor((y - inset) / cell);
    if (row < 0 || row > 2 || column < 0 || column > 2) return;
    if (room.game.board?.[row]?.[column]) {
      showToast("这个位置已经有棋子了");
      return;
    }
    busy = true;
    pendingMove = { kind: "tictactoe", row, column, mark: room.game.human_mark };
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
  document.querySelectorAll("[data-room-view]").forEach((button) => {
    button.addEventListener("click", () => setRoomView(button.dataset.roomView));
  });
  document.getElementById("seatAction").addEventListener("click", seatAction);
  document.getElementById("chatComposer").addEventListener("submit", submitChat);
  chatInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      document.getElementById("chatComposer").requestSubmit();
    }
  });
  chatInput.addEventListener("input", () => {
    chatInput.style.height = "auto";
    chatInput.style.height = `${Math.min(chatInput.scrollHeight, 126)}px`;
  });
  document.getElementById("diceRollAction").addEventListener("click", () => diceAction("roll"));
  document.getElementById("diceHoldAction").addEventListener("click", () => diceAction("hold"));
  drawCanvas.addEventListener("pointerdown", beginDrawing);
  drawCanvas.addEventListener("pointermove", continueDrawing);
  drawCanvas.addEventListener("pointerup", finishDrawing);
  drawCanvas.addEventListener("pointercancel", finishDrawing);
  document.getElementById("drawUndo").addEventListener("click", () => changeDrawing(drawStrokes.slice(0, -1)));
  document.getElementById("drawClear").addEventListener("click", () => changeDrawing([]));
  document.getElementById("drawGuessAction").addEventListener("click", guessDrawing);
  board.addEventListener("click", moveAt);
  window.addEventListener("pagehide", (event) => {
    if (!event.persisted) notifyLeave();
  });
  setRoomView("game");
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
