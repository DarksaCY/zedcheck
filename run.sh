#!/usr/bin/env bash
# Запуск веб-панели диагностики ZED. Открыть: http://127.0.0.1:8420
cd "$(dirname "$0")"
exec .venv/bin/python app.py
