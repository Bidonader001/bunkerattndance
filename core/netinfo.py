"""LAN IP/MAC discovery and in-memory kiosk camera frames."""

from __future__ import annotations

import base64
import re
import subprocess
import time

from core import db

_MAC_RE = re.compile(r"(?:[0-9A-Fa-f]{2}[-:]){5}[0-9A-Fa-f]{2}")
_frames: dict[int, tuple[bytes, float]] = {}
_arp_cache: dict[str, tuple[str | None, float, float]] = {}


def normalize_ip(ip: str) -> str:
    value = (ip or "").strip()
    if value.startswith("::ffff:"):
        value = value[7:]
    return value


def placeholder_mac(ip: str) -> str:
    parts = normalize_ip(ip).split(".")
    if len(parts) != 4:
        return "02:FF:00:00:00:00"
    nums = []
    for part in parts:
        try:
            nums.append(max(0, min(255, int(part))))
        except ValueError:
            nums.append(0)
    return f"02:FF:{nums[0]:02X}:{nums[1]:02X}:{nums[2]:02X}:{nums[3]:02X}"


def lookup_mac(ip: str) -> str | None:
    ip = normalize_ip(ip)
    if not ip or ip.startswith("127.") or ip == "0.0.0.0":
        return None
    cached = _arp_cache.get(ip)
    if cached and time.time() - cached[1] < cached[2]:
        return cached[0]
    try:
        subprocess.run(
            ["ping", "-n", "1", "-w", "250", ip],
            capture_output=True,
            timeout=2,
            check=False,
        )
    except Exception:
        pass
    try:
        raw = subprocess.check_output(["arp", "-a"], timeout=3, stderr=subprocess.DEVNULL)
        text = raw.decode("utf-8", "replace")
        if "Physical Address" not in text and "-" not in text:
            text = raw.decode("cp437", "replace")
    except Exception:
        return None
    for line in text.splitlines():
        bits = line.split()
        if len(bits) < 2 or bits[0] != ip:
            continue
        match = _MAC_RE.search(line)
        if not match:
            continue
        try:
            mac = db.normalize_mac(match.group(0))
        except ValueError:
            continue
        if mac in {"FF:FF:FF:FF:FF:FF", "00:00:00:00:00:00"}:
            continue
        if mac.startswith("01:00:5E"):
            continue
        _arp_cache[ip] = (mac, time.time(), 30)
        return mac
    _arp_cache[ip] = (None, time.time(), 5)
    return None


def put_preview(device_id: int, image_b64: str) -> bool:
    raw = (image_b64 or "").strip()
    if "base64," in raw:
        raw = raw.split("base64,", 1)[1]
    raw = "".join(raw.split())
    if not raw:
        return False
    try:
        data = base64.b64decode(raw)
    except Exception:
        return False
    if len(data) < 32 or len(data) > 900_000:
        return False
    _frames[int(device_id)] = (data, time.time())
    return True


def get_preview(device_id: int) -> bytes | None:
    row = _frames.get(int(device_id))
    if not row:
        return None
    data, ts = row
    if time.time() - ts > 20:
        return None
    return data


def has_preview(device_id: int) -> bool:
    return get_preview(device_id) is not None
