import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("worker", ROOT / "sigzel_cftv_worker.py")
worker = importlib.util.module_from_spec(spec)
sys.modules["worker"] = worker
spec.loader.exec_module(worker)

PING_OUTPUT = """
Disparando 192.168.0.236 com 32 bytes de dados:
Resposta de 192.168.0.236: bytes=32 tempo=11ms TTL=64
Resposta de 192.168.0.236: bytes=32 tempo=20ms TTL=64
Resposta de 192.168.0.236: bytes=32 tempo=13ms TTL=64
Resposta de 192.168.0.236: bytes=32 tempo=13ms TTL=64
Resposta de 192.168.0.236: bytes=32 tempo=10ms TTL=64
Resposta de 192.168.0.236: bytes=32 tempo=14ms TTL=64
Resposta de 192.168.0.236: bytes=32 tempo=12ms TTL=64
Resposta de 192.168.0.236: bytes=32 tempo=12ms TTL=64
Resposta de 192.168.0.236: bytes=32 tempo=10ms TTL=64
Resposta de 192.168.0.236: bytes=32 tempo=11ms TTL=64

Estat¡sticas do Ping para 192.168.0.236:
    Pacotes: Enviados = 10, Recebidos = 10, Perdidos = 0 (0% de
             perda),
Aproximar um n£mero redondo de vezes em milissegundos:
    M¡nimo = 10ms, M ximo = 20ms, M‚dia = 12ms
"""

icmp = worker.parse_icmp_result("192.168.0.236", PING_OUTPUT, 0)
assert icmp.reachable is True
assert icmp.packets_sent == 10
assert icmp.packets_received == 10
assert icmp.packets_lost == 0
assert icmp.packet_loss_pct == 0.0
assert icmp.latency_min_ms == 10.0
assert icmp.latency_avg_ms == 12.0
assert icmp.latency_max_ms == 20.0
assert icmp.jitter_ms == 10.0
assert icmp.ttl_detected == 64

camera = worker.CameraConfig(
    name="CAMERA_TESTE_192_168_0_236",
    ip="192.168.0.236",
    rtsp_url="rtsp://admin:SENHA@192.168.0.236:554/cam/realmonitor?channel=1&subtype=1",
    notes="Camera de teste local"
)

state = worker.CameraState()
state.success_count = 1
state.failure_count = 0
state.last_status = "online"
state.last_response_at = "2026-05-08T18:35:39Z"

rtsp = worker.RtspResult(
    connection_ok=True,
    frame_captured=True,
    attempt_duration_ms=812,
    snapshot_path="snapshots/CAMERA_TESTE_192_168_0_236_latest.jpg",
    snapshot_size_bytes=184320,
    snapshot_status="ok",
    rtsp_error=None,
    width=640,
    height=360
)

payload = worker.build_sigzel_payload(camera, state, icmp=icmp, rtsp=rtsp)

required_keys = [
    "name",
    "status",
    "reachable",
    "latency_min_ms",
    "latency_avg_ms",
    "latency_max_ms",
    "packet_loss_pct",
    "jitter_ms",
    "consecutive_failures",
    "consecutive_successes",
    "last_response_at",
    "icmp_status",
    "rtsp_url",
    "connection_ok",
    "frame_captured",
    "attempt_duration_ms",
    "snapshot_path",
    "snapshot_size_bytes",
    "snapshot_status",
    "rtsp_error",
]

missing = [key for key in required_keys if key not in payload]
assert not missing, f"Campos ausentes no payload: {missing}"

assert payload["reachable"] is True
assert payload["icmp_status"] == "online"
assert payload["connection_ok"] is True
assert payload["frame_captured"] is True
assert payload["snapshot_status"] == "ok"
assert "SENHA" not in payload["rtsp_url"], "Senha RTSP não deveria ir no payload por padrão"
assert payload["rtsp_url"] == "rtsp://admin:***@192.168.0.236:554/cam/realmonitor?channel=1&subtype=1"

print("[OK] Parser ICMP, payload SIGZEL API v2 e máscara RTSP validados.")
print(json.dumps(payload, indent=2, ensure_ascii=False))


create_payload = worker.create_camera_payload(camera)
assert create_payload["name"] == "CAMERA_TESTE_192_168_0_236"
assert create_payload["ip_address"] == "192.168.0.236"
assert create_payload["status"] == "pendente"
assert "rtsp_url" not in create_payload, "Cadastro não deve enviar RTSP/credenciais"

print("[OK] Payload de cadastro de câmera validado.")
print(json.dumps(create_payload, indent=2, ensure_ascii=False))
