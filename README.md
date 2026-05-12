# Monitoramento de rede das cameras

Script em Python para monitorar cameras IP na rede local. Este repositorio esta no GitHub apenas para documentar o funcionamento e manter uma copia segura do codigo, sem chaves, senhas ou configuracoes reais.

## Objetivo

O script executa verificacoes periodicas nas cameras configuradas e registra o estado de rede de cada uma.

Ele pode:

- Ler a lista local de cameras em `cameras.json`
- Testar conectividade por ping/ICMP
- Medir perda de pacotes, latencia e jitter
- Testar acesso RTSP capturando um frame
- Salvar snapshots localmente, se configurado
- Rodar uma vez para teste ou ficar em execucao continua

## Arquivos do projeto

- `sigzel_cftv_worker.py`: script principal de monitoramento
- `requirements.txt`: dependencias Python
- `.env.example`: modelo de configuracao sem credenciais
- `cameras.example.json`: modelo de cameras sem senhas reais
- `test_worker_payload.py`: teste local do parser e payload
- `.gitignore`: impede envio de arquivos locais e sensiveis

Arquivos locais que nao devem subir para o Git:

- `.env`
- `cameras.json`
- `.venv/`
- `snapshots/`
- `__pycache__/`
- logs e arquivos temporarios

## Instalacao

Crie e ative um ambiente virtual, se desejar:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
```

Instale as dependencias:

```powershell
pip install -r requirements.txt
```

## Configuracao local

Copie o exemplo de ambiente:

```powershell
copy .env.example .env
```

Copie o exemplo de cameras:

```powershell
copy cameras.example.json cameras.json
```

Depois edite localmente:

- `.env`: intervalos, modo de execucao e chave da API, se for usar envio externo
- `cameras.json`: IPs, nomes e URLs RTSP reais das cameras

Esses dois arquivos estao no `.gitignore` porque podem conter dados sensiveis.

## Exemplo de camera

```json
[
  {
    "name": "CAMERA_192_168_0_10",
    "ip": "192.168.0.10",
    "rtsp_url": "rtsp://admin:SENHA_AQUI@192.168.0.10:554/cam/realmonitor?channel=1&subtype=1",
    "location": "Portaria",
    "enabled": true
  }
]
```

Troque `SENHA_AQUI` apenas no seu `cameras.json` local.

## Rodar um teste

Executa um ciclo unico:

```powershell
python sigzel_cftv_worker.py --once
```

Validar o arquivo de cameras:

```powershell
python sigzel_cftv_worker.py --validate-config
```

Rodar teste local do payload:

```powershell
python test_worker_payload.py
```

## Rodar continuamente

```powershell
python sigzel_cftv_worker.py
```

Por padrao, os intervalos sao:

```env
ICMP_INTERVAL_SECONDS=180
RTSP_INTERVAL_SECONDS=300
```

Ou seja:

- Ping a cada 3 minutos
- Teste RTSP a cada 5 minutos

## Seguranca

Nunca envie para o GitHub:

- Chaves de API
- Arquivos `.env`
- Senhas RTSP
- `cameras.json` real
- Imagens de snapshots, caso revelem ambiente interno

Se alguma chave ou senha ja foi commitada ou compartilhada, considere exposta e gere uma nova.

## Observacao

Este repositorio nao precisa conter dados reais das cameras. Para documentacao e backup do codigo, mantenha apenas os arquivos de exemplo com placeholders.
