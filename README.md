# SIGZEL CFTV Worker API v2

Worker 24/7 para monitorar câmeras IP por ICMP e RTSP, compatível com a nova API CFTV SIGZEL.

## O que ele faz

- Lê as câmeras do `cameras.json`.
- Executa ICMP/ping para medir:
  - reachable
  - latency_min_ms
  - latency_avg_ms
  - latency_max_ms
  - packet_loss_pct
  - jitter_ms
  - consecutive_failures
  - consecutive_successes
  - last_response_at
  - icmp_status

- Executa RTSP sem streaming contínuo:
  - abre a conexão RTSP
  - captura apenas 1 frame
  - fecha a conexão
  - mede attempt_duration_ms
  - salva somente a última imagem por câmera por padrão

- Envia para o SIGZEL via:
  - POST /functions/v1/cftv-worker?action=bulk_update

## Arquivos

- `sigzel_cftv_worker.py`: worker principal
- `cameras.json`: câmeras cadastradas
- `.env.example`: exemplo de configuração
- `requirements.txt`: dependências
- `test_worker_payload.py`: teste local de coerência do payload

## Como instalar

```bash
pip install -r requirements.txt
```

## Como configurar

Copie o `.env.example` para `.env`.

Windows PowerShell:

```powershell
copy .env.example .env
```

Linux:

```bash
cp .env.example .env
```

## Teste sem enviar ao SIGZEL

No `.env`, deixe:

```env
SIGZEL_DRY_RUN=true
SIGZEL_WORKER_KEY=
```

Depois rode:

```bash
python sigzel_cftv_worker.py --once
```

Isso executa um ciclo único e mostra o payload no terminal sem enviar ao SIGZEL.

## Rodar 24/7

```bash
python sigzel_cftv_worker.py
```

Por padrão:

```env
ICMP_INTERVAL_SECONDS=180
RTSP_INTERVAL_SECONDS=300
```

Ou seja:

- ICMP a cada 3 minutos.
- RTSP frame a cada 5 minutos.

Se quiser tudo a cada 5 minutos:

```env
ICMP_INTERVAL_SECONDS=300
RTSP_INTERVAL_SECONDS=300
```

## Enviar de verdade para o SIGZEL

No `.env`:

```env
SIGZEL_DRY_RUN=false
SIGZEL_WORKER_KEY=SUA_CHAVE_AQUI
```

Ou:

```env
CFTV_WORKER_API_KEY=SUA_CHAVE_AQUI
```

## Segurança sobre RTSP

Por padrão:

```env
SEND_RTSP_CREDENTIALS=false
```

Assim o worker usa a URL RTSP completa localmente, mas envia ao SIGZEL a URL com senha mascarada.

Exemplo:

```text
rtsp://admin:***@192.168.0.236:554/cam/realmonitor?channel=1&subtype=1
```

Se o SIGZEL precisar armazenar a URL completa, altere para:

```env
SEND_RTSP_CREDENTIALS=true
```

## Câmera de teste

O `cameras.json` vem com:

```json
[
  {
    "name": "CAMERA_TESTE_192_168_0_236",
    "ip": "192.168.0.236",
    "rtsp_url": "rtsp://admin:SENHA@192.168.0.236:554/cam/realmonitor?channel=1&subtype=1",
    "location": "TESTE",
    "enabled": true,
    "notes": "Camera de teste local"
  }
]
```

Troque `SENHA` pela senha real da câmera.

## Testar a coerência do payload

```bash
python test_worker_payload.py
```

Esse teste valida:

- parser ICMP Windows
- campos da nova API SIGZEL
- payload bulk_update
- máscara de senha RTSP


## Cadastrar automaticamente as câmeras do cameras.json no SIGZEL

Para cadastrar as câmeras antes de monitorar, configure no `.env`:

```env
SIGZEL_DRY_RUN=false
SIGZEL_WORKER_KEY=SUA_CHAVE_AQUI
```

Depois execute:

```bash
python sigzel_cftv_worker.py --sync-cameras
```

Isso usa o endpoint:

```text
POST /functions/v1/cftv-worker
```

E envia para cada câmera:

```json
{
  "name": "Nome da câmera",
  "location": "Localização",
  "ip_address": "IP da câmera",
  "status": "pendente",
  "notes": "Observação"
}
```

Se quiser que o worker tente cadastrar as câmeras sempre que iniciar, use:

```env
AUTO_SYNC_CAMERAS=true
```

Recomendação: use `--sync-cameras` uma vez para cadastro inicial. Depois deixe `AUTO_SYNC_CAMERAS=false` em produção, a menos que você queira sincronização automática a cada reinício.
