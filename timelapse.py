#!/usr/bin/env python3
"""
Timelapse em vídeo da Bambu Lab A1 usando um celular Android (com o app
"IP Webcam") como câmera, disparado automaticamente pelo estado da
impressão lido via MQTT local.

Fluxo:
  - print começa (gcode_state RUNNING) -> chama /startvideo no celular
  - print termina (qualquer estado != RUNNING) -> chama /stopvideo,
    espera o arquivo aparecer no FTP do celular, baixa pra pasta de
    brutos (arquivada permanentemente, ver local_tmp no config), acelera
    com ffmpeg pro NAS, e só ENTÃO apaga o vídeo do celular (garante
    que nunca perde nada se o ffmpeg falhar).

Feito pra rodar via cron a cada ~1 minuto, no mesmo Orange Pi que já
roda o monitor de HMS. Cada execução é independente (sem estado em
memória) -- o progresso fica salvo em STATE_PATH (ver config.json).

Requisitos:
    pip3 install paho-mqtt requests --break-system-packages
    apt install ffmpeg -y

Configuração: ver config.json.example (copia pra config.json e edita
com seus dados -- NÃO versiona o config.json no repositório público).
"""

import json
import ssl
import time
import subprocess
from pathlib import Path
from ftplib import FTP

import paho.mqtt.client as mqtt
import requests

# ---------- Configuração ----------
CONFIG_PATH = Path(__file__).parent / "config.json"
cfg = json.loads(CONFIG_PATH.read_text())

MQTT_HOST = cfg["mqtt_host"]                 # IP local da impressora
MQTT_PORT = cfg.get("mqtt_port", 8883)
MQTT_USER = cfg.get("mqtt_user", "bblp")
MQTT_PASS = cfg["mqtt_access_code"]          # código de acesso (tela da impressora)
MQTT_SERIAL = cfg["mqtt_serial"]             # número de série (Configurações > Dispositivo)

CELULAR_BASE = cfg["celular_http_base"].rstrip("/")  # ex: http://192.168.1.50:8080

FTP_HOST = cfg["ftp_host"]
FTP_PORT = cfg.get("ftp_port", 2221)
FTP_USER = cfg["ftp_user"]
FTP_PASS = cfg["ftp_pass"]
FTP_DIR = cfg.get("ftp_dir", "/IP Webcam")   # pasta onde o app salva os vídeos

LOCAL_TMP = Path(cfg.get("local_tmp", "/tmp/bambu-timelapse"))
NAS_DIR = Path(cfg.get("nas_dir", "/mnt/nas"))
ACELERACAO_FALLBACK = cfg.get("aceleracao", 30)  # só usado se o ffprobe não conseguir medir a duração

STATE_PATH = Path(cfg.get("state_path", "/var/lib/bambu-timelapse/estado.json"))

LOCAL_TMP.mkdir(parents=True, exist_ok=True)
STATE_PATH.parent.mkdir(parents=True, exist_ok=True)


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ---------- Estado persistido entre execuções do cron ----------
def le_estado():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"gravando": False, "nome_arquivo": None, "nome_job": None, "ignorada": False}


def salva_estado(estado):
    STATE_PATH.write_text(json.dumps(estado))


# ---------- MQTT: pega o estado atual da impressora ----------
def le_estado_impressora(timeout=5):
    """Conecta, escuta uma mensagem de report e desconecta -- não fica
    residente (compatível com execução via cron)."""
    resultado = {}

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
            if "print" in payload:
                resultado.update(payload["print"])
        except Exception:
            pass

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.tls_set(cert_reqs=ssl.CERT_NONE)   # certificado local autoassinado
    client.tls_insecure_set(True)
    client.on_message = on_message

    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=10)
        client.subscribe(f"device/{MQTT_SERIAL}/report")
        client.loop_start()

        # pede um dump completo do estado -- sem isso só chegam
        # atualizações incrementais e gcode_state pode nunca aparecer
        client.publish(
            f"device/{MQTT_SERIAL}/request",
            json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}),
        )

        inicio = time.time()
        while "gcode_state" not in resultado and time.time() - inicio < timeout:
            time.sleep(0.2)
    finally:
        client.loop_stop()
        client.disconnect()

    return resultado


# ---------- FTP: listar / baixar / apagar no celular ----------
def _ftp_conecta():
    ftp = FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=10)
    ftp.login(user=FTP_USER, passwd=FTP_PASS)
    ftp.cwd(FTP_DIR)
    return ftp


def apaga_do_ftp(nome):
    ftp = _ftp_conecta()
    ftp.delete(nome)
    ftp.quit()


def espera_e_baixa_do_ftp(nome, tentativas=10, intervalo=3):
    """Abre UMA única conexão FTP pra esperar o arquivo (nome exato
    devolvido pelo startvideo) aparecer e já baixar em seguida --
    evita reconectar a cada tentativa e estourar o limite de login
    do app de FTP no celular. Devolve o caminho local, ou None se o
    arquivo não apareceu dentro do prazo."""
    ftp = _ftp_conecta()
    try:
        for _ in range(tentativas):
            if nome in ftp.nlst():
                destino = LOCAL_TMP / nome
                with open(destino, "wb") as f:
                    ftp.retrbinary(f"RETR {nome}", f.write)
                return destino
            time.sleep(intervalo)
        return None
    finally:
        ftp.quit()


def duracao_video_segundos(caminho):
    """Usa ffprobe pra descobrir a duração real do vídeo bruto baixado."""
    resultado = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(caminho)],
        capture_output=True, text=True,
    )
    try:
        return float(resultado.stdout.strip())
    except ValueError:
        return None


def escolhe_aceleracao(duracao_segundos):
    """Fator de velocidade do timelapse final, escalado pela duração
    do vídeo bruto -- prints mais longos usam um fator maior, senão
    o timelapse final ficaria longo demais."""
    duracao_min = duracao_segundos / 60
    if duracao_min < 30:
        return 10
    elif duracao_min < 60:
        return 15
    elif duracao_min < 120:
        return 30
    elif duracao_min < 180:
        return 60
    else:
        return 120


# ---------- Fluxo principal ----------
def main():
    estado = le_estado()
    status = le_estado_impressora()

    if not status:
        log("Não consegui ler o estado da impressora nessa execução -- tento de novo no próximo cron.")
        return

    gcode_state = status.get("gcode_state")
    subtask_name = status.get("subtask_name", "impressao")
    ipcam = status.get("ipcam") or {}
    timelapse_valor = ipcam.get("timelapse")  # "enable" / "disable", vem do fatiador

    # --- início de impressão ---
    if gcode_state == "RUNNING" and not estado["gravando"] and not estado.get("ignorada"):
        if timelapse_valor == "disable":
            log(f"Timelapse desativado no fatiador pra essa impressão ({subtask_name}) -- não vou gravar.")
            salva_estado({"gravando": False, "nome_arquivo": None, "nome_job": None, "ignorada": True})
            return
        if timelapse_valor != "enable":
            log("Ainda não recebi o status do timelapse do fatiador -- aguardando próxima checagem.")
            return  # não marca nada -- tenta de novo no próximo tick

        log(f"Impressão iniciada ({subtask_name}). Iniciando gravação no celular.")
        requests.post(f"{CELULAR_BASE}/enabletorch", timeout=5)
        requests.post(f"{CELULAR_BASE}/focus", timeout=5)
        time.sleep(2)  # dá tempo do foco assentar antes de começar a gravar
        resposta = requests.post(f"{CELULAR_BASE}/startvideo?force=1", timeout=5)
        nome_arquivo = None
        try:
            nome_arquivo = resposta.json().get("fname")
        except Exception:
            pass
        if nome_arquivo:
            log(f"Celular confirmou gravação de '{nome_arquivo}'.")
        else:
            log("startvideo não devolveu o nome do arquivo -- confere se a gravação começou mesmo.")
        salva_estado({"gravando": True, "nome_arquivo": nome_arquivo, "nome_job": subtask_name, "ignorada": False})
        return

    # --- impressão sem gravação (timelapse desligado no fatiador) terminou -- reseta pra próxima ---
    if gcode_state != "RUNNING" and estado.get("ignorada"):
        salva_estado({"gravando": False, "nome_arquivo": None, "nome_job": None, "ignorada": False})
        return

    # --- fim de impressão ---
    if gcode_state != "RUNNING" and estado["gravando"]:
        log(f"Impressão terminou (estado: {gcode_state}). Parando gravação.")
        requests.post(f"{CELULAR_BASE}/stopvideo?force=1", timeout=5)
        requests.post(f"{CELULAR_BASE}/disabletorch", timeout=5)

        nome = estado.get("nome_arquivo")
        if not nome:
            log("Não tinha o nome do arquivo salvo (startvideo pode ter falhado) -- confere manualmente.")
            salva_estado({"gravando": False, "nome_arquivo": None, "nome_job": None, "ignorada": False})
            return

        log(f"Esperando '{nome}' ficar pronto e baixando do celular.")
        bruto = espera_e_baixa_do_ftp(nome)
        if not bruto:
            log(f"'{nome}' não apareceu no FTP do celular a tempo -- confere manualmente.")
            salva_estado({"gravando": False, "nome_arquivo": None, "nome_job": None, "ignorada": False})
            return

        duracao = duracao_video_segundos(bruto)
        if duracao:
            fator = escolhe_aceleracao(duracao)
            log(f"Vídeo bruto tem {duracao/60:.1f}min -- usando aceleração {fator}x.")
        else:
            fator = ACELERACAO_FALLBACK
            log(f"Não consegui medir a duração do vídeo -- usando aceleração padrão {fator}x.")

        nome_job = estado.get("nome_job") or "impressao"
        timestamp = time.strftime("%Y%m%d_%H%M")
        destino_final = NAS_DIR / f"{timestamp}_timelapse.mp4"

        log(f"Acelerando ({fator}x) e salvando em {destino_final}.")
        resultado = subprocess.run(
            ["ffmpeg", "-y", "-i", str(bruto),
             "-vf", f"setpts=PTS/{fator},scale=1920:1080:flags=lanczos,unsharp=5:5:0.8:5:5:0.0",
             "-an", str(destino_final)],
            capture_output=True,
        )

        sucesso = (resultado.returncode == 0
                   and destino_final.exists()
                   and destino_final.stat().st_size > 0)

        if sucesso:
            log(f"Timelapse salvo no NAS com sucesso ({nome_job}). Bruto arquivado em {bruto}. "
                "Apagando o vídeo do celular pra liberar espaço lá.")
            apaga_do_ftp(nome)
        else:
            log("ffmpeg falhou ou o arquivo final ficou vazio -- NÃO apaguei nada do celular. "
                "Vídeo bruto continua em " + str(bruto) + " pra investigar.")
            log(resultado.stderr.decode(errors="ignore")[-2000:])

        salva_estado({"gravando": False, "nome_arquivo": None, "nome_job": None, "ignorada": False})
        return

    # nenhuma transição relevante nessa execução -- não faz nada
    log(f"Sem mudança de estado relevante (gcode_state={gcode_state}, gravando={estado['gravando']}).")


if __name__ == "__main__":
    main()
