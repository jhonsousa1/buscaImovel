#!/usr/bin/env bash
# Instala o monitor de imoveis SQS 308 como um systemd timer horario.
# Uso:  sudo bash install.sh
# Suporta Amazon Linux 2023 e Ubuntu/Debian.
set -euo pipefail

APP_DIR=/opt/imoveis-monitor
DATA_DIR=/var/lib/imoveis-monitor
ENV_FILE=/etc/imoveis-monitor.env
SVC_USER=imoveis
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ $EUID -eq 0 ]] || { echo "Rode como root: sudo bash install.sh"; exit 1; }
[[ -f "$SRC_DIR/monitor.py" ]] || { echo "monitor.py nao encontrado em $SRC_DIR"; exit 1; }

# ---------------------------------------------------------------- distro
if [[ -f /etc/os-release ]]; then . /etc/os-release; else ID=desconhecido; fi
echo ">> Distro detectada: ${PRETTY_NAME:-$ID}"

case "$ID" in
  amzn|rhel|centos|fedora)
    PKG=dnf
    dnf install -y python3 python3-pip >/dev/null
    # Bibliotecas que o Chromium headless precisa (o --with-deps do Playwright
    # nao cobre Amazon Linux).
    dnf install -y nss nspr atk at-spi2-atk at-spi2-core cups-libs libdrm \
        libxkbcommon libXcomposite libXdamage libXfixes libXrandr mesa-libgbm \
        pango cairo alsa-lib libxshmfence >/dev/null
    ;;
  ubuntu|debian)
    PKG=apt
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq python3 python3-pip python3-venv >/dev/null
    ;;
  *)
    echo "!! Distro nao reconhecida. Instale python3/python3-venv manualmente e rode de novo."
    exit 1
    ;;
esac

# ---------------------------------------------------------------- swap
# Chromium headless em instancia de 1 GB (t2/t3.micro) morre por falta de RAM.
MEM_MB=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
SWAP_MB=$(awk '/SwapTotal/{print int($2/1024)}' /proc/meminfo)
if (( MEM_MB < 2048 && SWAP_MB < 512 )); then
  echo ">> RAM de ${MEM_MB}MB sem swap — criando swapfile de 2GB em /swapfile"
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# ---------------------------------------------------------------- usuario e dirs
id -u "$SVC_USER" &>/dev/null || useradd --system --home-dir "$APP_DIR" --shell /sbin/nologin "$SVC_USER"
mkdir -p "$APP_DIR" "$DATA_DIR"
install -m 644 -o "$SVC_USER" -g "$SVC_USER" "$SRC_DIR/monitor.py" "$APP_DIR/monitor.py"

# Semeia o historico com o CSV existente (evita re-notificar o que ja e conhecido).
if [[ -f "$SRC_DIR/imoveis_historico.csv" && ! -f "$DATA_DIR/imoveis_historico.csv" ]]; then
  install -m 644 "$SRC_DIR/imoveis_historico.csv" "$DATA_DIR/imoveis_historico.csv"
  echo ">> Historico inicial copiado ($(($(wc -l < "$DATA_DIR/imoveis_historico.csv") - 1)) imoveis)."
fi
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR" "$DATA_DIR"

# ---------------------------------------------------------------- venv + playwright
echo ">> Criando venv e instalando dependencias (pode levar alguns minutos)..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q playwright beautifulsoup4

export PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/browsers"
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"
if [[ "$PKG" == "apt" ]]; then
  "$APP_DIR/venv/bin/playwright" install --with-deps chromium
else
  "$APP_DIR/venv/bin/playwright" install chromium
fi
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR"

# ---------------------------------------------------------------- env file
if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<'EOF'
# Senha de app do Gmail (16 caracteres, SEM espacos).
GMAIL_PASSWORD=COLOQUE_A_SENHA_DE_APP_AQUI
GMAIL_USER=jhonathan.sousa1@gmail.com
NOTIFY_EMAIL=jhonathan.sousa1@gmail.com
CC_EMAIL=mestter21@gmail.com
URL_ALVO=https://www.dfimoveis.com.br/aluguel/df/todos/imoveis?palavrachave=sqs-308&vagasdegaragem=1
DATA_DIR=/var/lib/imoveis-monitor
FALHAS_PARA_ALERTA=3
EOF
  echo ">> Criado $ENV_FILE — edite e coloque a senha de app do Gmail."
else
  echo ">> $ENV_FILE ja existe, mantido como esta."
fi
chown root:"$SVC_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

# ---------------------------------------------------------------- systemd
cat > /etc/systemd/system/imoveis-monitor.service <<EOF
[Unit]
Description=Monitor de imoveis SQS 308 (DFImoveis)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$SVC_USER
Group=$SVC_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
Environment=PLAYWRIGHT_BROWSERS_PATH=$APP_DIR/browsers
Environment=HOME=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/monitor.py
TimeoutStartSec=600
Nice=10
EOF

cat > /etc/systemd/system/imoveis-monitor.timer <<'EOF'
[Unit]
Description=Executa o monitor de imoveis SQS 308 a cada hora

[Timer]
OnCalendar=hourly
RandomizedDelaySec=300
Persistent=true
Unit=imoveis-monitor.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now imoveis-monitor.timer

echo
echo "======================================================================"
echo " Instalado."
echo
echo " 1) Edite a senha de app do Gmail:   sudo nano $ENV_FILE"
echo " 2) Teste agora:                     sudo systemctl start imoveis-monitor"
echo " 3) Veja o resultado:                sudo journalctl -u imoveis-monitor -n 50 --no-pager"
echo " 4) Proxima execucao:                systemctl list-timers imoveis-monitor.timer"
echo
echo " Historico: $DATA_DIR/imoveis_historico.csv"
echo "======================================================================"
