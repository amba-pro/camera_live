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


// Lightweight, multi-user LAN chat. The camera and iOS JPEG fallback above
// intentionally remain unchanged.
const chatForm = document.getElementById("chat-form");
const chatMessages = document.getElementById("chat-messages");
const chatEmpty = document.getElementById("chat-empty");
const chatNickname = document.getElementById("chat-nickname");
const chatText = document.getElementById("chat-text");
const chatStatus = document.getElementById("chat-status");
const chatError = document.getElementById("chat-error");
const chatCount = document.getElementById("chat-count");
const chatSend = document.getElementById("chat-send");
let chatLastId = 0;
let chatRefreshing = false;

try {
  chatNickname.value = localStorage.getItem("koprulu-chat-nickname") || "";
} catch (_) {
  // Storage can be disabled, e.g. private browsing.
}

chatText.addEventListener("input", () => {
  chatCount.textContent = chatText.value.length + " / 300";
});
chatText.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    chatForm.requestSubmit();
  }
});

function renderChatMessage(record) {
  if (!record || !Number.isSafeInteger(record.id) || typeof record.nickname !== "string" ||
      typeof record.text !== "string") return;
  if (record.id <= chatLastId) return;
  const isAtBottom = chatMessages.scrollHeight - chatMessages.scrollTop -
    chatMessages.clientHeight < 90;
  const item = document.createElement("article");
  item.className = "chat-message";
  const head = document.createElement("div");
  head.className = "chat-message-head";
  const nick = document.createElement("span");
  nick.className = "chat-message-name";
  nick.textContent = record.nickname; // Never use innerHTML with user input.
  const time = document.createElement("time");
  time.className = "chat-message-time";
  const date = new Date(record.timestamp);
  time.textContent = Number.isNaN(date.getTime()) ? "" :
    date.toLocaleTimeString("ru-RU", {hour: "2-digit", minute: "2-digit"});
  const message = document.createElement("p");
  message.className = "chat-message-body";
  message.textContent = record.text;
  head.append(nick, time);
  item.append(head, message);
  chatMessages.appendChild(item);
  chatLastId = record.id;
  chatEmpty.hidden = true;
  while (chatMessages.querySelectorAll(".chat-message").length > 200) {
    chatMessages.querySelector(".chat-message").remove();
  }
  if (isAtBottom) chatMessages.scrollTop = chatMessages.scrollHeight;
}

async function refreshChat() {
  if (chatRefreshing) return;
  chatRefreshing = true;
  try {
    const response = await fetch("/api/chat?after=" + chatLastId, {cache: "no-store"});
    if (!response.ok) throw new Error("HTTP " + response.status);
    const payload = await response.json();
    if (!Array.isArray(payload.messages)) throw new Error("Некорректный ответ");
    if (payload.latest_id < chatLastId) {
      // Python server restarted; history and IDs start from the beginning.
      chatLastId = 0;
      chatMessages.querySelectorAll(".chat-message").forEach((node) => node.remove());
      chatEmpty.hidden = false;
      return;
    }
    for (const record of payload.messages) renderChatMessage(record);
    chatStatus.textContent = "Канал активен · " +
      chatMessages.querySelectorAll(".chat-message").length + " сообщений в истории";
  } catch (_) {
    chatStatus.textContent = "Нет связи с чатом · повторное подключение…";
  } finally {
    chatRefreshing = false;
  }
}

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (chatSend.disabled) return;
  chatError.hidden = true;
  const nickname = chatNickname.value.trim();
  const text = chatText.value.trim();
  if (!nickname || !text) {
    chatError.textContent = "Заполните позывной и сообщение.";
    chatError.hidden = false;
    return;
  }
  chatSend.disabled = true;
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({nickname, text})
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Не удалось отправить сообщение");
    chatText.value = "";
    chatCount.textContent = "0 / 300";
    try { localStorage.setItem("koprulu-chat-nickname", nickname); } catch (_) {}
    await refreshChat();
    chatText.focus();
  } catch (error) {
    chatError.textContent = error.message;
    chatError.hidden = false;
  } finally {
    chatSend.disabled = false;
  }
});
refreshChat();
setInterval(refreshChat, 1700);
