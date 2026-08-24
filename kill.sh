#!/usr/bin/env bash

echo "[KILL] Matando supervisord e processos do trespassing..."

pkill -9 -f '/usr/bin/supervisord' 2>/dev/null || true

pkill -9 -f 'main_trespassing.py' 2>/dev/null || true
pkill -9 -f 'main_oclusao.py' 2>/dev/null || true
pkill -9 -f 'main_trespassing.sh' 2>/dev/null || true
pkill -9 -f 'main_oclusao.sh' 2>/dev/null || true

rm -f /var/run/supervisord.pid 2>/dev/null || true

echo "[KILL] Estado atual:"
pgrep -af 'supervisord|main_trespassing|main_oclusao' || echo "Tudo morto."
