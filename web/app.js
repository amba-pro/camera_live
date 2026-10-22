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
let lastOnline = false;

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
    if (online && !lastOnline) {
      // Restore an image connection after a camera disconnect.
      stream.src = "/stream.mjpg?t=" + Date.now();
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
  stream.src = "/stream.mjpg?t=" + Date.now();
  refreshStatus();
});
stream.addEventListener("error", () => {
  overlay.classList.remove("hidden");
  overlayLabel.textContent = "Видеопоток разорван. Попробуйте обновить видео.";
});
refreshStatus();
setInterval(refreshStatus, 2000);
