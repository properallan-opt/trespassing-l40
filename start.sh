#!/usr/bin/env bash
set -e

CONF=$(
    grep -ril \
        --include='*.conf' \
        '\[program:appTrespassing' \
        /etc/supervisor/conf.d 2>/dev/null \
    | head -n1
)

if [ -z "$CONF" ]; then
    echo "[START] ERRO: configuração do supervisord do trespassing não encontrada."
    exit 1
fi

if pgrep -f '/usr/bin/supervisord' >/dev/null; then
    echo "[START] supervisord já está rodando:"
    pgrep -af '/usr/bin/supervisord'
    exit 0
fi

rm -f /var/run/supervisord.pid 2>/dev/null || true

echo "[START] Usando configuração:"
echo "        $CONF"

nohup /usr/bin/supervisord -c "$CONF" \
    >/tmp/trespassing-supervisord.log 2>&1 &

sleep 2

echo "[START] Processos:"
pgrep -af 'supervisord|main_trespassing|main_oclusao' || true
