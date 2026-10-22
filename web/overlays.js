"use strict";
/* Shared temporary video annotations. Does not modify MJPEG or camera capture. */
(() => {
  const player = document.querySelector(".player");
  const video = document.getElementById("stream");
  const canvas = document.getElementById("draw-layer");
  const ctx = canvas.getContext("2d");
  const reactionLayer = document.getElementById("reaction-layer");
  const toggle = document.getElementById("draw-toggle");
  const options = document.getElementById("draw-options");
  const colorChoice = document.getElementById("draw-color");
  const widthChoice = document.getElementById("draw-width");
  const clearButton = document.getElementById("draw-clear");
  const status = document.getElementById("overlay-sync-status");
  if (!ctx || !player || !video) return;

  const STROKE_TTL = 120;
  const REACTION_TTL = 5;
  const POLL_MS = 550;
  const clientPrefix = Date.now().toString(36) + Math.random().toString(36).slice(2, 9);
  let sequence = 0;
  let session = "";
  let cursor = 0;
  let events = [];
  let optimistic = [];
  let knownIds = new Set();
  let reactions = new Map();
  let busy = false;
  let drawEnabled = false;
  let activeStroke = null;
  let lastFlush = 0;
  let queue = Promise.resolve();
  let serverClockOffset = 0;

  function serverNow() {
    return Date.now() / 1000 + serverClockOffset;
  }

  function newClientId() {
    sequence += 1;
    return (clientPrefix + "_" + sequence).slice(0, 48);
  }

  function viewport() {
    const w = player.clientWidth, h = player.clientHeight;
    const naturalW = video.naturalWidth || 1280;
    const naturalH = video.naturalHeight || 720;
    const factor = Math.min(w / naturalW, h / naturalH);
    const width = naturalW * factor, height = naturalH * factor;
    return {left: (w - width) / 2, top: (h - height) / 2, width, height};
  }

  function normalizedPoint(event) {
    const container = canvas.getBoundingClientRect();
    const view = viewport();
    const x = (event.clientX - container.left - view.left) / view.width;
    const y = (event.clientY - container.top - view.top) / view.height;
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
    // Do not draw over letterboxing. Pointer can travel outside while captured;
    // allow clamping only once an active stroke already began.
    if (!activeStroke && (x < 0 || x > 1 || y < 0 || y > 1)) return null;
    return [Math.max(0, Math.min(1, x)), Math.max(0, Math.min(1, y))];
  }

  function layoutCanvas() {
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, Math.round(player.clientWidth * ratio));
    const height = Math.max(1, Math.round(player.clientHeight * ratio));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width; canvas.height = height;
    }
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    redraw();
    for (const [id, node] of reactions) {
      const event = events.find((e) => e.id === id);
      if (event) positionReaction(node, event);
    }
  }

  function paintStroke(record) {
    if (!record || !Array.isArray(record.points) || !record.points.length) return;
    const view = viewport();
    const scale = Math.max(0.5, view.width / 960);
    const points = record.points;
    ctx.beginPath();
    ctx.lineWidth = (record.width || 6) * scale;
    ctx.lineCap = "round"; ctx.lineJoin = "round";
    ctx.strokeStyle = record.color || "#6be7ff";
    const start = points[0];
    const x0 = view.left + start[0] * view.width;
    const y0 = view.top + start[1] * view.height;
    ctx.moveTo(x0, y0);
    if (points.length === 1) {
      ctx.arc(x0, y0, Math.max(1.5, ctx.lineWidth / 2), 0, Math.PI * 2);
      ctx.fillStyle = ctx.strokeStyle;
      ctx.fill();
      return;
    }
    for (let i = 1; i < points.length; i++) {
      ctx.lineTo(view.left + points[i][0] * view.width,
                 view.top + points[i][1] * view.height);
    }
    ctx.stroke();
  }

  function redraw() {
    ctx.clearRect(0, 0, player.clientWidth, player.clientHeight);
    const now = serverNow();
    events = events.filter((e) =>
      (e.type === "stroke" && now - e.time < STROKE_TTL) ||
      (e.type === "reaction" && now - e.time < REACTION_TTL));
    // Cursor keeps a monotonic receipt position; old IDs need not live forever.
    if (knownIds.size > 2500) knownIds = new Set(events.map((e) => e.id));
    for (const entry of events) if (entry.type === "stroke") paintStroke(entry);
    for (const entry of optimistic) paintStroke(entry);
    if (activeStroke && activeStroke.points.length) paintStroke(activeStroke);
  }

  function positionReaction(node, event) {
    const view = viewport();
    node.style.left = (view.left + event.x * view.width) + "px";
    node.style.top = (view.top + event.y * view.height) + "px";
  }

  function showReaction(event) {
    if (reactions.has(event.id)) return;
    const age = Math.max(0, serverNow() - event.time);
    if (age >= REACTION_TTL) return;
    const node = document.createElement("span");
    node.className = "flying-reaction";
    node.textContent = event.emoji; // Safe emoji whitelist on the backend.
    node.style.animationDelay = (-age) + "s";
    positionReaction(node, event);
    reactionLayer.append(node);
    reactions.set(event.id, node);
    window.setTimeout(() => {
      node.remove();
      reactions.delete(event.id);
    }, Math.max(0, REACTION_TTL - age) * 1000);
  }

  function clearVisuals() {
    events = [];
    knownIds.clear();
    optimistic = [];
    reactions.forEach((node) => node.remove());
    reactions.clear();
    redraw();
  }

  function applyEvent(event) {
    if (!event || !Number.isSafeInteger(event.id) || knownIds.has(event.id)) return;
    if (event.type === "clear") clearVisuals();
    knownIds.add(event.id);
    events.push(event);
    optimistic = optimistic.filter((item) => item.client_id !== event.client_id);
    if (event.type === "reaction") showReaction(event);
    redraw();
  }

  function applyResponse(data) {
    if (!data || !Array.isArray(data.events)) return;
    if (Number.isFinite(data.server_time)) {
      // Laptop and phone clocks may differ. Align reaction age with server time.
      serverClockOffset = data.server_time - Date.now() / 1000;
    }
    const newSession = typeof data.session === "string" && data.session !== session;
    if (newSession || data.reset) clearVisuals();
    if (newSession) session = data.session;
    for (const event of data.events) applyEvent(event);
    cursor = Math.max(0, Number(data.latest_id) || 0);
    redraw();
  }

  async function poll() {
    if (busy) return;
    busy = true;
    try {
      const response = await fetch("/api/overlays?after=" + cursor, {cache: "no-store"});
      if (!response.ok) throw new Error("HTTP " + response.status);
      const data = await response.json();
      // A response can race with a successful local POST; don't rewind cursor.
      if (data.session === session && data.latest_id < cursor) return;
      applyResponse(data);
      status.textContent = "Реакции и рисунки общие";
      status.classList.remove("sync-error");
    } catch (_) {
      status.textContent = "Нет связи с реакциями";
      status.classList.add("sync-error");
    } finally {
      busy = false;
    }
  }

  function submit(payload, optimisticStroke = null) {
    const client_id = newClientId();
    const body = { ...payload, client_id };
    if (optimisticStroke) {
      optimisticStroke.client_id = client_id;
      optimistic.push(optimisticStroke);
    }
    queue = queue.catch(() => {}).then(async () => {
      try {
        const response = await fetch("/api/overlays", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(body),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Не удалось отправить.");
        applyEvent(data.event);
        cursor = Math.max(cursor, Number(data.event.id) || 0);
        status.textContent = "Реакции и рисунки общие";
        status.classList.remove("sync-error");
      } catch (error) {
        status.textContent = error.message || "Ошибка отправки";
        status.classList.add("sync-error");
      } finally {
        optimistic = optimistic.filter((record) => record.client_id !== client_id);
        redraw();
      }
    });
    return queue;
  }

  function flushStroke(force = false) {
    if (!activeStroke) return;
    const points = activeStroke.points;
    if (points.length < 2 && !force) return;
    if (points.length < 2 && force && activeStroke.hasChunks) return;
    const upload = points.slice(0, 32).map((point) => point.slice());
    if (!upload.length) return;
    // Retain the terminal point in the next chunk for contiguous lines.
    activeStroke.points = [upload[upload.length - 1]];
    activeStroke.hasChunks = true;
    lastFlush = performance.now();
    submit(
      {type: "stroke", points: upload, color: activeStroke.color, width: activeStroke.width},
      {points: upload, color: activeStroke.color, width: activeStroke.width}
    );
  }

  function pointerDown(event) {
    if (!drawEnabled || (event.pointerType === "mouse" && event.button !== 0)) return;
    const point = normalizedPoint(event);
    if (!point) return;
    event.preventDefault();
    canvas.setPointerCapture(event.pointerId);
    activeStroke = {
      pointerId: event.pointerId, points: [point],
      color: colorChoice.value, width: Number(widthChoice.value), hasChunks: false,
    };
    lastFlush = performance.now();
    redraw();
  }

  function pointerMove(event) {
    if (!activeStroke || activeStroke.pointerId !== event.pointerId) return;
    const point = normalizedPoint(event);
    if (!point) return;
    event.preventDefault();
    const points = activeStroke.points;
    const last = points[points.length - 1];
    if (Math.abs(point[0] - last[0]) + Math.abs(point[1] - last[1]) < .0005) return;
    points.push(point);
    if (points.length >= 31 || performance.now() - lastFlush > 115) flushStroke();
    redraw();
  }

  function pointerEnd(event) {
    if (!activeStroke || activeStroke.pointerId !== event.pointerId) return;
    const point = normalizedPoint(event);
    if (point) {
      const last = activeStroke.points[activeStroke.points.length - 1];
      if (Math.abs(last[0] - point[0]) + Math.abs(last[1] - point[1]) >= .0005) {
        activeStroke.points.push(point);
      }
    }
    flushStroke(true);
    activeStroke = null;
    redraw();
    if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
  }

  canvas.addEventListener("pointerdown", pointerDown);
  canvas.addEventListener("pointermove", pointerMove);
  canvas.addEventListener("pointerup", pointerEnd);
  canvas.addEventListener("pointercancel", pointerEnd);
  canvas.addEventListener("lostpointercapture", (event) => {
    if (activeStroke && activeStroke.pointerId === event.pointerId) {
      flushStroke(true);
      activeStroke = null;
      redraw();
    }
  });

  toggle.addEventListener("click", () => {
    drawEnabled = !drawEnabled;
    toggle.setAttribute("aria-pressed", String(drawEnabled));
    toggle.textContent = drawEnabled ? "✓ Закончить рисование" : "✎ Рисовать";
    toggle.classList.toggle("active", drawEnabled);
    canvas.classList.toggle("is-drawing", drawEnabled);
    options.hidden = !drawEnabled;
  });

  clearButton.addEventListener("click", () => {
    if (window.confirm("Удалить все рисунки и реакции у ВСЕХ зрителей?")) {
      submit({type: "clear"});
    }
  });

  document.querySelectorAll(".reaction-button").forEach((button) => {
    button.addEventListener("click", () => {
      const emoji = button.dataset.emoji;
      const x = .18 + Math.random() * .64;
      const y = .48 + Math.random() * .31;
      submit({type: "reaction", emoji, x, y});
      button.classList.add("reacted");
      setTimeout(() => button.classList.remove("reacted"), 320);
    });
  });

  if ("ResizeObserver" in window) {
    new ResizeObserver(layoutCanvas).observe(player);
  } else {
    window.addEventListener("resize", layoutCanvas);
  }
  video.addEventListener("load", layoutCanvas);
  layoutCanvas();
  poll();
  window.setInterval(poll, POLL_MS);
  window.setInterval(redraw, 1600);
})();
