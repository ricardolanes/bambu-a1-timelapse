# bambu-a1-timelapse

Timelapse em vídeo das impressões de uma Bambu Lab A1, usando um celular
Android parado (rodando o app [IP Webcam](https://play.google.com/store/apps/details?id=com.pas.webcam))
como câmera. A gravação inicia e para sozinha, disparada pelo estado da
impressora lido via MQTT local — sem precisar apertar nada no celular.

Projeto irmão: [bambu-a1-monitor](../bambu-a1-monitor) (leitura de HMS/telemetria).

## Como funciona

1. Um script Python roda via cron (a cada ~1 minuto) e lê o `gcode_state`
   atual da impressora via MQTT local (com um comando `pushall` pra forçar
   um dump completo do estado, já que o MQTT da Bambu só manda deltas por
   padrão).
2. Quando o estado vira `RUNNING` pela primeira vez, o script chama
   `POST /startvideo` no celular (via HTTP, IP Webcam). A resposta já
   devolve o nome exato do arquivo que vai ser gravado.
3. Quando o estado sai de `RUNNING`, o script chama `POST /stopvideo`,
   espera o arquivo aparecer no servidor FTP do celular, baixa ele,
   mede a duração real com `ffprobe` e escolhe automaticamente um fator
   de aceleração (mais rápido pra impressões mais longas — ver tabela
   abaixo), gera o timelapse final em 1080p com `ffmpeg`, e só **depois**
   de confirmar que o arquivo final foi salvo com sucesso, apaga o vídeo
   do celular (nunca apaga nada se o ffmpeg falhar).
4. O vídeo bruto baixado é **arquivado permanentemente** (não é
   temporário) — só o espaço do celular é liberado a cada ciclo.

Cada execução do cron é independente (sem processo residente); o
progresso entre uma transição de estado e outra fica salvo num arquivo
de estado em disco (`state_path` no config).

### Aceleração automática

| Duração do vídeo bruto | Aceleração |
|---|---|
| < 30 min | 10x |
| < 60 min | 15x |
| < 120 min | 30x |
| < 180 min | 60x |
| ≥ 180 min | 120x |

## Requisitos

- Impressora Bambu Lab com MQTT local habilitado (modo LAN), código de
  acesso e número de série (Configurações > Dispositivo, na tela da
  impressora).
- Celular Android com:
  - [IP Webcam](https://play.google.com/store/apps/details?id=com.pas.webcam)
    instalado, com "iniciar no boot" e "rodar em segundo plano"
    ativados, e a otimização de bateria desativada pro app.
  - Um servidor FTP rodando (qualquer app de FTP server), expondo a
    mesma pasta onde o IP Webcam salva os vídeos.
  - IP fixo na rede (reserva DHCP no roteador).
- Um host na mesma rede local pra rodar o script (roda bem em qualquer
  Linux com Python 3 — testado tanto em SBC ARM quanto em x86).
- `ffmpeg` (inclui `ffprobe`) instalado nesse host.

**Recomendação de gravação:** MKV em vez de MP4 no app (mais resiliente
a interrupção no meio de gravações longas), 720p, bitrate ~8Mbps —
o resultado final já sai em 1080p de qualquer forma, então gravar em
resolução/bitrate mais baixo no celular economiza espaço sem perda
perceptível no timelapse final.

## Instalação

```bash
git clone <este-repo>
cd bambu-a1-timelapse
pip3 install -r requirements.txt --break-system-packages
sudo apt install ffmpeg -y

cp config.json.example config.json
nano config.json   # preenche com seus dados
```

Testa rodando manualmente uma vez, de preferência durante uma
impressão em andamento:

```bash
python3 timelapse_a1.py
```

### Colocando no cron

```bash
crontab -e
```

```
* * * * * /usr/bin/flock -n /tmp/timelapse-a1.lock /usr/bin/python3 /caminho/completo/timelapse_a1.py >> /var/log/timelapse-a1.log 2>&1
```

O `flock -n` é importante: garante que só uma execução roda por vez.
Sem ele, uma execução que demore mais que 1 minuto (comum ao baixar e
processar vídeos grandes) pode se sobrepor com a próxima, causando
downloads corrompidos e disputa de conexões no FTP do celular.

## Configuração (`config.json`)

Ver [`config.json.example`](config.json.example) pros campos
disponíveis. Nunca versiona o `config.json` de verdade — ele tem
credenciais (código de acesso da impressora, senha do FTP).

## Limitações conhecidas

- O nome/pasta onde o IP Webcam salva vídeo pode variar por versão do
  app e por restrições de armazenamento do Android — confere
  manualmente antes de configurar `ftp_dir`.
- Rodar num host fraco (ex: SBC ARM de baixo custo) que também presta
  outro serviço crítico (ex: DNS local) pode causar contenção de CPU
  durante o `ffmpeg` — prefira um host dedicado ou mais robusto pra
  essa etapa se isso for um problema.
