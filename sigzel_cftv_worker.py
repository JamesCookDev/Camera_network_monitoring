from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import os
import platform
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import cv2
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass



API_URL = os.getenv(
    "SIGZEL_BASE_URL",
)

WORKER_KEY = os.getenv("SIGZEL_WORKER_KEY") or os.getenv("CFTV_WORKER_API_KEY") or ""

DRY_RUN = os.getenv("SIGZEL_DRY_RUN", "true").lower() in ("1", "true", "yes", "sim")
AUTO_SYNC_CAMERAS = os.getenv("AUTO_SYNC_CAMERAS", "false").lower() in ("1", "true", "yes", "sim")

CAMERAS_FILE = os.getenv("CAMERAS_FILE", "cameras.json")
SNAPSHOT_DIR = Path(os.getenv("SNAPSHOT_DIR", "snapshots"))
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

ICMP_INTERVAL_SECONDS = int(os.getenv("ICMP_INTERVAL_SECONDS", "180"))
RTSP_INTERVAL_SECONDS = int(os.getenv("RTSP_INTERVAL_SECONDS", "300"))
MAIN_LOOP_SLEEP_SECONDS = int(os.getenv("MAIN_LOOP_SLEEP_SECONDS", "5"))

MAX_ICMP_WORKERS = int(os.getenv("MAX_ICMP_WORKERS", "4"))
MAX_RTSP_WORKERS = int(os.getenv("MAX_RTSP_WORKERS", "1"))

PING_COUNT = int(os.getenv("PING_COUNT", "3"))
PING_TIMEOUT_SECONDS = int(os.getenv("PING_TIMEOUT_SECONDS", "2"))

RTSP_OPEN_TIMEOUT_MS = int(os.getenv("RTSP_OPEN_TIMEOUT_MS", "4000"))
RTSP_READ_ATTEMPTS = int(os.getenv("RTSP_READ_ATTEMPTS", "8"))
RTSP_FRAME_WIDTH = int(os.getenv("RTSP_FRAME_WIDTH", "640"))
JPEG_QUALITY = int(os.getenv("JPEG_QUALITY", "65"))


SNAPSHOT_MODE = os.getenv("SNAPSHOT_MODE", "keep_latest").lower()
SNAPSHOT_TTL_MINUTES = int(os.getenv("SNAPSHOT_TTL_MINUTES", "30"))

OFFLINE_AFTER_FAILURES = int(os.getenv("OFFLINE_AFTER_FAILURES", "3"))
ONLINE_AFTER_SUCCESSES = int(os.getenv("ONLINE_AFTER_SUCCESSES", "1"))

HTTP_TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "15"))
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "3"))
HTTP_RETRY_BASE_SECONDS = float(os.getenv("HTTP_RETRY_BASE_SECONDS", "2"))

SEND_RTSP_CREDENTIALS = os.getenv("SEND_RTSP_CREDENTIALS", "false").lower() in ("1", "true", "yes", "sim")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("sigzel-cftv-worker")


@dataclass
class CameraConfig:
    name: str
    ip: str
    rtsp_url: str = ""
    location: Optional[str] = None
    sigzel_id: Optional[str] = None
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
    if value is None:
        return None
    return round(float(value), digits)


def load_cameras(path: str) -> List[CameraConfig]:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Arquivo de câmeras não encontrado: {file_path.resolve()}")

    data = json.loads(file_path.read_text(encoding="utf-8"))
    cameras: List[CameraConfig] = []

    for item in data:
        cameras.append(
            CameraConfig(
                name=item["name"],
                ip=item["ip"],
                rtsp_url=item.get("rtsp_url", ""),
                location=item.get("location"),
                sigzel_id=item.get("sigzel_id"),
                enabled=bool(item.get("enabled", True)),
                notes=item.get("notes"),
            )
        )

    return [camera for camera in cameras if camera.enabled]


def safe_filename(name: str) -> str:
    chars = []
    for ch in name:
        if ch.isalnum() or ch in ("-", "_", "."):
            chars.append(ch)
        else:
            chars.append("_")
    return "".join(chars)[:120]


def redact_url(url: str) -> str:
    if not url:
        return url

    try:
        parts = urlsplit(url)
        if "@" not in parts.netloc:
            return url

        userinfo, host = parts.netloc.rsplit("@", 1)

        if ":" in userinfo:
            user = userinfo.split(":", 1)[0]
            netloc = f"{user}:***@{host}"
        else:
            netloc = f"***@{host}"

        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

    except Exception:
        return "rtsp://***"


def rtsp_url_for_payload(camera: CameraConfig) -> Optional[str]:
    if not camera.rtsp_url:
        return None

    if SEND_RTSP_CREDENTIALS:
        return camera.rtsp_url

    return redact_url(camera.rtsp_url)


def classify_icmp(
    reachable: bool,
    packet_loss_pct: Optional[float],
    latency_avg_ms: Optional[float],
    latency_max_ms: Optional[float],
    jitter_ms: Optional[float],
) -> str:
    if not reachable:
        return "OFFLINE / INACESSIVEL VIA ICMP"

    if packet_loss_pct is not None and packet_loss_pct >= 100:
        return "OFFLINE / 100% DE PERDA"

    if packet_loss_pct is not None and packet_loss_pct > 5:
        return "CRITICO - perda de pacotes alta"

    if latency_avg_ms is not None and latency_avg_ms > 100:
        return "CRITICO - latencia media muito alta"

    if jitter_ms is not None and jitter_ms > 30:
        return "INSTAVEL - jitter alto"

    if latency_max_ms is not None and latency_max_ms > 100:
        return "INSTAVEL - pico de latencia alto"

    if packet_loss_pct is not None and packet_loss_pct > 0:
        return "ATENCAO - houve perda de pacotes"

    if latency_avg_ms is not None and latency_avg_ms > 50:
        return "ATENCAO - latencia elevada"

    return "OK - rede saudavel"


def build_ping_command(ip: str) -> List[str]:
    system = platform.system().lower()

    if "windows" in system:
        return [
            "ping",
            "-n", str(PING_COUNT),
            "-w", str(PING_TIMEOUT_SECONDS * 1000),
            ip,
        ]

    return [
        "ping",
        "-c", str(PING_COUNT),
        "-W", str(PING_TIMEOUT_SECONDS),
        ip,
    ]


def extract_ttls(output: str) -> List[int]:
    values: List[int] = []
    for match in re.findall(r"ttl[= ](\d+)", output, flags=re.IGNORECASE):
        try:
            values.append(int(match))
        except ValueError:
            pass
    return values


def parse_icmp_result(ip: str, output: str, return_code: int) -> IcmpResult:
    text = output.replace(",", ".")

    sent: Optional[int] = None
    received: Optional[int] = None
    lost: Optional[int] = None
    packet_loss_pct: Optional[float] = None

    latency_min_ms: Optional[float] = None
    latency_avg_ms: Optional[float] = None
    latency_max_ms: Optional[float] = None
    jitter_ms: Optional[float] = None

    ttl_values = extract_ttls(text)
    ttl_detected = ttl_values[0] if ttl_values else None

   
    packet_match = re.search(
        r"Enviados\s*=\s*(\d+).*?Recebidos\s*=\s*(\d+).*?Perdidos\s*=\s*(\d+).*?\((\d+(?:\.\d+)?)%.*?\)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    
    if not packet_match:
        packet_match = re.search(
            r"Sent\s*=\s*(\d+).*?Received\s*=\s*(\d+).*?Lost\s*=\s*(\d+).*?\((\d+(?:\.\d+)?)%.*?\)",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )

    if packet_match:
        sent = int(packet_match.group(1))
        received = int(packet_match.group(2))
        lost = int(packet_match.group(3))
        packet_loss_pct = float(packet_match.group(4))

    
    if sent is None or received is None:
        packet_match_linux = re.search(
            r"(\d+)\s+packets transmitted,\s+(\d+)\s+(?:packets\s+)?received.*?(\d+(?:\.\d+)?)%\s+packet loss",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )

        if packet_match_linux:
            sent = int(packet_match_linux.group(1))
            received = int(packet_match_linux.group(2))
            packet_loss_pct = float(packet_match_linux.group(3))
            lost = sent - received

    
    latency_match = re.search(
        r"M.nimo\s*=\s*(\d+(?:\.\d+)?)ms.*?M.ximo\s*=\s*(\d+(?:\.\d+)?)ms.*?M.dia\s*=\s*(\d+(?:\.\d+)?)ms",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if not latency_match:
        latency_match = re.search(
            r"Minimum\s*=\s*(\d+(?:\.\d+)?)ms.*?Maximum\s*=\s*(\d+(?:\.\d+)?)ms.*?Average\s*=\s*(\d+(?:\.\d+)?)ms",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )

    if latency_match:
        latency_min_ms = float(latency_match.group(1))
        latency_max_ms = float(latency_match.group(2))
        latency_avg_ms = float(latency_match.group(3))

    if latency_min_ms is None:
        latency_match_linux = re.search(
            r"min/avg/max/(?:mdev|stddev)\s*=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)",
            text,
            flags=re.IGNORECASE,
        )

        if latency_match_linux:
            latency_min_ms = float(latency_match_linux.group(1))
            latency_avg_ms = float(latency_match_linux.group(2))
            latency_max_ms = float(latency_match_linux.group(3))
            jitter_ms = float(latency_match_linux.group(4))

    if sent is None:
        sent = PING_COUNT

    if received is None:
        received = 0 if return_code != 0 else 0

    if lost is None:
        lost = max(sent - received, 0)

    if packet_loss_pct is None and sent > 0:
        packet_loss_pct = round((lost / sent) * 100, 1)

    if jitter_ms is None and latency_min_ms is not None and latency_max_ms is not None:
        jitter_ms = latency_max_ms - latency_min_ms

    reachable = return_code == 0 and received > 0

    classification = classify_icmp(
        reachable=reachable,
        packet_loss_pct=packet_loss_pct,
        latency_avg_ms=latency_avg_ms,
        latency_max_ms=latency_max_ms,
        jitter_ms=jitter_ms,
    )

    return IcmpResult(
        reachable=reachable,
        packets_sent=sent,
        packets_received=received,
        packets_lost=lost,
        packet_loss_pct=packet_loss_pct,
        latency_min_ms=latency_min_ms,
        latency_avg_ms=latency_avg_ms,
        latency_max_ms=latency_max_ms,
        jitter_ms=jitter_ms,
        ttl_detected=ttl_detected,
        ttl_values=ttl_values,
        classification=classification,
        raw=output[-1500:],
    )


def check_icmp(ip: str) -> IcmpResult:
    cmd = build_ping_command(ip)

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=(PING_COUNT * PING_TIMEOUT_SECONDS) + 5,
            check=False,
        )

        raw = (completed.stdout or "") + "\n" + (completed.stderr or "")
        return parse_icmp_result(ip, raw, completed.returncode)

    except subprocess.TimeoutExpired as exc:
        return IcmpResult(
            reachable=False,
            packets_sent=PING_COUNT,
            packets_received=0,
            packets_lost=PING_COUNT,
            packet_loss_pct=100.0,
            latency_min_ms=None,
            latency_avg_ms=None,
            latency_max_ms=None,
            jitter_ms=None,
            ttl_detected=None,
            ttl_values=[],
            classification="OFFLINE / TIMEOUT ICMP",
            error=f"icmp_timeout: {exc}",
        )

    except Exception as exc:
        return IcmpResult(
            reachable=False,
            packets_sent=PING_COUNT,
            packets_received=0,
            packets_lost=PING_COUNT,
            packet_loss_pct=None,
            latency_min_ms=None,
            latency_avg_ms=None,
            latency_max_ms=None,
            jitter_ms=None,
            ttl_detected=None,
            ttl_values=[],
            classification="ERRO ICMP",
            error=f"icmp_error: {exc}",
        )


def cleanup_old_snapshots() -> None:
    if SNAPSHOT_MODE != "ttl":
        return

    try:
        cutoff = time.time() - (SNAPSHOT_TTL_MINUTES * 60)

        for file_path in SNAPSHOT_DIR.glob("*.jpg"):
            if file_path.stat().st_mtime < cutoff:
                file_path.unlink(missing_ok=True)

    except Exception as exc:
        logger.warning("Falha ao limpar snapshots antigos: %s", exc)


def capture_rtsp_frame(camera: CameraConfig) -> RtspResult:
    start = time.time()
    cap = None

    if not camera.rtsp_url:
        duration = int((time.time() - start) * 1000)
        return RtspResult(
            connection_ok=False,
            frame_captured=False,
            attempt_duration_ms=duration,
            snapshot_status="falha",
            rtsp_error="rtsp_url_vazia",
        )

    try:
        os.environ.setdefault(
            "OPENCV_FFMPEG_CAPTURE_OPTIONS",
            f"rtsp_transport;tcp|stimeout;{RTSP_OPEN_TIMEOUT_MS * 1000}",
        )

        cap = cv2.VideoCapture(camera.rtsp_url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, RTSP_OPEN_TIMEOUT_MS)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, RTSP_OPEN_TIMEOUT_MS)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if not cap.isOpened():
            duration = int((time.time() - start) * 1000)
            return RtspResult(
                connection_ok=False,
                frame_captured=False,
                attempt_duration_ms=duration,
                snapshot_status="falha",
                rtsp_error="rtsp_open_failed",
            )

        frame = None

        for _ in range(RTSP_READ_ATTEMPTS):
            ok, candidate = cap.read()

            if ok and candidate is not None:
                frame = candidate
                break

        if frame is None:
            duration = int((time.time() - start) * 1000)
            return RtspResult(
                connection_ok=True,
                frame_captured=False,
                attempt_duration_ms=duration,
                snapshot_status="falha",
                rtsp_error="rtsp_frame_read_failed",
            )

        height, width = frame.shape[:2]

        if width > RTSP_FRAME_WIDTH:
            ratio = RTSP_FRAME_WIDTH / float(width)
            new_height = int(height * ratio)
            frame = cv2.resize(frame, (RTSP_FRAME_WIDTH, new_height))
            height, width = frame.shape[:2]

        if SNAPSHOT_MODE == "none":
            duration = int((time.time() - start) * 1000)
            return RtspResult(
                connection_ok=True,
                frame_captured=True,
                attempt_duration_ms=duration,
                snapshot_path=None,
                snapshot_size_bytes=None,
                snapshot_status="ok",
                rtsp_error=None,
                width=width,
                height=height,
            )

        if SNAPSHOT_MODE == "keep_latest":
            filename = f"{safe_filename(camera.name)}_latest.jpg"
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{safe_filename(camera.name)}_{timestamp}.jpg"

        path = SNAPSHOT_DIR / filename

        ok = cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])

        if not ok:
            duration = int((time.time() - start) * 1000)
            return RtspResult(
                connection_ok=True,
                frame_captured=True,
                attempt_duration_ms=duration,
                snapshot_status="falha",
                rtsp_error="snapshot_write_failed",
                width=width,
                height=height,
            )

        cleanup_old_snapshots()

        duration = int((time.time() - start) * 1000)
        return RtspResult(
            connection_ok=True,
            frame_captured=True,
            attempt_duration_ms=duration,
            snapshot_path=path.as_posix(),
            snapshot_size_bytes=path.stat().st_size,
            snapshot_status="ok",
            rtsp_error=None,
            width=width,
            height=height,
        )

    except Exception as exc:
        duration = int((time.time() - start) * 1000)
        return RtspResult(
            connection_ok=False,
            frame_captured=False,
            attempt_duration_ms=duration,
            snapshot_status="falha",
            rtsp_error=f"rtsp_error: {exc}",
        )

    finally:
        if cap is not None:
            cap.release()



def build_notes(camera: CameraConfig, icmp: Optional[IcmpResult], rtsp: Optional[RtspResult]) -> str:
    parts = []

    if camera.notes:
        parts.append(camera.notes)

    if icmp:
        parts.append(f"classificacao_icmp={icmp.classification}")
        if icmp.ttl_detected is not None:
            parts.append(f"ttl={icmp.ttl_detected}")

    if rtsp and rtsp.width and rtsp.height:
        parts.append(f"snapshot_res={rtsp.width}x{rtsp.height}")

    return "; ".join(parts)[:950] if parts else ""


def build_sigzel_payload(
    camera: CameraConfig,
    state: CameraState,
    icmp: Optional[IcmpResult] = None,
    rtsp: Optional[RtspResult] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "name": camera.name,
        "status": state.last_status,
    }

    if camera.sigzel_id:
        payload["id"] = camera.sigzel_id

    notes = build_notes(camera, icmp, rtsp)
    if notes:
        payload["notes"] = notes

    if icmp is not None:
        payload.update(
            {
                "reachable": icmp.reachable,
                "latency_min_ms": round_or_none(icmp.latency_min_ms),
                "latency_avg_ms": round_or_none(icmp.latency_avg_ms),
                "latency_max_ms": round_or_none(icmp.latency_max_ms),
                "packet_loss_pct": round_or_none(icmp.packet_loss_pct),
                "jitter_ms": round_or_none(icmp.jitter_ms),
                "consecutive_failures": state.failure_count,
                "consecutive_successes": state.success_count,
                "icmp_status": state.last_status,
            }
        )

        if icmp.reachable and state.last_response_at:
            payload["last_response_at"] = state.last_response_at

    if rtsp is not None:
        payload.update(
            {
                "rtsp_url": rtsp_url_for_payload(camera),
                "connection_ok": rtsp.connection_ok,
                "frame_captured": rtsp.frame_captured,
                "attempt_duration_ms": rtsp.attempt_duration_ms,
                "snapshot_path": rtsp.snapshot_path,
                "snapshot_size_bytes": rtsp.snapshot_size_bytes,
                "snapshot_status": rtsp.snapshot_status,
                "rtsp_error": rtsp.rtsp_error,
            }
        )

    cleaned = {}
    for key, value in payload.items():
        if value is not None or key == "rtsp_error":
            cleaned[key] = value

    return cleaned



def send_bulk_update(updates: List[Dict[str, Any]]) -> bool:
    if not updates:
        return True

    if DRY_RUN:
        logger.info("DRY RUN ativo. Nenhum dado será enviado ao SIGZEL.")
        for update in updates:
            safe_update = dict(update)
            if "rtsp_url" in safe_update:
                safe_update["rtsp_url"] = redact_url(str(safe_update["rtsp_url"]))
            logger.info("Payload local: %s", json.dumps(safe_update, ensure_ascii=False))
        return True

    if not WORKER_KEY:
        logger.error("SIGZEL_DRY_RUN=false, mas nenhuma chave foi configurada em SIGZEL_WORKER_KEY ou CFTV_WORKER_API_KEY.")
        return False

    url = f"{API_URL}?action=bulk_update"
    headers = {
        "x-worker-key": WORKER_KEY,
        "Content-Type": "application/json",
    }
    payload = {"cameras": updates}

    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=HTTP_TIMEOUT_SECONDS,
            )

            if response.status_code < 400:
                logger.info("SIGZEL bulk_update OK: %s câmera(s)", len(updates))
                return True

            if 400 <= response.status_code < 500:
                logger.error("Erro não retentável SIGZEL %s: %s", response.status_code, response.text[:500])
                return False

            logger.warning("Erro SIGZEL %s tentativa %s/%s: %s", response.status_code, attempt, HTTP_RETRIES, response.text[:500])

        except Exception as exc:
            logger.warning("Falha HTTP tentativa %s/%s: %s", attempt, HTTP_RETRIES, exc)

        if attempt < HTTP_RETRIES:
            time.sleep(HTTP_RETRY_BASE_SECONDS * attempt)

    logger.error("Falha ao enviar bulk_update depois de %s tentativa(s).", HTTP_RETRIES)
    return False



def create_camera_payload(camera: CameraConfig) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "name": camera.name,
        "location": camera.location,
        "ip_address": camera.ip,
        "status": "pendente",
        "notes": camera.notes or "Cadastrada automaticamente pelo SIGZEL CFTV Worker",
        "work_location_id": None,
    }

    return {key: value for key, value in payload.items() if value is not None}


def create_camera_in_sigzel(camera: CameraConfig) -> bool:
    payload = create_camera_payload(camera)

    if DRY_RUN:
        logger.info("DRY RUN ativo. Cadastro não enviado. Payload create: %s", json.dumps(payload, ensure_ascii=False))
        return True

    if not WORKER_KEY:
        logger.error("Não é possível cadastrar câmera: chave do worker ausente.")
        return False

    headers = {
        "x-worker-key": WORKER_KEY,
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            API_URL,
            headers=headers,
            json=payload,
            timeout=HTTP_TIMEOUT_SECONDS,
        )

        if response.status_code == 201:
            logger.info("Câmera cadastrada no SIGZEL: %s", camera.name)
            return True

        body = response.text[:1000]
        body_lower = body.lower()

        already_exists_markers = [
            "duplicate",
            "duplicado",
            "already exists",
            "já existe",
            "unique",
            "violates unique",
            "câmera já cadastrada",
            "camera ja cadastrada",
        ]

        if response.status_code in (400, 409, 500) and any(marker in body_lower for marker in already_exists_markers):
            logger.info("Câmera aparentemente já cadastrada no SIGZEL: %s", camera.name)
            return True

        logger.warning("Falha ao cadastrar câmera %s: HTTP %s | %s", camera.name, response.status_code, body)
        return False

    except Exception as exc:
        logger.warning("Erro ao cadastrar câmera %s: %s", camera.name, exc)
        return False


def sync_cameras_to_sigzel(cameras: List[CameraConfig]) -> None:
    logger.info("Sincronizando cadastro de %s câmera(s) do cameras.json com o SIGZEL", len(cameras))

    success_count = 0
    fail_count = 0

    for camera in cameras:
        ok = create_camera_in_sigzel(camera)

        if ok:
            success_count += 1
        else:
            fail_count += 1

        time.sleep(0.2)

    logger.info("Sincronização de cadastro concluída: sucesso=%s | falha=%s", success_count, fail_count)




class SigzelCftvWorker:
    def __init__(self, cameras: List[CameraConfig]) -> None:
        self.cameras = cameras
        self.states: Dict[str, CameraState] = {camera.name: CameraState() for camera in cameras}

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
        output: List[Tuple[CameraConfig, IcmpResult]] = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_ICMP_WORKERS) as executor:
            future_map = {executor.submit(check_icmp, camera.ip): camera for camera in batch}

            for future in concurrent.futures.as_completed(future_map):
                camera = future_map[future]

                try:
                    result = future.result()
                except Exception as exc:
                    result = IcmpResult(
                        reachable=False,
                        packets_sent=PING_COUNT,
                        packets_received=0,
                        packets_lost=PING_COUNT,
                        packet_loss_pct=None,
                        latency_min_ms=None,
                        latency_avg_ms=None,
                        latency_max_ms=None,
                        jitter_ms=None,
                        ttl_detected=None,
                        ttl_values=[],
                        classification="ERRO ICMP",
                        error=str(exc),
                    )

                self.update_state_icmp(camera, result)
                output.append((camera, result))

                logger.info(
                    "ICMP | %s | %s | perda=%s%% | avg=%sms | jitter=%sms | %s",
                    camera.name,
                    "OK" if result.reachable else "FALHA",
                    result.packet_loss_pct,
                    result.latency_avg_ms,
                    result.jitter_ms,
                    result.classification,
                )

        return output

    def run_rtsp_batch(self, batch: List[CameraConfig]) -> List[Tuple[CameraConfig, RtspResult]]:
        output: List[Tuple[CameraConfig, RtspResult]] = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_RTSP_WORKERS) as executor:
            future_map = {executor.submit(capture_rtsp_frame, camera): camera for camera in batch}

            for future in concurrent.futures.as_completed(future_map):
                camera = future_map[future]

                try:
                    result = future.result()
                except Exception as exc:
                    result = RtspResult(
                        connection_ok=False,
                        frame_captured=False,
                        attempt_duration_ms=0,
                        snapshot_status="falha",
                        rtsp_error=str(exc),
                    )

                self.update_state_rtsp(camera, result)
                output.append((camera, result))

                logger.info(
                    "RTSP | %s | conn=%s | frame=%s | dur=%sms | snapshot=%s | erro=%s",
                    camera.name,
                    result.connection_ok,
                    result.frame_captured,
                    result.attempt_duration_ms,
                    result.snapshot_path,
                    result.rtsp_error,
                )

        return output

    def run_cycle(self, force: bool = False) -> None:
        now = time.time()

        icmp_batch = [camera for camera in self.cameras if force or self.due_icmp(camera, now)]
        rtsp_batch = [camera for camera in self.cameras if force or self.due_rtsp(camera, now)]

        icmp_results: Dict[str, IcmpResult] = {}
        rtsp_results: Dict[str, RtspResult] = {}

        if icmp_batch:
            logger.info("Executando ICMP em %s câmera(s)", len(icmp_batch))
            for camera, result in self.run_icmp_batch(icmp_batch):
                icmp_results[camera.name] = result

        if rtsp_batch:
            logger.info("Executando RTSP frame em %s câmera(s)", len(rtsp_batch))
            for camera, result in self.run_rtsp_batch(rtsp_batch):
                rtsp_results[camera.name] = result

        touched_names = set(icmp_results.keys()) | set(rtsp_results.keys())
        updates: List[Dict[str, Any]] = []

        for name in touched_names:
            camera = next(camera for camera in self.cameras if camera.name == name)
            state = self.states[name]
            payload = build_sigzel_payload(
                camera=camera,
                state=state,
                icmp=icmp_results.get(name),
                rtsp=rtsp_results.get(name),
            )
            updates.append(payload)

        if updates:
            send_bulk_update(updates)

    def run_forever(self) -> None:
        logger.info("Iniciando SIGZEL CFTV Worker API v2")
        logger.info("Câmeras habilitadas: %s", len(self.cameras))
        logger.info("ICMP a cada %ss | RTSP a cada %ss", ICMP_INTERVAL_SECONDS, RTSP_INTERVAL_SECONDS)
        logger.info("Workers ICMP=%s | RTSP=%s", MAX_ICMP_WORKERS, MAX_RTSP_WORKERS)
        logger.info("DRY_RUN=%s", DRY_RUN)

        while True:
            try:
                self.run_cycle(force=False)
            except KeyboardInterrupt:
                logger.info("Worker interrompido manualmente.")
                break
            except Exception as exc:
                logger.exception("Erro no loop principal: %s", exc)

            time.sleep(MAIN_LOOP_SLEEP_SECONDS)



def main() -> None:
    parser = argparse.ArgumentParser(description="SIGZEL CFTV Worker API v2")
    parser.add_argument("--once", action="store_true", help="Executa somente um ciclo e encerra.")
    parser.add_argument("--validate-config", action="store_true", help="Valida cameras.json e encerra.")
    parser.add_argument("--sync-cameras", action="store_true", help="Cadastra no SIGZEL as câmeras do cameras.json e encerra.")
    args = parser.parse_args()

    cameras = load_cameras(CAMERAS_FILE)

    if not cameras:
        raise RuntimeError("Nenhuma câmera habilitada encontrada.")

    logger.info("Arquivo de câmeras carregado: %s", CAMERAS_FILE)
    logger.info("Total de câmeras habilitadas: %s", len(cameras))

    if args.validate_config:
        for camera in cameras:
            logger.info("OK config | name=%s | ip=%s | rtsp=%s", camera.name, camera.ip, redact_url(camera.rtsp_url))
        return

    if args.sync_cameras:
        sync_cameras_to_sigzel(cameras)
        return

    if AUTO_SYNC_CAMERAS:
        sync_cameras_to_sigzel(cameras)

    worker = SigzelCftvWorker(cameras)

    if args.once:
        worker.run_cycle(force=True)
        return

    worker.run_forever()


if __name__ == "__main__":
    main()
