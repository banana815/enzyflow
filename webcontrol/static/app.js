/* Enzyflow local control panel. Motion requests are never retried. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const token = document.querySelector('meta[name="enzyflow-token"]').content;
  const uuid = () => window.crypto.randomUUID ? window.crypto.randomUUID() : Array.from(window.crypto.getRandomValues(new Uint8Array(16)), (n) => n.toString(16).padStart(2, "0")).join("");
  const clientId = uuid();
  const terminalStates = new Set(["done", "complete", "completed", "cancelled", "canceled", "failed", "error", "stopped", "finished"]);
  const defaultAxes = [1, 2, 3].map((id) => ({ id, name: id === 1 ? "长轴" : id === 2 ? "短轴 A" : "短轴 B", min_mm: 30, max_mm: id === 1 ? 310 : 210, physical_min_mm: 20, physical_max_mm: id === 1 ? 320 : 220, position_mm: null, anchored: false, lead_mm: 4, forward_sign: -1 }));
  let state = { connected: false, connecting: false, owner: null, configured: false, axes: defaultAxes, job: null, events: [] };
  let online = false;
  let lastSeen = 0;
  let pollInFlight = false;
  let requestInFlight = 0;
  let heartbeatInFlight = false;
  let initialClaimAttempted = false;
  let setupManuallyOpened = false;
  let localOperation = null;
  let lastEventSignature = "";
  let lastSession = null;
  const editedAnchorPositions = new Set();
  let toastTimer;
  const localEvents = [];

  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const mm = (value) => finite(value) ? `${value.toFixed(1)} mm` : "—";
  const axisFor = (id) => state.axes.find((axis) => Number(axis.id) === id) || defaultAxes[id - 1];
  const owns = () => state.owner === clientId || (state.owner && typeof state.owner === "object" && state.owner.client_id === clientId);
  const activeJob = () => state.job && !terminalStates.has(String(state.job.state || "running").toLowerCase());
  const pendingOperation = () => Boolean(state.connecting || state.operation || state.pending || localOperation);
  const busy = () => Boolean(activeJob() || pendingOperation());
  const fresh = () => online && Date.now() - lastSeen < 2500;
  const canControl = () => fresh() && owns() && state.connected && !state.estop_latched && !requestInFlight && !busy();
  const configured = () => state.configured === true;

  function note(message, level = "info") {
    localEvents.push({ time: new Date().toISOString(), level, message });
    if (localEvents.length > 40) localEvents.shift();
    renderEvents();
  }

  function showError(message) {
    $("error-message").textContent = message;
    $("error-banner").hidden = false;
    note(message, "error");
  }

  function toast(message) {
    clearTimeout(toastTimer);
    $("toast").textContent = message;
    $("toast").hidden = false;
    toastTimer = setTimeout(() => { $("toast").hidden = true; }, 4200);
  }

  function errorText(value) {
    if (typeof value === "string" && value.trim()) return value;
    if (value && typeof value.message === "string") return value.message;
    return "操作未完成，请查看设备状态与操作记录。";
  }

  async function fetchJson(path, options = {}, timeout = 7000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(path, { cache: "no-store", ...options, signal: controller.signal });
      let body;
      try { body = await response.json(); } catch (_) { throw new Error("控制服务返回了无法识别的响应，请检查服务窗口。"); }
      if (!response.ok || body.ok === false) throw new Error(errorText(body.error || body.detail || `请求失败（${response.status}）`));
      return body;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("请求结果未确认。请等待状态更新，不要重复发送运动指令。");
      if (error instanceof TypeError) throw new Error("无法连接本机控制服务。请检查启动窗口；连接恢复后需核对设备状态。");
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  async function post(op, args = {}, options = {}) {
    if (requestInFlight && !options.priority) return false;
    if (!options.silent) { requestInFlight += 1; render(); }
    try {
      const response = await fetchJson(`/api/${op}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Enzyflow-Token": token },
        body: JSON.stringify({ client_id: clientId, request_id: uuid(), ...args }),
      });
      if (response.state) acceptState(response.state);
      if (!options.silent) $("error-banner").hidden = true;
      return response;
    } catch (error) {
      if (!options.silent) showError(error.message);
      return false;
    } finally {
      if (!options.silent) { requestInFlight -= 1; render(); }
    }
  }

  function acceptState(next) {
    if (!next || !Array.isArray(next.axes)) throw new Error("控制服务状态不完整，暂不允许运动。");
    const wasPumping = state.job?.kind === "pump" && activeJob();
    state = { ...next, axes: next.axes.length ? next.axes : defaultAxes, events: Array.isArray(next.events) ? next.events : [] };
    if (wasPumping && (!state.connected || !activeJob())) {
      $("pump-confirm").checked = false;
      for (const id of [1,2,3]) $(`pump-remaining-${id}`).value = "";
    }
    online = true;
    lastSeen = Date.now();
    if (lastSession !== null && state.session !== lastSession) {
      setupManuallyOpened = true;
      localOperation = null;
      editedAnchorPositions.clear();
      for (const id of [1, 2, 3]) {
        $(`anchor-mm-${id}`).value = "";
        $(`anchor-sign-${id}`).value = "";
        $(`anchor-lead-${id}`).value = "";
        $(`pump-remaining-${id}`).value = "";
        $(`pump-direction-${id}`).value = "";
        $("pump-confirm").checked = false;
      }
      $("mapping-confirmed").checked = false;
    }
    lastSession = state.session;
    if (localOperation) {
      const finished = localOperation.kind === "configure" ? state.configured === true : axisFor(localOperation.axis).anchored === true;
      if (finished || Date.now() - localOperation.started > 10000 || !state.connected) localOperation = null;
    }
    render();
  }

  async function poll() {
    if (pollInFlight) return;
    pollInFlight = true;
    try {
      acceptState(await fetchJson("/api/state", {}, 3500));
      if (!initialClaimAttempted) {
        initialClaimAttempted = true;
        if (!state.owner) await post("claim", {}, { silent: true });
      }
    } catch (_) {
      online = false;
      render();
    } finally {
      pollInFlight = false;
    }
  }

  async function heartbeat() {
    if (heartbeatInFlight || !online || !owns()) return;
    heartbeatInFlight = true;
    try { await post("heartbeat", {}, { silent: true, priority: true }); }
    finally { heartbeatInFlight = false; }
  }

  function buildCards() {
    for (const id of [1, 2, 3]) {
      const card = document.createElement("article");
      card.className = "axis-card";
      card.id = `axis-${id}`;
      card.setAttribute("aria-labelledby", `axis-name-${id}`);
      card.innerHTML = `
        <div class="axis-card-head"><div class="axis-ident"><span class="axis-index">ID ${id}</span><h3 id="axis-name-${id}"></h3></div><span class="axis-badge" id="axis-badge-${id}">待标定</span></div>
        <div class="axis-position"><div><div class="position-caption">当前位置</div><div class="position-value unknown" id="position-value-${id}">—<small>mm</small></div></div><div class="position-origin" id="position-source-${id}">尚未建立<br>本次位置基准</div></div>
        <div class="axis-range"><div class="rail" id="rail-${id}" role="meter" aria-label="当前位置与允许范围"><div class="rail-fill" id="rail-fill-${id}"></div><div class="rail-marker" id="rail-marker-${id}" hidden></div></div><div class="range-labels"><span id="min-label-${id}"></span><span>允许目标范围</span><span id="max-label-${id}"></span></div></div>
        <div class="axis-controls">
          <div class="speed-section"><label class="control-label" for="rpm-${id}"><span>运行速度</span><span class="unit-conversion" id="speed-conversion-${id}">0.80 mm/s</span></label><div class="speed-control"><input id="speed-slider-${id}" type="range" min="3" max="30" step="1" value="12" aria-label="ID ${id} 速度调节"><div class="speed-number"><input id="rpm-${id}" type="number" min="3" max="30" step="1" value="12" inputmode="numeric" aria-label="ID ${id} 转速"><span>rpm</span></div></div></div>
          <div class="target-control"><label class="control-label" for="target-${id}">移动到指定位置</label><div class="target-row"><div class="target-field"><input id="target-${id}" type="number" step="0.1" inputmode="decimal" placeholder="输入目标位置" autocomplete="off"><span>mm</span></div><button class="button button-primary" id="move-${id}" type="button" disabled>移动 <span aria-hidden="true">↗</span></button></div></div>
          <div class="jog-control"><div class="control-label"><label for="step-${id}">定距连续移动</label><select class="step-select" id="step-${id}" aria-label="ID ${id} 移动距离"><option value="0.1">0.1 mm</option><option value="0.5" selected>0.5 mm</option><option value="1">1.0 mm</option></select></div><div class="jog-buttons"><button class="button" id="back-${id}" type="button" disabled><span class="jog-symbol" aria-hidden="true">−</span>后退</button><button class="button" id="forward-${id}" type="button" disabled><span class="jog-symbol" aria-hidden="true">＋</span>前进</button></div></div>
        </div>
        <div class="axis-controls"><label class="checkbox-label"><input type="checkbox" id="group-${id}"><span>参与同时启动</span></label><div id="recovery-${id}" hidden><p>安全区外：核实当前位置后，仅可低速向内恢复。</p><label class="checkbox-label"><input type="checkbox" id="recover-confirm-${id}"><span>已确认向内方向和可用空间</span></label><button class="button button-outline" id="recover-${id}" type="button" disabled>向安全区内移动 0.5 mm</button><p>固定 6 rpm，每次最多 0.5 mm；进入安全区后自动禁用。</p></div></div>
        <div class="axis-footer"><span><i class="mini-lock" aria-hidden="true"></i><span id="axis-footnote-${id}">等待标定后解锁</span></span><button class="calibrate-link" id="calibrate-${id}" type="button">标定位置</button></div>`;
      $("axis-grid").append(card);

      const calibration = document.createElement("div");
      calibration.className = "calibration-card";
      calibration.innerHTML = `<div class="calibration-title"><strong id="calibration-name-${id}"></strong><span id="anchor-status-${id}">未标定</span></div><div class="calibration-input-row"><label class="input-label" for="anchor-mm-${id}">当前尺量位置<div class="input-with-unit"><input id="anchor-mm-${id}" type="number" step="0.1" min="20" inputmode="decimal" placeholder="请实测后填写" autocomplete="off"><span>mm</span></div></label></div><div class="calibration-assumptions"><details><summary>方向与导程（沿用既有设置）</summary><label>前进方向 <select id="anchor-sign-${id}"><option value="">沿用（默认反转）</option><option value="-1">反转（−1）</option><option value="1">正转（+1）</option></select></label><label>导程 <select id="anchor-lead-${id}"><option value="">沿用（默认 4 mm / 圈）</option><option value="4">4 mm / 圈</option><option value="2">2 mm / 圈</option></select></label></details></div>`;
      $("calibration-grid").append(calibration);
      const pumpCard = document.createElement("div");
      pumpCard.className = "calibration-card";
      pumpCard.id = `pump-card-${id}`;
      pumpCard.innerHTML = `<strong id="pump-name-${id}">ID ${id}</strong><label class="input-label">注射速率（mL/min）<input id="pump-flow-${id}" type="number" min="0.01" step="0.01" value="1"></label><small id="pump-rate-${id}"></small><label class="input-label">推动注射器的方向<select id="pump-direction-${id}"><option value="">请选择实际推注方向</option><option value="1">前进（坐标增大）</option><option value="-1">后退（坐标减小）</option></select></label><label class="input-label">推杆实际剩余可推距离（mm）<input id="pump-remaining-${id}" type="number" min="1.5" max="200" step="0.1" placeholder="尺量后填写"></label>`;
      pumpCard.innerHTML += `<label class="checkbox-label"><input type="checkbox" id="pump-plan-${id}"><span>按距离分段调速</span></label><label class="input-label" id="pump-plan-input-${id}" hidden>每行填写距离（mm）、速率（mL/min）<textarea id="pump-segments-${id}" rows="3" placeholder="2, 0.5&#10;3, 1"></textarea></label>`;
      $("pump-inputs").append(pumpCard);
      const resetCard = document.createElement("div");
      resetCard.className = "calibration-card";
      resetCard.innerHTML = `<label class="input-label"><span id="reset-name-${id}">ID ${id} 复位目标</span><input id="reset-target-${id}" type="number" step="0.1" placeholder="填写目标位置（mm）"></label>`;
      $("reset-inputs").append(resetCard);
      $(`reset-target-${id}`).addEventListener("input", () => { $("reset-confirm").checked = false; render(); });
      $(`pump-flow-${id}`).addEventListener("input", () => { $("pump-confirm").checked = false; render(); });
      for (const field of ["plan", "segments"]) {
        $(`pump-${field}-${id}`).addEventListener("input", () => { $("pump-confirm").checked = false; render(); });
      }
      $(`pump-direction-${id}`).addEventListener("change", () => { $("pump-confirm").checked = false; render(); });
      $(`pump-remaining-${id}`).addEventListener("input", () => { $("pump-confirm").checked = false; render(); });

      $(`speed-slider-${id}`).addEventListener("input", (event) => { $(`rpm-${id}`).value = event.target.value; updateSpeed(id); });
      $(`rpm-${id}`).addEventListener("input", () => { const value = Number($(`rpm-${id}`).value); if (Number.isFinite(value)) $(`speed-slider-${id}`).value = value; updateSpeed(id); });
      $(`step-${id}`).addEventListener("change", render);
      $(`group-${id}`).addEventListener("change", render);
      $(`recover-confirm-${id}`).addEventListener("change", render);
      $(`recover-${id}`).addEventListener("click", () => recover(id));
      $(`move-${id}`).addEventListener("click", () => move(id));
      $(`target-${id}`).addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); if (!$(`move-${id}`).disabled) move(id); } });
      $(`back-${id}`).addEventListener("click", () => jog(id, -1));
      $(`forward-${id}`).addEventListener("click", () => jog(id, 1));
      $(`anchor-mm-${id}`).addEventListener("input", () => editedAnchorPositions.add(id));
      $(`calibrate-${id}`).addEventListener("click", () => { openSetup(); $(`anchor-mm-${id}`).focus(); });
    }
  }

  function updateSpeed(id) {
    const rpm = Number($(`rpm-${id}`).value);
    const lead = axisFor(id).lead_mm || 4;
    $(`speed-conversion-${id}`).textContent = Number.isInteger(rpm) && rpm >= 3 && rpm <= 30 ? `${(rpm * lead / 60).toFixed(2)} mm/s` : "请输入 3–30 rpm";
  }

  function validRpm(id) {
    const rpm = Number($(`rpm-${id}`).value);
    if (!Number.isInteger(rpm) || rpm < 3 || rpm > 30) throw new Error("速度请输入 3–30 之间的整数，单位 rpm。");
    return rpm;
  }

  async function move(id) {
    if (!canControl() || !axisFor(id).anchored) return;
    try {
      const value = $(`target-${id}`).value.trim();
      const target = Number(value);
      const axis = axisFor(id);
      if (!value || !Number.isFinite(target)) throw new Error("请填写目标位置，单位 mm。");
      if (target < axis.min_mm || target > axis.max_mm) throw new Error(`${axis.name}的目标必须在 ${axis.min_mm}–${axis.max_mm} mm 内，以保留两端余量。`);
      if (finite(axis.position_mm) && Math.abs(target - axis.position_mm) < 0.1) throw new Error("目标距离不足 0.1 mm，无需继续移动。");
      const result = await post("move", { axis: id, target_mm: target, rpm: validRpm(id) });
      if (result) toast("已提交目标，等待设备反馈。");
    } catch (error) { showError(error.message); }
  }

  async function jog(id, direction) {
    if (!canControl() || !axisFor(id).anchored) return;
    try {
      const distance = Number($(`step-${id}`).value) * direction;
      const axis = axisFor(id);
      if (!finite(axis.position_mm)) throw new Error("当前位置未知，请先建立本次位置基准。");
      const target = axis.position_mm + distance;
      if (target < axis.min_mm || target > axis.max_mm) throw new Error("这一步会进入预留余量，已取消移动。");
      await post("jog", { axis: id, delta_mm: distance, rpm: validRpm(id) });
    } catch (error) { showError(error.message); }
  }

  async function recover(id) {
    if (!canControl() || !$(`recover-confirm-${id}`).checked) return;
    const axis = axisFor(id);
    if (!axis.anchored || !finite(axis.position_mm)) return;
    const below = axis.position_mm < axis.min_mm;
    if (!below && axis.position_mm <= axis.max_mm) return;
    const distance = Math.min(0.5, Math.max(0.1, below ? axis.min_mm + 0.1 - axis.position_mm : axis.position_mm - axis.max_mm + 0.1));
    await post("recover", {axis: id, delta_mm: below ? distance : -distance, rpm: 6, confirmed: true});
  }

  async function moveGroup() {
    if (!canControl()) return;
    try {
      const moves = [1,2,3].filter(id => $(`group-${id}`).checked).map(id => {
        const text = $(`target-${id}`).value.trim();
        if (!text || !Number.isFinite(Number(text))) throw new Error("请填写每个选择轴的目标位置。");
        return {axis: id, target_mm: Number(text), rpm: validRpm(id)};
      });
      if (moves.length < 2) throw new Error("请选择至少两个轴；单轴请点击该轴的移动按钮。");
      await post("move_group", {moves});
    } catch (error) { showError(error.message); }
  }

  async function resetPositions() {
    if (!canControl() || !$("reset-confirm").checked) return;
    try {
      const moves = [1,2,3].map(id => {
        const text = $(`reset-target-${id}`).value.trim();
        if (!text || !Number.isFinite(Number(text))) throw new Error("请分别填写三轴复位目标（mm）。");
        return {axis:id, target_mm:Number(text)};
      });
      const rpm = Number($("reset-rpm").value);
      if (!Number.isInteger(rpm) || rpm<3 || rpm>30) throw new Error("复位转速必须为3–30rpm整数。");
      await post("reset_positions", {moves, rpm, confirmed:true});
    } catch (error) { showError(error.message); }
  }

  async function pumpStart() {
    if (!canControl() || !$("pump-confirm").checked) return;
    try {
      const items = state.axes.filter(a => a.id !== state.long_id).map(axis => {
        const text = $(`pump-remaining-${axis.id}`).value.trim();
        const direction = Number($(`pump-direction-${axis.id}`).value);
        if (!text || !Number.isFinite(Number(text)) || ![-1,1].includes(direction)) throw new Error("请填写两个短轴的推注方向与实际剩余行程。");
        const item = {axis:axis.id, direction, remaining_mm:Number(text)};
        const checkRate = flow => {
          const rpm = flow*1000/(Math.PI*(19.22/2)**2)/axis.lead_mm;
          if (!(rpm >= 0.1 && rpm <= 2)) throw new Error(`请填写 ID${axis.id} 允许范围内的注射速率。`);
          return flow;
        };
        if ($(`pump-plan-${axis.id}`).checked) {
          const lines = $(`pump-segments-${axis.id}`).value.trim().split(/\r?\n/).filter(line => line.trim());
          if (lines.length < 1 || lines.length > 12) throw new Error(`ID${axis.id} 须填写 1–12 段。`);
          item.segments = lines.map((line, index) => {
            const fields = line.split(/[,，]/);
            const [distance, rate] = fields.map(Number);
            if (fields.length !== 2 || !Number.isFinite(distance) || distance < 0.5) {
              throw new Error(`ID${axis.id} 第 ${index+1} 段须填写距离和速率，距离至少 0.5 mm。`);
            }
            return {distance_mm:distance, flow_ml_min:checkRate(rate)};
          });
        } else {
          item.flow_ml_min = checkRate(Number($(`pump-flow-${axis.id}`).value));
        }
        return item;
      });
      if (await post("pump_start", {items, confirmed:true})) toast("已提交注射请求。");
    } catch (error) { showError(error.message); }
  }

  async function anchorAll() {
    if (!canControl() || !configured() || !$("anchor-confirm").checked) return;
    if (state.supports_batch_anchor !== true) {
      showError("当前运行的电机网页服务未加载三轴标定功能。请退出旧服务，重新启动后刷新页面。");
      return;
    }
    const positions = [];
    for (const id of [1, 2, 3]) {
      const input = $(`anchor-mm-${id}`);
      const axis = axisFor(id);
      const lower = axis.physical_min_mm ?? 20;
      const upper = axis.physical_max_mm ?? (axis.max_mm + 10);
      const measured = Number(input.value);
      if (!input.value.trim() || !finite(measured) || measured < lower || measured > upper) {
        showError(`ID${id} 请输入 ${lower}–${upper} mm 内的实测位置。`);
        input.focus();
        return;
      }
      const settings = {axis:id, mm:measured};
      if (["-1", "1"].includes($(`anchor-sign-${id}`).value)) settings.sign = Number($(`anchor-sign-${id}`).value);
      if (["2", "4"].includes($(`anchor-lead-${id}`).value)) settings.lead = Number($(`anchor-lead-${id}`).value);
      positions.push(settings);
    }
    if (await post("anchor", {positions, confirmed:true})) toast("已提交三轴位置，等待编码器确认。");
  }

  function openSetup() {
    setupManuallyOpened = true;
    render();
    $("setup-panel").scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  }

  function render() {
    const live = fresh();
    const controlled = owns();
    const working = Boolean(activeJob());
    const enabled = canControl();
    const allAnchored = state.connected && state.axes.length === 3 && state.axes.every((axis) => axis.anchored);
    const hasUnsafePosition = state.connected && state.axes.some((axis) => axis.anchored && finite(axis.position_mm) && (axis.position_mm < axis.min_mm || axis.position_mm > axis.max_mm));
    // Emergency remains clickable even when polling is stale or another request is pending.
    $("estop-button").disabled = false;
    $("estop-banner").hidden = !state.estop_latched;
    $("estop-detail").textContent = state.estop_confirmed === true ? "三轴已失能并确认静止；排除故障后可恢复驱动，无需例行断电，恢复后须核对位置。" : state.estop_confirmed === false ? "停机未确认，请切断电机电源。" : "急停信号已发出，等待反馈。";
    $("estop-clear").disabled = !live || !controlled || state.connected || state.connecting || state.estop_confirmed === null || busy() || requestInFlight || !$("estop-clear-confirm").checked;
    if (!state.estop_latched) $("estop-clear-confirm").checked = false;
    $("drive-restore").disabled = !enabled || !state.capabilities?.includes('drive_restore') || !$("drive-restore-confirm").checked;
    $("drive-restore-confirm").disabled = !enabled;
    $("reset-rpm").disabled = !enabled;
    $("reset-confirm").disabled = !enabled || !allAnchored;
    $("reset-start").disabled = !enabled || !state.capabilities?.includes("continuous_reset") || !allAnchored || !$("reset-confirm").checked;
    if (!allAnchored) $("reset-confirm").checked = false;
    $("connection-light").className = `status-light${!live ? " error" : state.connecting || working ? " busy" : state.connected ? " online" : ""}`;
    $("connection-title").textContent = !live ? "控制服务未连接" : state.connecting ? "正在连接 ESP32" : working ? "设备正在运动" : state.connected ? "设备已连接" : "设备未连接";
    $("connection-detail").textContent = !live ? "请检查本机启动窗口" : hasUnsafePosition ? "有轴位于安全区外，请核实位置" : state.demo ? "模拟模式 · 不连接真实电机" : state.connected ? allAnchored ? "位置基准已建立" : "请完成本次位置标定" : "USB 连接后点击连接设备";
    $("port-name").textContent = state.port || "串口待连接";
    $("device-name").textContent = state.demo ? "模拟设备 · 无硬件运动" : (typeof state.device === "string" && state.device) || "ESP32 · USB / RS485";
    $("connect-button").hidden = Boolean(state.connected);
    $("connect-button").disabled = state.estop_latched || !live || !controlled || Boolean(state.connecting) || requestInFlight || working;
    $("connect-button").textContent = state.connecting ? "正在连接…" : "连接设备";
    $("disconnect-button").hidden = !state.connected;
    $("disconnect-button").disabled = !live || !controlled || requestInFlight || working || pendingOperation();
    // Cancellation remains available to a local viewer; starting motion still
    // requires the single control lease enforced by the server.
    $("stop-button").disabled = !live || !state.connected;
    $("stop-button").textContent = working && state.job?.kind === "pump" ? "停止注射" : working && state.job?.kind === "reset_continuous" ? "停止运动" : "停止";
    const pumpCapable = state.capabilities?.includes("pump_rates");
    const shorts = state.axes.filter(a => a.id !== state.long_id);
    const pumpReady = shorts.length === 2 && shorts.every(a => a.anchored && finite(a.position_mm) && a.position_mm >= a.min_mm && a.position_mm <= a.max_mm);
    $("pump-start").disabled = !enabled || !pumpCapable || !pumpReady || !$("pump-confirm").checked;
    $("pump-confirm").disabled = !enabled || !pumpCapable || !pumpReady;
    $("pump-hint").textContent = !pumpCapable ? "连接支持注射的固件后可用。" : !pumpReady ? "请先建立两个短轴的位置基准并进入安全区。" : "默认恒速推进。可为 A、B 分别开启分段调速。";
    $("ownership-banner").hidden = !live || Boolean(controlled);
    $("ownership-description").textContent = state.owner ? "另一页面正在控制设备。请在原页面停止并退出后再获取控制权。" : "获取控制权后才能连接、标定和移动。";
    $("claim-button").disabled = requestInFlight || Boolean(state.owner && !controlled);
    $("setup-panel").hidden = allAnchored && !setupManuallyOpened;
    $("close-setup").hidden = !allAnchored;
    $("step-connect").className = state.connected ? "completed" : "active";
    $("step-configure").className = configured() && state.connected ? "completed" : state.connected ? "active" : "";
    $("step-anchor").className = allAnchored ? "completed" : configured() && state.connected ? "active" : "";
    $("long-axis-id").disabled = !enabled;
    $("mapping-confirmed").disabled = !enabled;
    $("configure-button").disabled = !enabled || !$("mapping-confirmed").checked;
    $("configure-button").textContent = localOperation?.kind === "configure" ? "正在确认…" : configured() ? "重新确认对应关系" : "确认对应关系";
    $("footer-status").textContent = !live ? "连接中断 · 暂停操作，历史坐标不作为当前基准" : state.demo ? "模拟模式 · 所有位置仅用于界面演示" : "位置以本次尺量基准与编码器反馈换算";

    for (const id of [1, 2, 3]) {
      const axis = axisFor(id);
      const known = live && state.connected && axis.anchored && finite(axis.position_mm);
      const inSafeRange = known && finite(axis.min_mm) && finite(axis.max_mm) && axis.position_mm >= axis.min_mm && axis.position_mm <= axis.max_mm;
      const moving = working && (Number(state.job.axis) === id || state.job.members?.some(m => m.axis === id && !m.done));
      const axisEnabled = enabled && inSafeRange;
      const name = axis.name || defaultAxes[id - 1].name;
      $(`axis-name-${id}`).textContent = name;
      $(`pump-card-${id}`).hidden = id === state.long_id;
      $(`reset-name-${id}`).textContent = `${name} · ID ${id} 复位目标（mm）`;
      $(`reset-target-${id}`).disabled = !enabled || !known;
      $(`reset-target-${id}`).min = axis.min_mm;
      $(`reset-target-${id}`).max = axis.max_mm;
      $(`pump-name-${id}`).textContent = `${name} · ID ${id}`;
      const plan = $(`pump-plan-${id}`);
      const hasSegments = state.capabilities?.includes("pump_segments");
      if (!hasSegments) plan.checked = false;
      plan.disabled = !enabled || !known || !hasSegments;
      $(`pump-plan-input-${id}`).hidden = !plan.checked;
      $(`pump-segments-${id}`).disabled = plan.disabled;
      $(`pump-flow-${id}`).disabled = !enabled || !known || plan.checked;
      const flow = Number($(`pump-flow-${id}`).value);
      const area = Math.PI*(19.22/2)**2;
      const lead = axis.lead_mm;
      if (known && lead) {
        const low = Math.ceil(0.1*lead*area/1000*100)/100;
        const high = Math.floor(2*lead*area/1000*100)/100;
        $(`pump-flow-${id}`).min = low;
        $(`pump-flow-${id}`).max = high;
        $(`pump-rate-${id}`).textContent = `可设 ${low}–${high} mL/min` + (plan.checked ? "" : ` · ${flow.toFixed(2)}倍 · ${(flow*1000/area/lead).toFixed(6)} rpm`);
      } else $(`pump-rate-${id}`).textContent = "以1mL/min为1倍；标定导程后显示可用范围。";
      $(`pump-direction-${id}`).disabled = !enabled || !known;
      $(`pump-remaining-${id}`).disabled = !enabled || !known;
      if (!known && id !== state.long_id) $("pump-confirm").checked = false;
      $(`calibration-name-${id}`).textContent = `${name} · ID ${id}`;
      $(`axis-${id}`).classList.toggle("moving", Boolean(moving));
      $(`axis-badge-${id}`).textContent = moving ? "运动中" : !live || !state.connected ? "未连接" : known && !inSafeRange ? "安全区外" : known ? "已就绪" : "待标定";
      $(`axis-badge-${id}`).className = `axis-badge${moving ? " moving" : known && !inSafeRange ? " outside" : known ? " ready" : ""}`;
      const position = $(`position-value-${id}`);
      position.classList.toggle("unknown", !known);
      position.replaceChildren(document.createTextNode(known ? axis.position_mm.toFixed(1) : "—"));
      const unit = document.createElement("small"); unit.textContent = "mm"; position.append(unit);
      $(`position-source-${id}`).textContent = known ? "已建立的位置基准\n＋实时编码器反馈" : "尚未建立\n本次位置基准";
      $(`position-source-${id}`).style.whiteSpace = "pre-line";
      $(`min-label-${id}`).textContent = `${axis.min_mm} mm`;
      $(`max-label-${id}`).textContent = `${axis.max_mm} mm`;
      const physicalMin = axis.physical_min_mm ?? 20;
      const physicalMax = axis.physical_max_mm ?? (axis.max_mm + 10);
      const width = physicalMax - physicalMin;
      const marginLeft = (axis.min_mm - physicalMin) / width * 100;
      const marginRight = (physicalMax - axis.max_mm) / width * 100;
      const percentage = known ? Math.max(0, Math.min(100, (axis.position_mm - physicalMin) / width * 100)) : 0;
      const rail = $(`rail-${id}`);
      rail.style.setProperty("--margin-left", `${marginLeft}%`);
      rail.style.setProperty("--margin-right", `${marginRight}%`);
      rail.setAttribute("aria-valuemin", axis.min_mm);
      rail.setAttribute("aria-valuemax", axis.max_mm);
      rail.setAttribute("aria-valuetext", known ? `当前位置 ${mm(axis.position_mm)}` : "当前位置未知");
      if (known) rail.setAttribute("aria-valuenow", axis.position_mm); else rail.removeAttribute("aria-valuenow");
      $(`rail-marker-${id}`).hidden = !known;
      $(`rail-marker-${id}`).style.left = `${percentage}%`;
      $(`rail-fill-${id}`).style.width = `${Math.max(0, percentage - marginLeft)}%`;
      for (const element of [`rpm-${id}`, `speed-slider-${id}`, `target-${id}`, `step-${id}`]) $(element).disabled = !axisEnabled;
      $(`target-${id}`).min = axis.min_mm;
      $(`target-${id}`).max = axis.max_mm;
      $(`move-${id}`).disabled = !axisEnabled;
      $(`group-${id}`).disabled = !axisEnabled || !state.capabilities?.includes("continuous_move");
      if (!known || !inSafeRange) $(`group-${id}`).checked = false;
      const recovery = known && !inSafeRange && state.capabilities?.includes("recover");
      $(`recovery-${id}`).hidden = !recovery;
      $(`recover-confirm-${id}`).disabled = !enabled || !recovery;
      $(`recover-${id}`).disabled = !enabled || !recovery || !$(`recover-confirm-${id}`).checked;
      if (!recovery) $(`recover-confirm-${id}`).checked = false;
      if (recovery) {
        const distance = Math.min(0.5, Math.max(0.1, axis.position_mm < axis.min_mm ? axis.min_mm + 0.1-axis.position_mm : axis.position_mm-axis.max_mm+0.1));
        $(`recover-${id}`).textContent = `向内${axis.position_mm < axis.min_mm ? "前进" : "后退"} ${distance.toFixed(2)} mm`;
      }
      const step = Number($(`step-${id}`).value);
      const maxStep = axis.lead_mm === 2 ? 0.5 : 1;
      $(`step-${id}`).querySelector('option[value="1"]').disabled = maxStep < 1;
      $(`back-${id}`).disabled = !axisEnabled || step > maxStep || axis.position_mm - step < axis.min_mm;
      $(`forward-${id}`).disabled = !axisEnabled || step > maxStep || axis.position_mm + step > axis.max_mm;
      $(`axis-footnote-${id}`).textContent = known && !inSafeRange ? "安全区外 · 普通调节锁定" : known ? "两端各保留 10 mm" : "等待标定后解锁";
      $(`axis-footnote-${id}`).title = known && !inSafeRange ? `当前坐标不在 ${axis.min_mm}–${axis.max_mm} mm 内。请核实尺量基准；可使用上方的低速向内恢复。` : "";
      $(`calibrate-${id}`).disabled = working || requestInFlight;
      $(`anchor-mm-${id}`).disabled = !enabled || !configured();
      $(`anchor-mm-${id}`).max = physicalMax;
      $(`anchor-sign-${id}`).disabled = $(`anchor-lead-${id}`).disabled = !enabled || !configured();
      const savedAxis = state.saved_positions?.axes.find(a => a.id === id);
      const olderPosition = state.saved_positions?.long_id === state.long_id
        ? state.saved_positions?.reference_positions?.[id] : undefined;
      const anchorInput = $(`anchor-mm-${id}`);
      if (!editedAnchorPositions.has(id)) {
        const savedPosition = savedAxis?.position_mm ?? olderPosition;
        const defaultValue = finite(savedPosition) && savedPosition >= (axis.physical_min_mm ?? 20) && savedPosition <= physicalMax
          ? String(savedPosition) : "";
        if (anchorInput.value !== defaultValue) anchorInput.value = defaultValue;
      }
      anchorInput.title = finite(olderPosition) || (savedAxis && !state.saved_positions.reliable) ? "上次记录仅供参考，请核对实测位置。" : "";
      $(`anchor-status-${id}`).textContent = localOperation?.kind === "anchor" && localOperation.axis === id ? "正在核对…" : known ? "已建立基准" : "未标定";
      updateSpeed(id);
    }
    $("anchor-confirm").disabled = !enabled || !configured();
    $("anchor-all").disabled = !enabled || !configured() || !$("anchor-confirm").checked;
    $("move-group").disabled = !enabled || !state.capabilities?.includes("continuous_move") || [1,2,3].filter(id => $(`group-${id}`).checked).length < 2;
    const saved = state.saved_positions;
    $("saved-position-state").textContent = saved ? `${saved.reliable ? '已保存可靠位置' : '位置参考，不能恢复'} · ${new Date(saved.saved_at).toLocaleString()} · ${saved.axes.map(a=>`ID${a.id}: ${a.position_mm.toFixed(3)} mm`).join(' / ')} · ${saved.reason}` : '尚无保存位置；首次尺量建立基准后自动保存。';
    $("restore-positions").disabled = !enabled || !saved?.reliable;
    renderJob();
    renderEvents();
  }

  function renderJob() {
    const job = state.job;
    $("job-panel").hidden = !job;
    if (!job) return;
    const labels = { queued: "等待执行", running: "运动中", moving: "运动中", stopping: "等待刹车及静止反馈", cancelling: "正在取消后续动作", completed: "已完成", complete: "已完成", done: "已完成", finished: "已完成", cancelled: "已停止", canceled: "已停止", stopped: "已停止后续动作", failed: "发生异常", error: "发生异常" };
    if (job.kind === "pump") {
      $("job-title").textContent = "双注射器恒速推进";
      $("job-state").textContent = labels[job.state] || "等待反馈";
      $("job-start").textContent = job.members.map(m => `ID${m.axis}: ${mm(m.start_mm)}`).join(" / ");
      $("job-target").textContent = job.members.map(m => `ID${m.axis}: ${mm(m.end_mm)}`).join(" / ");
      const progress = job.members.length ? job.members.reduce((sum, member) => sum + (member.progress || 0), 0)/job.members.length*100 : 0;
      $("job-progress-label").textContent = `${Math.round(progress)}%`;
      $("job-progress-fill").style.width = `${progress}%`;
      $("job-detail").textContent = job.error || job.members.map(m => `ID${m.axis} · ${m.flow_ml_min ?? 1} mL/min · ${m.rpm.toFixed(6)} rpm · 估算 ${(m.volume_ml || 0).toFixed(3)} mL`).join("；");
      return;
    }
    if (job.kind === "move_group" || job.kind === "reset_continuous") {
      $("job-title").textContent = job.reset ? "三轴连续复位到指定位置" : "连续定位";
      $("job-state").textContent = labels[job.state] || "等待反馈";
      $("job-start").textContent = job.members.map(m => `ID${m.axis}: ${mm(m.start_mm)}`).join(" / ");
      $("job-target").textContent = job.members.map(m => `ID${m.axis}: ${mm(m.target_mm)}`).join(" / ");
      $("job-progress-label").textContent = `${Math.round(job.progress*100)}%`;
      $("job-progress-fill").style.width = `${job.progress*100}%`;
      $("job-detail").textContent = job.error || job.members.map(m => `ID${m.axis} · ${m.rpm} rpm · ${mm(m.position_mm)}${m.done ? " · 到位" : ""}`).join("；");
      return;
    }
    const axis = axisFor(Number(job.axis));
    let percentage = 0;
    if (finite(job.progress)) percentage = job.progress <= 1 ? job.progress * 100 : job.progress;
    else if (finite(job.start_mm) && finite(job.target_mm) && finite(job.position_mm) && job.target_mm !== job.start_mm) percentage = Math.abs((job.position_mm - job.start_mm) / (job.target_mm - job.start_mm)) * 100;
    percentage = Math.min(100, Math.max(0, percentage));
    $("job-title").textContent = `${axis.name || `ID ${job.axis}`} · 位置调节`;
    $("job-state").textContent = labels[job.state] || "等待设备反馈";
    $("job-start").textContent = mm(job.start_mm);
    $("job-target").textContent = mm(job.target_mm);
    $("job-progress-label").textContent = `${Math.round(percentage)}%`;
    $("job-progress-fill").style.width = `${percentage}%`;
    $("job-detail").textContent = job.error ? `任务未完成：${errorText(job.error)}` : `${job.rpm || "—"} rpm · 整段连续移动并实时核对反馈${finite(job.position_mm) ? ` · 最近反馈 ${mm(job.position_mm)}` : ""}`;
  }

  function eventTime(value) {
    if (!value) return "—";
    if (typeof value === "number") return new Date(value < 1e12 ? value * 1000 : value).toLocaleTimeString("zh-CN", { hour12: false });
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? String(value).slice(-8) : parsed.toLocaleTimeString("zh-CN", { hour12: false });
  }

  function eventTimestamp(value) {
    if (typeof value === "number") return value < 1e12 ? value * 1000 : value;
    if (/^\d{2}:\d{2}:\d{2}$/.test(String(value))) {
      const [hours, minutes, seconds] = value.split(":").map(Number);
      const date = new Date(); date.setHours(hours, minutes, seconds, 0); return date.getTime();
    }
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function renderEvents() {
    const events = [...state.events, ...localEvents].sort((a, b) => eventTimestamp(a.time) - eventTimestamp(b.time)).slice(-100);
    const signature = JSON.stringify(events);
    if (signature === lastEventSignature) return;
    lastEventSignature = signature;
    $("log-count").textContent = `${events.length} 条`;
    const list = $("log-list");
    list.replaceChildren();
    if (!events.length) { const empty = document.createElement("p"); empty.className = "empty-log"; empty.textContent = "连接设备后，操作与反馈将在这里显示。"; list.append(empty); return; }
    for (const item of [...events].reverse()) {
      const row = document.createElement("div"); row.className = "log-entry";
      const time = document.createElement("time"); time.textContent = eventTime(item.time);
      const level = document.createElement("span");
      const normalized = ["error", "warning", "warn", "success", "info"].includes(item.level) ? item.level : "info";
      level.className = `log-level ${normalized === "warn" ? "warning" : normalized}`;
      level.textContent = { error: "异常", warning: "提示", warn: "提示", success: "完成", info: "记录" }[normalized];
      const message = document.createElement("span"); message.textContent = String(item.message || "");
      row.append(time, level, message); list.append(row);
    }
  }

  buildCards();
  $("estop-button").addEventListener("click", () => post("emergency_stop", {}, {priority:true}));
  $("estop-clear-confirm").addEventListener("change", render);
  $("estop-clear").addEventListener("click", () => post("clear_estop", {confirmed:$("estop-clear-confirm").checked}));
  $("reset-confirm").addEventListener("change", render);
  $("reset-rpm").addEventListener("input", () => { $("reset-confirm").checked = false; render(); });
  $("reset-start").addEventListener("click", resetPositions);
  $("pump-confirm").addEventListener("change", render);
  $("pump-start").addEventListener("click", pumpStart);
  $("move-group").addEventListener("click", moveGroup);
  $("connect-button").addEventListener("click", () => post("connect"));
  $("disconnect-button").addEventListener("click", () => post("disconnect"));
  $("stop-button").addEventListener("click", async () => { const result = await post("stop", {}, { priority: true }); if (result) toast(state.job?.kind === "pump" ? "已请求停止两支注射器，请等待停机反馈。" : state.job?.kind === "reset_continuous" ? "已请求停止连续运动，请等待刹车反馈。" : "已请求停止连续运动，请等待静止确认。"); });
  $("claim-button").addEventListener("click", () => post("claim"));
  $("dismiss-error").addEventListener("click", () => { $("error-banner").hidden = true; });
  $("drive-restore-confirm").onchange = render;
  $("drive-restore").onclick = async () => { await post('restore_drives',{confirmed:$("drive-restore-confirm").checked}); $("drive-restore-confirm").checked=false; render(); };
  $("anchor-confirm").addEventListener("change", render);
  $("anchor-all").addEventListener("click", anchorAll);
  $("restore-positions").onclick = () => post("restore_positions");
  $("show-setup").addEventListener("click", openSetup);
  $("close-setup").addEventListener("click", () => { setupManuallyOpened = false; render(); });
  $("mapping-confirmed").addEventListener("change", render);
  $("long-axis-id").addEventListener("change", () => { $("mapping-confirmed").checked = false; render(); });
  $("configure-button").addEventListener("click", async () => {
    if (!canControl() || !$("mapping-confirmed").checked) return;
    const result = await post("configure", { ids: [1, 2, 3], long_id: Number($("long-axis-id").value), confirmed: true });
    if (result) {
      if (state.configured !== true) localOperation = { kind: "configure", started: Date.now() };
      editedAnchorPositions.clear();
      for (const id of [1, 2, 3]) { $(`anchor-mm-${id}`).value = ""; }
      toast("对应关系已提交，请等待确认后逐轴尺量标定。");
      render();
    }
  });
  window.addEventListener("pagehide", () => {
    if (!owns() || !state.connected) return;
    fetch("/api/stop", { method: "POST", keepalive: true, headers: { "Content-Type": "application/json", "X-Enzyflow-Token": token }, body: JSON.stringify({ client_id: clientId, request_id: uuid() }) }).catch(() => {});
  });
  render();
  poll();
  setInterval(poll, 500);
  setInterval(heartbeat, 1000);
})();
