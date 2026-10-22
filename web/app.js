"use strict";
const state = document.getElementById("state");
const stateLabel = document.getElementById("state-label");
const overlay = document.getElementById("overlay");
const overlayLabel = document.getElementById("overlay-label");
const resolution = document.getElementById("resolution");
const fps = document.getElementById("fps");
const source = document.getElementById("source");
const message = document.getElementById("message");
const snapshot = document.getElementById("snapshot");
const stream = document.getElementById("stream");
const notice = document.getElementById("notice");
const lan = document.getElementById("lan");
let lastOnline = false;
let sawOffline = false;
let snapshotMode = false;
let snapshotTimer = 0;
let lastBlobUrl = "";

function stopSnapshotMode() {
  snapshotMode = false;
  if (snapshotTimer) clearInterval(snapshotTimer);
  snapshotTimer = 0;
}

function startSnapshotMode() {
  // iPhone Safari does not render MJPEG in an image. Pull JPEG frames instead.
  if (snapshotMode) return;
  snapshotMode = true;
  const tick = async () => {
    if (!snapshotMode) return;
    try {
      const response = await fetch("/snapshot.jpg?t=" + Date.now(), {cache: "no-store"});
      if (!response.ok) return;
      const url = URL.createObjectURL(await response.blob());
      stream.src = url;
      if (lastBlobUrl) URL.revokeObjectURL(lastBlobUrl);
      lastBlobUrl = url;
      overlay.classList.add("hidden");
    } catch (error) {
      overlay.classList.remove("hidden");
      overlayLabel.textContent = "Нет кадра с камеры";
    }
  };
  tick();
  snapshotTimer = setInterval(tick, 200);
}

let ignoreStreamError = false;

function useMjpegStream() {
  stopSnapshotMode();
  ignoreStreamError = true;
  stream.src = "/stream.mjpg?t=" + Date.now();
  setTimeout(() => {
    ignoreStreamError = false;
  }, 700);
  setTimeout(() => {
    if (!snapshotMode && stream.naturalWidth === 0) startSnapshotMode();
  }, 2500);
}

async function refreshStatus() {
  try {
    const response = await fetch("/api/status", {cache: "no-store"});
    if (!response.ok) throw new Error("HTTP " + response.status);
    const data = await response.json();
    const online = Boolean(data.online);
    state.classList.toggle("online", online);
    state.classList.toggle("offline", !online);
    stateLabel.textContent = online ? "Камера подключена" : "Камера недоступна";
    overlay.classList.toggle("hidden", online);
    overlayLabel.textContent = online ? "" : "Ожидание кадра камеры…";
    resolution.textContent = online ? data.width + " × " + data.height : "— × —";
    fps.textContent = online ? data.fps + " FPS" : "— FPS";
    const cameraLabel = data.camera_name || ("Camera " + data.camera_index);
    source.textContent = cameraLabel + " · " + data.camera_index + (online && data.backend ? " · " + data.backend : "");
    snapshot.setAttribute("aria-disabled", online ? "false" : "true");
    const note = online ? (data.notice || "") : "";
    notice.textContent = note;
    notice.classList.toggle("hidden", !note);
    message.textContent = online
      ? (note || "Трансляция активна. Изображение доступно на этом сервере.")
      : (data.error || "Повторное подключение камеры…");
    const urls = Array.isArray(data.lan_urls) ? data.lan_urls : [];
    lan.hidden = urls.length === 0;
    const network = data.lan_network ? (" в Wi-Fi «" + data.lan_network + "»") : " в той же сети";
    lan.textContent = urls.length ? ("С телефона" + network + " откройте: " + urls.join(" или ")) : "";
    if (!online) sawOffline = true;
    if (online && !lastOnline && sawOffline && !snapshotMode) {
      useMjpegStream();
    }
    lastOnline = online;
  } catch (error) {
    lastOnline = false;
    state.classList.remove("online");
    stateLabel.textContent = "Сервер недоступен";
    overlay.classList.remove("hidden");
    overlayLabel.textContent = "Нет связи с сервером";
    notice.classList.add("hidden");
    snapshot.setAttribute("aria-disabled", "true");
    message.textContent = "Ошибка соединения: " + error.message;
  }
}

document.getElementById("reconnect").addEventListener("click", () => {
  useMjpegStream();
  refreshStatus();
});
stream.addEventListener("error", () => {
  if (ignoreStreamError || snapshotMode) return;
  startSnapshotMode();
});
refreshStatus();
setInterval(refreshStatus, 2000);
setTimeout(() => {
  if (!snapshotMode && stream.naturalWidth === 0) startSnapshotMode();
}, 2500);
