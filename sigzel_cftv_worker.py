from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

import cv2
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

API_URL = os.getenv("SIGZEL_BASE_URL", "https://ynjazjyjxdpubtethqvu.supabase.co/functions/v1/cftv-worker")
WORKER_KEY = os.getenv("SIGZEL_WORKER_KEY") or os.getenv("CFTV_WORKER_API_KEY") or ""
WORKER_ID = os.getenv("WORKER_ID", "").strip()
WORKER_NAME = os.getenv("WORKER_NAME", platform.node() or "sigzel-cftv-worker").strip()
WORKER_TOKEN = os.getenv("WORKER_TOKEN", "").strip()
CAMERA_SOURCE = os.getenv("CAMERA_SOURCE", "hybrid").lower()
WORKER_CONFIG_URL = os.getenv("WORKER_CONFIG_URL", "").strip()
WORK_LOCATION_IDS = [x.strip() for x in os.getenv("WORK_LOCATION_IDS", "").split(",") if x.strip()]
ALLOW_UNASSIGNED_CAMERAS = os.getenv("ALLOW_UNASSIGNED_CAMERAS", "false").lower() in ("1", "true", "yes", "sim")
CAMERAS_CACHE_FILE = os.getenv("CAMERAS_CACHE_FILE", "cameras_cache.json")
CAMERAS_FILE = os.getenv("CAMERAS_FILE", "cameras.json")
JSON_BACKUP_BEFORE_SYNC = os.getenv("JSON_BACKUP_BEFORE_SYNC", "true").lower() in ("1", "true", "yes", "sim")
SNAPSHOT_DIR = Path(os.getenv("SNAPSHOT_DIR", "snapshots"))
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_CAMERA_USER = os.getenv("DEFAULT_CAMERA_USER", "admin")
DEFAULT_CAMERA_PASSWORD = os.getenv("DEFAULT_CAMERA_PASSWORD", "")
DEFAULT_RTSP_TEMPLATE = os.getenv("DEFAULT_RTSP_TEMPLATE", "rtsp://{user}:{password}@{ip}:554/cam/realmonitor?channel=1&subtype=1")
SEND_RTSP_CREDENTIALS = os.getenv("SEND_RTSP_CREDENTIALS", "false").lower() in ("1", "true", "yes", "sim")
ICMP_INTERVAL_SECONDS = int(os.getenv("ICMP_INTERVAL_SECONDS", "180"))
RTSP_INTERVAL_SECONDS = int(os.getenv("RTSP_INTERVAL_SECONDS", "300"))
CONFIG_REFRESH_SECONDS = int(os.getenv("CONFIG_REFRESH_SECONDS", "300"))
MAIN_LOOP_SLEEP_SECONDS = int(os.getenv("MAIN_LOOP_SLEEP_SECONDS", "5"))
MAX_ICMP_WORKERS = int(os.getenv("MAX_ICMP_WORKERS", "4"))
MAX_RTSP_WORKERS = int(os.getenv("MAX_RTSP_WORKERS", "1"))
PING_COUNT = int(os.getenv("PING_COUNT", "3"))
PING_TIMEOUT_SECONDS = int(os.getenv("PING_TIMEOUT_SECONDS", "2"))
RTSP_OPEN_TIMEOUT_MS = int(os.getenv("RTSP_OPEN_TIMEOUT_MS", "4000"))
RTSP_READ_ATTEMPTS = int(os.getenv("RTSP_READ_ATTEMPTS", "8"))
RTSP_FRAME_WIDTH = int(os.getenv("RTSP_FRAME_WIDTH", "640"))
JPEG_QUALITY = int(os.getenv("JPEG_QUALITY", "65"))
SKIP_RTSP_IF_ICMP_UNREACHABLE = os.getenv("SKIP_RTSP_IF_ICMP_UNREACHABLE", "true").lower() in ("1", "true", "yes", "sim")
SNAPSHOT_MODE = os.getenv("SNAPSHOT_MODE", "keep_latest").lower()
SNAPSHOT_TTL_MINUTES = int(os.getenv("SNAPSHOT_TTL_MINUTES", "30"))
OFFLINE_AFTER_FAILURES = int(os.getenv("OFFLINE_AFTER_FAILURES", "3"))
ONLINE_AFTER_SUCCESSES = int(os.getenv("ONLINE_AFTER_SUCCESSES", "1"))
HTTP_TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "15"))
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "3"))
HTTP_RETRY_BASE_SECONDS = float(os.getenv("HTTP_RETRY_BASE_SECONDS", "2"))
SIGZEL_DRY_RUN = os.getenv("SIGZEL_DRY_RUN", "true").lower() in ("1", "true", "yes", "sim")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("sigzel-cftv-worker")

@dataclass
class CameraConfig:
    name: str
    ip: str
    rtsp_url: str = ""
    location: Optional[str] = None
    sigzel_id: Optional[str] = None
    work_location_id: Optional[str] = None
    worker_id: Optional[str] = None
    enabled: bool = True
    notes: Optional[str] = None

@dataclass
class CameraState:
    failure_count: int = 0
    success_count: int = 0
    last_status: str = "pendente"
    last_response_at: Optional[str] = None
    last_icmp_check_epoch: float = 0.0
    last_rtsp_check_epoch: float = 0.0
    last_error: Optional[str] = None

@dataclass
class IcmpResult:
    reachable: bool
    packets_sent: int
    packets_received: int
    packets_lost: int
    packet_loss_pct: Optional[float]
    latency_min_ms: Optional[float]
    latency_avg_ms: Optional[float]
    latency_max_ms: Optional[float]
    jitter_ms: Optional[float]
    ttl_detected: Optional[int]
    ttl_values: List[int]
    classification: str
    raw: str = ""
    error: Optional[str] = None

@dataclass
class RtspResult:
    connection_ok: bool
    frame_captured: bool
    attempt_duration_ms: int
    snapshot_path: Optional[str] = None
    snapshot_size_bytes: Optional[int] = None
    snapshot_status: str = "falha"
    rtsp_error: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None

def utc_now_iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def round_or_none(value: Optional[float], digits: int = 1) -> Optional[float]:
    return None if value is None else round(float(value), digits)

def safe_filename(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in name)[:120]

def redact_url(url: str) -> str:
    if not url:
        return url
    try:
        parts = urlsplit(url)
        if "@" not in parts.netloc:
            return url
        userinfo, host = parts.netloc.rsplit("@", 1)
        user = userinfo.split(":", 1)[0] if ":" in userinfo else "***"
        return urlunsplit((parts.scheme, f"{user}:***@{host}", parts.path, parts.query, parts.fragment))
    except Exception:
        return "rtsp://***"

def build_default_rtsp_url(ip: str) -> str:
    return DEFAULT_RTSP_TEMPLATE.format(user=quote(DEFAULT_CAMERA_USER or "", safe=""), password=quote(DEFAULT_CAMERA_PASSWORD or "", safe=""), ip=ip)

def rtsp_url_for_payload(camera: CameraConfig) -> Optional[str]:
    if not camera.rtsp_url:
        return None
    return camera.rtsp_url if SEND_RTSP_CREDENTIALS else redact_url(camera.rtsp_url)

def get_headers() -> Dict[str, str]:
    headers = {"x-worker-key": WORKER_KEY, "Content-Type": "application/json"}
    if WORKER_ID:
        headers["x-worker-id"] = WORKER_ID
    if WORKER_TOKEN:
        headers["x-worker-token"] = WORKER_TOKEN
    return headers

def normalize_camera_item(item: Dict[str, Any], source: str = "system") -> Optional[CameraConfig]:
    name = item.get("name") or item.get("camera_name")
    ip = item.get("ip_address") or item.get("ip")
    if not name or not ip:
        return None
    rtsp_url = item.get("rtsp_url") or item.get("stream_url") or build_default_rtsp_url(str(ip))
    return CameraConfig(
        name=str(name), ip=str(ip), rtsp_url=str(rtsp_url), location=item.get("location"),
        sigzel_id=item.get("id") or item.get("camera_id") or item.get("sigzel_id"),
        work_location_id=item.get("work_location_id"), worker_id=item.get("worker_id") or item.get("assigned_worker_id"),
        enabled=bool(item.get("enabled", True)), notes=item.get("notes"),
    )

def camera_to_json_item(camera: CameraConfig) -> Dict[str, Any]:
    item = {"name": camera.name, "ip": camera.ip, "rtsp_url": camera.rtsp_url, "location": camera.location or "", "enabled": camera.enabled}
    if camera.sigzel_id:
        item["sigzel_id"] = camera.sigzel_id
    if camera.work_location_id:
        item["work_location_id"] = camera.work_location_id
    if camera.worker_id:
        item["worker_id"] = camera.worker_id
    if camera.notes:
        item["notes"] = camera.notes
    return item

def save_cameras_cache(cameras: List[CameraConfig], path: str = CAMERAS_CACHE_FILE) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if JSON_BACKUP_BEFORE_SYNC and file_path.exists():
        try:
            shutil.copyfile(file_path, file_path.with_suffix(file_path.suffix + ".bak"))
        except Exception as exc:
            logger.warning("Cache backup failed: %s", exc)
    file_path.write_text(json.dumps([camera_to_json_item(c) for c in cameras], indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Camera cache updated: %s | total=%s", file_path, len(cameras))

def load_cameras_json(path: str) -> List[CameraConfig]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    data = json.loads(file_path.read_text(encoding="utf-8"))
    cameras = []
    for item in data:
        if isinstance(item, dict):
            cam = normalize_camera_item(item, source="json")
            if cam and cam.enabled:
                cameras.append(cam)
    return cameras

def camera_is_assigned_to_this_worker(camera: CameraConfig) -> bool:
    if not camera.enabled:
        return False
    if camera.worker_id:
        return bool(WORKER_ID and camera.worker_id == WORKER_ID)
    if camera.work_location_id:
        return not WORK_LOCATION_IDS or camera.work_location_id in WORK_LOCATION_IDS
    return ALLOW_UNASSIGNED_CAMERAS

def fetch_cameras_from_worker_config() -> List[CameraConfig]:
    if not WORKER_CONFIG_URL:
        return []
    if not WORKER_KEY and not WORKER_TOKEN:
        logger.error("WORKER_CONFIG_URL is set, but no worker key/token was configured.")
        return []
    try:
        params = {"worker_id": WORKER_ID} if WORKER_ID else {}
        response = requests.get(WORKER_CONFIG_URL, headers=get_headers(), params=params, timeout=HTTP_TIMEOUT_SECONDS)
        if response.status_code >= 400:
            logger.error("Worker config failed: HTTP %s | %s", response.status_code, response.text[:1000])
            return []
        data = response.json()
        raw_cameras = data.get("cameras", []) if isinstance(data, dict) else data if isinstance(data, list) else []
        cameras = [cam for item in raw_cameras if isinstance(item, dict) for cam in [normalize_camera_item(item, "worker_config")] if cam and cam.enabled]
        logger.info("Cameras loaded from worker config: %s", len(cameras))
        if cameras:
            save_cameras_cache(cameras)
        return cameras
    except Exception as exc:
        logger.error("Worker config error: %s", exc)
        return []

def fetch_cameras_from_sigzel_get() -> List[CameraConfig]:
    if SIGZEL_DRY_RUN:
        logger.warning("SIGZEL_DRY_RUN=true: GET from SIGZEL skipped.")
        return []
    if not WORKER_KEY:
        logger.error("No SIGZEL worker key for GET.")
        return []
    try:
        response = requests.get(API_URL, headers=get_headers(), timeout=HTTP_TIMEOUT_SECONDS)
        if response.status_code >= 400:
            logger.error("SIGZEL GET failed: HTTP %s | %s", response.status_code, response.text[:1000])
            return []
        data = response.json()
        if not isinstance(data, list):
            logger.error("SIGZEL GET returned unexpected format.")
            return []
        all_cameras = [cam for item in data if isinstance(item, dict) for cam in [normalize_camera_item(item, "sigzel_get")] if cam]
        assigned = [cam for cam in all_cameras if camera_is_assigned_to_this_worker(cam)]
        logger.info("SIGZEL cameras: total=%s | assigned=%s", len(all_cameras), len(assigned))
        if assigned:
            save_cameras_cache(assigned)
        return assigned
    except Exception as exc:
        logger.error("SIGZEL GET error: %s", exc)
        return []

def load_cameras_from_source() -> List[CameraConfig]:
    if CAMERA_SOURCE == "json":
        cams = load_cameras_json(CAMERAS_FILE) or load_cameras_json(CAMERAS_CACHE_FILE)
        logger.info("Cameras loaded from JSON/cache: %s", len(cams))
        return cams
    if CAMERA_SOURCE == "system_config":
        return fetch_cameras_from_worker_config() or load_cameras_json(CAMERAS_CACHE_FILE)
    if CAMERA_SOURCE == "sigzel_cameras":
        return fetch_cameras_from_sigzel_get() or load_cameras_json(CAMERAS_CACHE_FILE)
    if CAMERA_SOURCE != "hybrid":
        raise ValueError("Invalid CAMERA_SOURCE. Use hybrid, system_config, sigzel_cameras or json.")
    return fetch_cameras_from_worker_config() or fetch_cameras_from_sigzel_get() or load_cameras_json(CAMERAS_CACHE_FILE) or load_cameras_json(CAMERAS_FILE)

def build_ping_command(ip: str) -> List[str]:
    return ["ping", "-n", str(PING_COUNT), "-w", str(PING_TIMEOUT_SECONDS * 1000), ip] if "windows" in platform.system().lower() else ["ping", "-c", str(PING_COUNT), "-W", str(PING_TIMEOUT_SECONDS), ip]

def extract_ttls(output: str) -> List[int]:
    values = []
    for match in re.findall(r"ttl[= ](\d+)", output, flags=re.IGNORECASE):
        try:
            values.append(int(match))
        except ValueError:
            pass
    return values

def classify_icmp(reachable: bool, loss: Optional[float], avg: Optional[float], max_latency: Optional[float], jitter: Optional[float]) -> str:
    if not reachable:
        return "OFFLINE / INACESSIVEL VIA ICMP"
    if loss is not None and loss >= 100:
        return "OFFLINE / 100% DE PERDA"
    if loss is not None and loss > 5:
        return "CRITICO - perda de pacotes alta"
    if avg is not None and avg > 100:
        return "CRITICO - latencia media muito alta"
    if jitter is not None and jitter > 30:
        return "INSTAVEL - jitter alto"
    if max_latency is not None and max_latency > 100:
        return "INSTAVEL - pico de latencia alto"
    if loss is not None and loss > 0:
        return "ATENCAO - houve perda de pacotes"
    if avg is not None and avg > 50:
        return "ATENCAO - latencia elevada"
    return "OK - rede saudavel"

def parse_icmp_result(output: str, return_code: int) -> IcmpResult:
    text = output.replace(",", ".")
    sent = received = lost = None
    packet_loss_pct = None
    latency_min_ms = latency_avg_ms = latency_max_ms = jitter_ms = None
    ttl_values = extract_ttls(text)
    ttl_detected = ttl_values[0] if ttl_values else None
    packet_match = re.search(r"Enviados\s*=\s*(\d+).*?Recebidos\s*=\s*(\d+).*?Perdidos\s*=\s*(\d+).*?\((\d+(?:\.\d+)?)%.*?\)", text, flags=re.IGNORECASE | re.DOTALL)
    if not packet_match:
        packet_match = re.search(r"Sent\s*=\s*(\d+).*?Received\s*=\s*(\d+).*?Lost\s*=\s*(\d+).*?\((\d+(?:\.\d+)?)%.*?\)", text, flags=re.IGNORECASE | re.DOTALL)
    if packet_match:
        sent, received, lost = int(packet_match.group(1)), int(packet_match.group(2)), int(packet_match.group(3))
        packet_loss_pct = float(packet_match.group(4))
    if sent is None:
        linux_packet = re.search(r"(\d+)\s+packets transmitted,\s+(\d+)\s+(?:packets\s+)?received.*?(\d+(?:\.\d+)?)%\s+packet loss", text, flags=re.IGNORECASE | re.DOTALL)
        if linux_packet:
            sent, received = int(linux_packet.group(1)), int(linux_packet.group(2))
            packet_loss_pct = float(linux_packet.group(3))
            lost = sent - received
    latency_match = re.search(r"M.nimo\s*=\s*(\d+(?:\.\d+)?)ms.*?M.ximo\s*=\s*(\d+(?:\.\d+)?)ms.*?M.dia\s*=\s*(\d+(?:\.\d+)?)ms", text, flags=re.IGNORECASE | re.DOTALL)
    if not latency_match:
        latency_match = re.search(r"Minimum\s*=\s*(\d+(?:\.\d+)?)ms.*?Maximum\s*=\s*(\d+(?:\.\d+)?)ms.*?Average\s*=\s*(\d+(?:\.\d+)?)ms", text, flags=re.IGNORECASE | re.DOTALL)
    if latency_match:
        latency_min_ms, latency_max_ms, latency_avg_ms = float(latency_match.group(1)), float(latency_match.group(2)), float(latency_match.group(3))
    if latency_min_ms is None:
        linux_latency = re.search(r"min/avg/max/(?:mdev|stddev)\s*=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)", text, flags=re.IGNORECASE)
        if linux_latency:
            latency_min_ms, latency_avg_ms, latency_max_ms, jitter_ms = float(linux_latency.group(1)), float(linux_latency.group(2)), float(linux_latency.group(3)), float(linux_latency.group(4))
    sent = PING_COUNT if sent is None else sent
    received = 0 if received is None and return_code != 0 else 0 if received is None else received
    lost = max(sent - received, 0) if lost is None else lost
    packet_loss_pct = round((lost / sent) * 100, 1) if packet_loss_pct is None and sent > 0 else packet_loss_pct
    jitter_ms = latency_max_ms - latency_min_ms if jitter_ms is None and latency_min_ms is not None and latency_max_ms is not None else jitter_ms
    reachable = return_code == 0 and received > 0
    return IcmpResult(reachable, sent, received, lost, packet_loss_pct, latency_min_ms, latency_avg_ms, latency_max_ms, jitter_ms, ttl_detected, ttl_values, classify_icmp(reachable, packet_loss_pct, latency_avg_ms, latency_max_ms, jitter_ms), output[-1500:])

def check_icmp(ip: str) -> IcmpResult:
    try:
        completed = subprocess.run(build_ping_command(ip), capture_output=True, text=True, timeout=(PING_COUNT * PING_TIMEOUT_SECONDS) + 5, check=False)
        return parse_icmp_result((completed.stdout or "") + "\n" + (completed.stderr or ""), completed.returncode)
    except subprocess.TimeoutExpired as exc:
        return IcmpResult(False, PING_COUNT, 0, PING_COUNT, 100.0, None, None, None, None, None, [], "OFFLINE / TIMEOUT ICMP", error=str(exc))
    except Exception as exc:
        return IcmpResult(False, PING_COUNT, 0, PING_COUNT, None, None, None, None, None, None, [], "ERRO ICMP", error=str(exc))

def cleanup_old_snapshots() -> None:
    if SNAPSHOT_MODE != "ttl":
        return
    cutoff = time.time() - (SNAPSHOT_TTL_MINUTES * 60)
    for file_path in SNAPSHOT_DIR.glob("*.jpg"):
        try:
            if file_path.stat().st_mtime < cutoff:
                file_path.unlink(missing_ok=True)
        except Exception:
            pass

def capture_rtsp_frame(camera: CameraConfig) -> RtspResult:
    start = time.time()
    cap = None
    if not camera.rtsp_url:
        return RtspResult(False, False, 0, snapshot_status="falha", rtsp_error="rtsp_url_vazia")
    try:
        os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", f"rtsp_transport;tcp|stimeout;{RTSP_OPEN_TIMEOUT_MS * 1000}")
        cap = cv2.VideoCapture(camera.rtsp_url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, RTSP_OPEN_TIMEOUT_MS)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, RTSP_OPEN_TIMEOUT_MS)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not cap.isOpened():
            return RtspResult(False, False, int((time.time() - start) * 1000), snapshot_status="falha", rtsp_error="rtsp_open_failed")
        frame = None
        for _ in range(RTSP_READ_ATTEMPTS):
            ok, candidate = cap.read()
            if ok and candidate is not None:
                frame = candidate
                break
        if frame is None:
            return RtspResult(True, False, int((time.time() - start) * 1000), snapshot_status="falha", rtsp_error="rtsp_frame_read_failed")
        height, width = frame.shape[:2]
        if width > RTSP_FRAME_WIDTH:
            ratio = RTSP_FRAME_WIDTH / float(width)
            frame = cv2.resize(frame, (RTSP_FRAME_WIDTH, int(height * ratio)))
            height, width = frame.shape[:2]
        if SNAPSHOT_MODE == "none":
            return RtspResult(True, True, int((time.time() - start) * 1000), snapshot_status="ok", width=width, height=height)
        filename = f"{safe_filename(camera.name)}_latest.jpg" if SNAPSHOT_MODE == "keep_latest" else f"{safe_filename(camera.name)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        path = SNAPSHOT_DIR / filename
        ok = cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        if not ok:
            return RtspResult(True, True, int((time.time() - start) * 1000), snapshot_status="falha", rtsp_error="snapshot_write_failed", width=width, height=height)
        cleanup_old_snapshots()
        return RtspResult(True, True, int((time.time() - start) * 1000), path.as_posix(), path.stat().st_size, "ok", None, width, height)
    except Exception as exc:
        return RtspResult(False, False, int((time.time() - start) * 1000), snapshot_status="falha", rtsp_error=f"rtsp_error: {exc}")
    finally:
        if cap is not None:
            cap.release()

def build_notes(camera: CameraConfig, icmp: Optional[IcmpResult], rtsp: Optional[RtspResult]) -> str:
    parts = []
    if camera.notes:
        parts.append(camera.notes)
    if WORKER_ID:
        parts.append(f"worker_id={WORKER_ID}")
    if WORKER_NAME:
        parts.append(f"worker_name={WORKER_NAME}")
    if camera.work_location_id:
        parts.append(f"work_location_id={camera.work_location_id}")
    if icmp:
        parts.append(f"classificacao_icmp={icmp.classification}")
        if icmp.ttl_detected is not None:
            parts.append(f"ttl={icmp.ttl_detected}")
    if rtsp and rtsp.width and rtsp.height:
        parts.append(f"snapshot_res={rtsp.width}x{rtsp.height}")
    return "; ".join(parts)[:950]

def build_sigzel_payload(camera: CameraConfig, state: CameraState, icmp: Optional[IcmpResult], rtsp: Optional[RtspResult]) -> Dict[str, Any]:
    payload = {"name": camera.name, "status": state.last_status, "notes": build_notes(camera, icmp, rtsp)}
    if camera.sigzel_id:
        payload["id"] = camera.sigzel_id
    if icmp is not None:
        payload.update({"reachable": icmp.reachable, "latency_min_ms": round_or_none(icmp.latency_min_ms), "latency_avg_ms": round_or_none(icmp.latency_avg_ms), "latency_max_ms": round_or_none(icmp.latency_max_ms), "packet_loss_pct": round_or_none(icmp.packet_loss_pct), "jitter_ms": round_or_none(icmp.jitter_ms), "consecutive_failures": state.failure_count, "consecutive_successes": state.success_count, "icmp_status": state.last_status})
        if icmp.reachable and state.last_response_at:
            payload["last_response_at"] = state.last_response_at
    if rtsp is not None:
        payload.update({"rtsp_url": rtsp_url_for_payload(camera), "connection_ok": rtsp.connection_ok, "frame_captured": rtsp.frame_captured, "attempt_duration_ms": rtsp.attempt_duration_ms, "snapshot_path": rtsp.snapshot_path, "snapshot_size_bytes": rtsp.snapshot_size_bytes, "snapshot_status": rtsp.snapshot_status, "rtsp_error": rtsp.rtsp_error})
    return {k: v for k, v in payload.items() if v is not None or k == "rtsp_error"}

def send_bulk_update(updates: List[Dict[str, Any]]) -> bool:
    if not updates:
        return True
    if SIGZEL_DRY_RUN:
        logger.info("DRY RUN active. Nothing will be sent to SIGZEL.")
        for update in updates:
            safe = dict(update)
            if "rtsp_url" in safe:
                safe["rtsp_url"] = redact_url(str(safe["rtsp_url"]))
            logger.info("Local payload: %s", json.dumps(safe, ensure_ascii=False))
        return True
    if not WORKER_KEY:
        logger.error("SIGZEL_DRY_RUN=false but no SIGZEL_WORKER_KEY/CFTV_WORKER_API_KEY was set.")
        return False
    url = f"{API_URL}?action=bulk_update"
    payload = {"cameras": updates}
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            response = requests.post(url, headers=get_headers(), json=payload, timeout=HTTP_TIMEOUT_SECONDS)
            if response.status_code < 400:
                logger.info("SIGZEL bulk_update OK: %s cameras", len(updates))
                return True
            if response.status_code < 500:
                logger.error("Non-retryable SIGZEL error %s: %s", response.status_code, response.text[:700])
                return False
            logger.warning("SIGZEL error %s attempt %s/%s: %s", response.status_code, attempt, HTTP_RETRIES, response.text[:700])
        except Exception as exc:
            logger.warning("HTTP failure attempt %s/%s: %s", attempt, HTTP_RETRIES, exc)
        if attempt < HTTP_RETRIES:
            time.sleep(HTTP_RETRY_BASE_SECONDS * attempt)
    return False

class SigzelCftvWorker:
    def __init__(self, cameras: List[CameraConfig]) -> None:
        self.cameras = cameras
        self.states: Dict[str, CameraState] = {camera.name: CameraState() for camera in cameras}
        self.last_config_refresh_epoch = time.time()

    def refresh_config_if_due(self, force: bool = False) -> None:
        if CONFIG_REFRESH_SECONDS <= 0 and not force:
            return
        now = time.time()
        if not force and now - self.last_config_refresh_epoch < CONFIG_REFRESH_SECONDS:
            return
        self.last_config_refresh_epoch = now
        try:
            new_cameras = load_cameras_from_source()
        except Exception as exc:
            logger.warning("Config reload failed: %s", exc)
            return
        old_names = {cam.name for cam in self.cameras}
        new_names = {cam.name for cam in new_cameras}
        self.cameras = new_cameras
        for cam in self.cameras:
            if cam.name not in self.states:
                self.states[cam.name] = CameraState()
                logger.info("New camera assigned: %s | %s", cam.name, cam.ip)
        for name in list(self.states.keys()):
            if name not in new_names:
                self.states.pop(name, None)
                logger.info("Camera removed from assignment: %s", name)
        if old_names != new_names:
            logger.info("Config updated: before=%s | now=%s", len(old_names), len(new_names))

    def due_icmp(self, camera: CameraConfig, now: float) -> bool:
        return now - self.states[camera.name].last_icmp_check_epoch >= ICMP_INTERVAL_SECONDS

    def due_rtsp(self, camera: CameraConfig, now: float) -> bool:
        return now - self.states[camera.name].last_rtsp_check_epoch >= RTSP_INTERVAL_SECONDS

    def update_state_icmp(self, camera: CameraConfig, result: IcmpResult) -> None:
        state = self.states[camera.name]
        state.last_icmp_check_epoch = time.time()
        if result.reachable:
            state.failure_count = 0
            state.success_count += 1
            state.last_response_at = utc_now_iso_z()
            state.last_error = None
            if state.success_count >= ONLINE_AFTER_SUCCESSES:
                state.last_status = "online"
        else:
            state.success_count = 0
            state.failure_count += 1
            state.last_error = result.error or result.classification
            if state.failure_count >= OFFLINE_AFTER_FAILURES:
                state.last_status = "offline"
            elif state.last_status not in ("online", "offline"):
                state.last_status = "pendente"

    def update_state_rtsp(self, camera: CameraConfig, result: RtspResult) -> None:
        state = self.states[camera.name]
        state.last_rtsp_check_epoch = time.time()
        if not result.frame_captured and result.rtsp_error:
            state.last_error = result.rtsp_error

    def run_icmp_batch(self, batch: List[CameraConfig]) -> List[Tuple[CameraConfig, IcmpResult]]:
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_ICMP_WORKERS) as executor:
            futures = {executor.submit(check_icmp, camera.ip): camera for camera in batch}
            for future in concurrent.futures.as_completed(futures):
                camera = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = IcmpResult(False, PING_COUNT, 0, PING_COUNT, None, None, None, None, None, None, [], "ERRO ICMP", error=str(exc))
                self.update_state_icmp(camera, result)
                results.append((camera, result))
                logger.info("ICMP | %s | %s | loss=%s%% | avg=%sms | %s", camera.name, "OK" if result.reachable else "FAIL", result.packet_loss_pct, result.latency_avg_ms, result.classification)
        return results

    def run_rtsp_batch(self, batch: List[CameraConfig]) -> List[Tuple[CameraConfig, RtspResult]]:
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_RTSP_WORKERS) as executor:
            futures = {executor.submit(capture_rtsp_frame, camera): camera for camera in batch}
            for future in concurrent.futures.as_completed(futures):
                camera = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = RtspResult(False, False, 0, snapshot_status="falha", rtsp_error=str(exc))
                self.update_state_rtsp(camera, result)
                results.append((camera, result))
                logger.info("RTSP | %s | conn=%s | frame=%s | dur=%sms | err=%s", camera.name, result.connection_ok, result.frame_captured, result.attempt_duration_ms, result.rtsp_error)
        return results

    def run_cycle(self, force: bool = False) -> None:
        self.refresh_config_if_due(False)
        now = time.time()
        icmp_batch = [cam for cam in self.cameras if force or self.due_icmp(cam, now)]
        rtsp_batch = [cam for cam in self.cameras if force or self.due_rtsp(cam, now)]
        icmp_results: Dict[str, IcmpResult] = {}
        rtsp_results: Dict[str, RtspResult] = {}
        if icmp_batch:
            logger.info("Running ICMP on %s cameras", len(icmp_batch))
            for camera, result in self.run_icmp_batch(icmp_batch):
                icmp_results[camera.name] = result
        if rtsp_batch:
            filtered = []
            if SKIP_RTSP_IF_ICMP_UNREACHABLE:
                for camera in rtsp_batch:
                    icmp_result = icmp_results.get(camera.name)
                    if icmp_result is not None and not icmp_result.reachable:
                        rtsp_results[camera.name] = RtspResult(False, False, 0, snapshot_status="falha", rtsp_error="skip_rtsp_icmp_unreachable")
                        self.update_state_rtsp(camera, rtsp_results[camera.name])
                        logger.info("RTSP | %s | skipped because ICMP failed", camera.name)
                    else:
                        filtered.append(camera)
                rtsp_batch = filtered
            if rtsp_batch:
                logger.info("Running RTSP frame on %s cameras", len(rtsp_batch))
                for camera, result in self.run_rtsp_batch(rtsp_batch):
                    rtsp_results[camera.name] = result
        updates = []
        for name in set(icmp_results) | set(rtsp_results):
            camera = next(cam for cam in self.cameras if cam.name == name)
            updates.append(build_sigzel_payload(camera, self.states[name], icmp_results.get(name), rtsp_results.get(name)))
        if updates:
            send_bulk_update(updates)

    def run_forever(self) -> None:
        logger.info("Starting SIGZEL CFTV Worker System Driven")
        logger.info("WORKER_ID=%s | WORKER_NAME=%s", WORKER_ID or "-", WORKER_NAME)
        logger.info("CAMERA_SOURCE=%s | cameras=%s", CAMERA_SOURCE, len(self.cameras))
        logger.info("ICMP=%ss | RTSP=%ss | config_refresh=%ss", ICMP_INTERVAL_SECONDS, RTSP_INTERVAL_SECONDS, CONFIG_REFRESH_SECONDS)
        logger.info("DRY_RUN=%s | skip_rtsp_if_icmp_fail=%s", SIGZEL_DRY_RUN, SKIP_RTSP_IF_ICMP_UNREACHABLE)
        while True:
            try:
                self.run_cycle(False)
            except KeyboardInterrupt:
                logger.info("Worker stopped manually.")
                break
            except Exception as exc:
                logger.exception("Main loop error: %s", exc)
            try:
                time.sleep(MAIN_LOOP_SLEEP_SECONDS)
            except KeyboardInterrupt:
                logger.info("Worker stopped manually during sleep.")
                break

def main() -> None:
    parser = argparse.ArgumentParser(description="SIGZEL CFTV Worker System Driven")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    parser.add_argument("--validate-config", action="store_true", help="Load config and list cameras.")
    parser.add_argument("--sync-cache", action="store_true", help="Fetch config and write cache JSON.")
    args = parser.parse_args()
    cameras = load_cameras_from_source()
    if args.validate_config:
        logger.info("Config loaded: %s cameras", len(cameras))
        for cam in cameras:
            logger.info("CAM | name=%s | ip=%s | local=%s | id=%s | rtsp=%s", cam.name, cam.ip, cam.work_location_id, cam.sigzel_id, redact_url(cam.rtsp_url))
        return
    if args.sync_cache:
        save_cameras_cache(cameras)
        return
    if not cameras:
        logger.warning("No cameras loaded for this worker. Check CAMERA_SOURCE, token/key, locations and API.")
    worker = SigzelCftvWorker(cameras)
    if args.once:
        worker.run_cycle(True)
        return
    worker.run_forever()

if __name__ == "__main__":
    main()