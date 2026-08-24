#!/usr/bin/env bash

echo "[STOP] Parando supervisord..."

pkill -TERM -f '/usr/bin/supervisord' 2>/dev/null || true

sleep 2

echo "[STOP] Limpando processos restantes..."

pkill -TERM -f 'main_trespassing.py' 2>/dev/null || true
pkill -TERM -f 'main_oclusao.py' 2>/dev/null || true
pkill -TERM -f 'main_trespassing.sh' 2>/dev/null || true
pkill -TERM -f 'main_oclusao.sh' 2>/dev/null || true

sleep 1

echo "[STOP] Estado atual:"
pgrep -af 'supervisord|main_trespassing|main_oclusao' || echo "Nenhum serviço rodando."
