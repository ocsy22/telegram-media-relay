#!/usr/bin/env bash
set -euo pipefail

if (( EUID != 0 )); then
  echo '请在 Ubuntu 的 root 终端运行。' >&2
  exit 1
fi

address=${1:-}
if [[ -z "$address" ]]; then
  read -r -p '请输入 VPS 公网 IPv4 或主机名: ' address </dev/tty
fi
if [[ ! "$address" =~ ^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$ ]]; then
  echo '请输入 IPv4 或主机名。' >&2
  exit 1
fi

if ! command -v python3 >/dev/null || ! command -v curl >/dev/null; then
  apt-get update
  apt-get install -y ca-certificates curl python3
fi

work=$(mktemp -d)
trap 'rm -f -- "$work/app.py"; rmdir -- "$work"' EXIT
ref=${RELAY_REF:-main}
curl -fsSL --retry 3 "https://raw.githubusercontent.com/ocsy22/telegram-media-relay/$ref/app.py" -o "$work/app.py"
python3 -c 'import pathlib, sys; p=pathlib.Path(sys.argv[1]); compile(p.read_bytes(), str(p), "exec")' "$work/app.py"

install -d -m 0700 /etc/telegram-media-relay
config=/etc/telegram-media-relay/service.env
if [[ ! -f "$config" || "${2:-}" == --configure-admin ]]; then
  read -r -p '管理员用户名: ' admin_user </dev/tty
  if [[ ! "$admin_user" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]; then
    echo '用户名只接受字母、数字、点、下划线和连字符。' >&2
    exit 1
  fi
  read -rs -p '管理员密码（至少 8 字符）: ' password </dev/tty
  echo
  read -rs -p '再次输入管理员密码: ' confirm </dev/tty
  echo
  if [[ ${#password} -lt 8 || "$password" != "$confirm" ]]; then
    echo '密码太短或两次输入不一致。' >&2
    exit 1
  fi
  escaped=${password//\\/\\\\}
  escaped=${escaped//\"/\\\"}
  tmp_config=$(mktemp /etc/telegram-media-relay/.env.XXXXXX)
  printf 'ADMIN_USERNAME=%s\nADMIN_PASSWORD="%s"\n' "$admin_user" "$escaped" > "$tmp_config"
  mv -f -- "$tmp_config" "$config"
  unset password confirm escaped
fi

install -d -m 0755 /opt/telegram-media-relay
install -m 0644 "$work/app.py" /opt/telegram-media-relay/app.py
cat > /etc/systemd/system/telegram-media-relay.service <<'UNIT'
[Unit]
Description=Telegram Media Relay
Wants=network-online.target
After=network-online.target

[Service]
DynamicUser=yes
StateDirectory=telegram-media-relay
StateDirectoryMode=0700
EnvironmentFile=/etc/telegram-media-relay/service.env
Environment=DB_PATH=/var/lib/telegram-media-relay/relay.sqlite3
Environment=BIND_HOST=0.0.0.0
Environment=PORT=8766
ExecStart=/usr/bin/python3 /opt/telegram-media-relay/app.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable telegram-media-relay.service
systemctl restart telegram-media-relay.service
for attempt in {1..20}; do
  status=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 2 http://127.0.0.1:8766/ 2>/dev/null || true)
  if [[ "$status" == 401 ]] && systemctl is-active --quiet telegram-media-relay.service; then
    admin_user=$(sed -n 's/^ADMIN_USERNAME=//p' "$config")
    printf '\n管理地址 http://%s:8766/\n用户名 %s\n' "$address" "$admin_user"
    printf '请在云安全组和防火墙允许管理端访问 TCP 8766。HTTP 不加密用户名、密码或 Bot Token。\n'
    exit 0
  fi
  sleep 1
done
journalctl -u telegram-media-relay.service -n 20 --no-pager >&2
exit 1
