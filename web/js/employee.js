const video = document.getElementById("video");
const canvas = document.getElementById("canvas");
const hello = document.getElementById("hello");
const sub = document.getElementById("sub");
const meta = document.getElementById("meta");
const stateLabel = document.getElementById("state-label");
const btnOut = document.getElementById("btn-clockout");
const modal = document.getElementById("modal");
const modalText = document.getElementById("modal-text");

let busy = false;
let awaitingClear = false;
let pendingOut = null;

function kioskPayload(image_b64) {
  const body = { image_b64 };
  const mac = typeof getDeviceMac === "function" ? getDeviceMac() : "";
  if (mac) body.device_mac = mac;
  return JSON.stringify(body);
}

function rememberDevice(device) {
  if (!device) return;
  if (typeof setDeviceMac === "function" && device.mac_address && device.mac_resolved !== false) {
    if (String(device.mac_address).indexOf("02:FF:") !== 0) setDeviceMac(device.mac_address);
  }
}

function announceKiosk(cameraOn) {
  const mac = typeof getDeviceMac === "function" ? getDeviceMac() : "";
  fetch("/api/v1/devices/announce", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mac_address: mac, camera: !!cameraOn }),
  })
    .then(function (res) { return res.json(); })
    .then(function (data) {
      rememberDevice(data.device);
    })
    .catch(function () {});
}

function pushKioskPreview() {
  if (!video || !video.videoWidth) return;
  const image_b64 = frameToB64(canvas, video, 480, 0.55);
  fetch("/api/v1/devices/preview", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ image_b64: image_b64, camera: true }),
  })
    .then(function (res) { return res.json(); })
    .then(function (data) {
      rememberDevice(data.device);
    })
    .catch(function () {});
}

function setUi(label, title, detail, showOut) {
  stateLabel.textContent = label;
  hello.textContent = title;
  sub.textContent = detail;
  btnOut.classList.toggle("hidden", !showOut);
  document.body.dataset.state = String(label || "scanning")
    .toLowerCase()
    .replace(/\s+/g, "-");
}

async function tick() {
  if (busy || !video.videoWidth || !modal.classList.contains("hidden")) return;
  busy = true;
  try {
    const image_b64 = frameToB64(canvas, video);
    const result = await api("/identify", {
      method: "POST",
      body: kioskPayload(image_b64),
    });

    if (result.status === "face_off") {
      setUi("WAIT", "Face ID is off", result.message || "Turn Face ID on in the dashboard.", false);
      return;
    }
    if (result.status === "no_face") {
      awaitingClear = false;
      pendingOut = null;
      setUi("SCANNING", "Look at the camera", "You will be clocked in automatically.", false);
      meta.textContent = "";
      return;
    }
    if (result.status === "multiple_faces") {
      setUi("WAIT", "One person at a time", "Step back so only one face is visible.", false);
      return;
    }
    if (result.status !== "matched") {
      setUi("UNKNOWN", "Face not recognized", "Ask admin to register this employee.", false);
      return;
    }

    if (result.clocked_in) {
      awaitingClear = false;
      setUi(
        "CLOCKED IN",
        `Hi ${result.name}`,
        "Press CLOCK OUT when your shift is done. We confirm your face first.",
        true
      );
      const sess = result.session || {};
      meta.textContent = [
        sess.shift_name || result.shift_name ? `SHIFT  ${sess.shift_name || result.shift_name}` : "",
        sess.late ? "LATE" : "",
      ].filter(Boolean).join("  ·  ");
      btnOut.dataset.employeeId = result.employee_id;
      return;
    }

    if (result.cooldown) {
      setUi("COOLDOWN", `Hi ${result.name}`, result.cooldown.message, false);
      meta.textContent = "6 hours after clock-out";
      awaitingClear = true;
      return;
    }

    if (awaitingClear) return;

    const punched = await api("/clock-in", {
      method: "POST",
      body: kioskPayload(image_b64),
    });
    if (punched.action === "cooldown") {
      speak(punched.message);
      setUi("COOLDOWN", `Hi ${punched.name}`, punched.message, false);
      meta.textContent = "6 hours after clock-out";
      awaitingClear = true;
      return;
    }
    if (punched.action === "clocked_in" || punched.action === "already_in") {
      speak(punched.message);
      setUi("CLOCKED IN", `Hi ${punched.name}`, punched.message, true);
      meta.textContent = [
        punched.shift_name ? `SHIFT  ${punched.shift_name}` : "",
        punched.late ? "LATE" : "",
      ].filter(Boolean).join("  ·  ");
      awaitingClear = true;
      btnOut.dataset.employeeId = punched.employee_id;
    }
  } catch (e) {
    setUi("ERROR", "Cannot scan", e.message, false);
  } finally {
    busy = false;
  }
}

btnOut.addEventListener("click", async () => {
  if (!video.videoWidth) return;
  try {
    const image_b64 = frameToB64(canvas, video);
    const result = await api("/identify", {
      method: "POST",
      body: kioskPayload(image_b64),
    });
    if (result.status !== "matched") {
      setUi("NO FACE", "We need to see your face", "Look at the camera, then press CLOCK OUT again.", true);
      return;
    }
    if (!result.clocked_in) {
      setUi("NOT IN", `Hi ${result.name}`, "You are not clocked in.", false);
      return;
    }
    pendingOut = result;
    const prompt = `Hi ${result.name}, are you ready to clock out?`;
    modalText.textContent = prompt;
    speak(prompt);
    modal.classList.remove("hidden");
  } catch (e) {
    setUi("ERROR", "Cannot verify", e.message, true);
  }
});

document.getElementById("btn-cancel-out").addEventListener("click", () => {
  modal.classList.add("hidden");
  pendingOut = null;
});

document.getElementById("btn-confirm-out").addEventListener("click", async () => {
  if (!video.videoWidth) return;
  try {
    const image_b64 = frameToB64(canvas, video);
    const result = await api("/clock-out", {
      method: "POST",
      body: kioskPayload(image_b64),
    });
    modal.classList.add("hidden");
    if (result.action === "clocked_out") {
      speak(result.message);
      setUi("CLOCKED OUT", `Bye ${result.name}`, result.message, false);
      awaitingClear = true;
    } else {
      setUi("WAIT", result.message || "Could not clock out", "Look at the camera and try again.", true);
    }
  } catch (e) {
    setUi("ERROR", "Clock out failed", e.message, true);
  }
});

const btnCam = document.getElementById("btn-enable-cam");
let scanning = false;

async function bootCamera() {
  try {
    unlockSpeech();
    await startCamera(video);
    btnCam.classList.add("hidden");
    setUi("SCANNING", "Look at the camera", "You will be clocked in automatically.", false);
    announceKiosk(true);
    pushKioskPreview();
    if (!scanning) {
      scanning = true;
      setInterval(tick, 1600);
      setInterval(function () { announceKiosk(true); }, 15000);
      setInterval(pushKioskPreview, 800);
    }
  } catch (e) {
    btnCam.classList.remove("hidden");
    setUi("CAMERA", "Allow the camera", e.message, false);
  }
}

btnCam.addEventListener("click", bootCamera);
announceKiosk(false);
bootCamera();

function paintClock() {
  const el = document.getElementById("kiosk-time");
  if (!el) return;
  el.textContent = new Date().toLocaleString([], {
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}
paintClock();
setInterval(paintClock, 15000);
