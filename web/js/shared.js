const API = "/api/v1";
const TOKEN_KEY = "access_pass_token";
let memoryToken = typeof window !== "undefined" && window.__BOOT_TOKEN__ ? window.__BOOT_TOKEN__ : "";

function readCookie(name) {
  try {
    const parts = document.cookie.split(";");
    for (const part of parts) {
      const [k, ...rest] = part.trim().split("=");
      if (k === name) return decodeURIComponent(rest.join("="));
    }
  } catch (err) {
    /* ignore */
  }
  return "";
}

function getAuthToken() {
  try {
    const qs = new URLSearchParams(window.location.search).get("t");
    return (
      qs ||
      localStorage.getItem(TOKEN_KEY) ||
      sessionStorage.getItem(TOKEN_KEY) ||
      memoryToken ||
      readCookie(TOKEN_KEY) ||
      ""
    );
  } catch (err) {
    return memoryToken || "";
  }
}

function setAuthToken(token) {
  memoryToken = token || "";
  try {
    if (token) {
      localStorage.setItem(TOKEN_KEY, token);
      sessionStorage.setItem(TOKEN_KEY, token);
    } else {
      localStorage.removeItem(TOKEN_KEY);
      sessionStorage.removeItem(TOKEN_KEY);
    }
  } catch (err) {
    /* Private mode: cookie + memory still keep this tab signed in */
  }
}

function apiError(data, fallback) {
  if (!data) return fallback;
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) return data.detail.map((d) => d.msg || d).join("; ");
  return fallback;
}

async function api(path, options = {}) {
  const token = getAuthToken();
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(`${API}${path}`, {
    ...options,
    credentials: "include",
    headers,
  });
  const isJson = (res.headers.get("content-type") || "").includes("json");
  const data = isJson ? await res.json().catch(() => ({})) : await res.blob();
  if (!res.ok) {
    if (isJson) throw new Error(apiError(data, res.statusText));
    throw new Error(res.statusText);
  }
  return data;
}

function frameToB64(canvas, videoEl, maxWidth = 720, quality = 0.88) {
  const ctx = canvas.getContext("2d");
  let w = videoEl.videoWidth || 640;
  let h = videoEl.videoHeight || 480;
  if (w > maxWidth) {
    h = Math.round((h * maxWidth) / w);
    w = maxWidth;
  }
  canvas.width = w;
  canvas.height = h;
  ctx.drawImage(videoEl, 0, 0, w, h);
  return canvas.toDataURL("image/jpeg", quality);
}

function stopStream(stream) {
  if (!stream) return;
  stream.getTracks().forEach((t) => t.stop());
}

function unlockSpeech() {
  if (!window.speechSynthesis) return;
  window.speechSynthesis.cancel();
  const warmup = new SpeechSynthesisUtterance(" ");
  warmup.volume = 0;
  window.speechSynthesis.speak(warmup);
}

async function startCamera(videoEl) {
  if (!window.isSecureContext) {
    throw new Error("Camera needs https. On your phone open https://YOUR-PC-IP:8000 (accept the warning).");
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    throw new Error("Use Safari on iPad, and allow the camera.");
  }
  // iOS Safari often rejects exact width/height. Keep constraints loose.
  const attempts = [
    { video: { facingMode: "user" }, audio: false },
    { video: { facingMode: { ideal: "user" } }, audio: false },
    { video: true, audio: false },
  ];
  let stream = null;
  let lastError = null;
  for (const c of attempts) {
    try {
      stream = await navigator.mediaDevices.getUserMedia(c);
      break;
    } catch (err) {
      lastError = err;
    }
  }
  if (!stream) {
    const msg = lastError && lastError.message ? lastError.message : "Could not access camera";
    throw new Error(`${msg}. Allow camera for this site in Safari settings.`);
  }
  videoEl.muted = true;
  videoEl.playsInline = true;
  videoEl.setAttribute("playsinline", "");
  videoEl.setAttribute("webkit-playsinline", "true");
  videoEl.autoplay = true;
  videoEl.srcObject = stream;
  await videoEl.play().catch(async () => {
    await new Promise((r) => setTimeout(r, 150));
    await videoEl.play();
  });
  if (!videoEl.videoWidth) {
    await new Promise((resolve) => {
      const done = () => resolve();
      videoEl.addEventListener("loadedmetadata", done, { once: true });
      setTimeout(done, 1200);
    });
  }
  return stream;
}

function getDeviceMac() {
  try {
    return localStorage.getItem("codai_device_mac") || "";
  } catch (err) {
    return "";
  }
}

function setDeviceMac(mac) {
  try {
    if (mac) localStorage.setItem("codai_device_mac", mac);
    else localStorage.removeItem("codai_device_mac");
  } catch (err) {
    /* ignore */
  }
}

function speak(text) {
  if (!window.speechSynthesis) return;
  window.speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.rate = 0.95;
  window.speechSynthesis.speak(u);
}

var NIGHT_KEY = "access_pass_night";

function isNightOn() {
  try {
    return localStorage.getItem(NIGHT_KEY) === "1";
  } catch (err) {
    return document.documentElement.classList.contains("night");
  }
}

function syncNightControls(on) {
  if (typeof on === "undefined") on = isNightOn();
  var boxes = document.querySelectorAll("[data-night-toggle]");
  var i;
  for (i = 0; i < boxes.length; i++) {
    boxes[i].checked = !!on;
  }
  var buttons = document.querySelectorAll("[data-night-button]");
  for (i = 0; i < buttons.length; i++) {
    buttons[i].setAttribute("aria-pressed", on ? "true" : "false");
    buttons[i].textContent = on ? "Day" : "Night";
  }
}

function applyNightMode(on) {
  on = !!on;
  if (on) document.documentElement.classList.add("night");
  else document.documentElement.classList.remove("night");
  try {
    localStorage.setItem(NIGHT_KEY, on ? "1" : "0");
  } catch (err) {
    /* private mode */
  }
  syncNightControls(on);
}

function bootNightMode() {
  applyNightMode(isNightOn());
  document.addEventListener("click", function (e) {
    var t = e.target;
    if (!t) return;
    var btn = t.getAttribute && t.getAttribute("data-night-button") !== null ? t : null;
    if (!btn && t.closest) btn = t.closest("[data-night-button]");
    if (!btn) return;
    applyNightMode(!document.documentElement.classList.contains("night"));
  });
  document.addEventListener("change", function (e) {
    var t = e.target;
    if (!t || !t.getAttribute) return;
    if (t.getAttribute("data-night-toggle") === null) return;
    applyNightMode(!!t.checked);
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", bootNightMode);
} else {
  bootNightMode();
}
