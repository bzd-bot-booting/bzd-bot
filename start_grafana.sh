#!/bin/bash

set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

echo "=========================================="
echo "       BZHD BOT — ЗАПУСК GRAFANA"
echo "=========================================="

# Проверка sudo
if ! command -v sudo >/dev/null 2>&1; then
    echo "[ERROR] Не найдена утилита sudo."
    exit 1
fi

# Проверка Docker
if ! command -v docker >/dev/null 2>&1; then
    echo "[INFO] Docker не установлен. Устанавливаем..."
    sudo apt-get update
    sudo apt-get install -y docker.io
fi

# Проверка Docker Compose
if ! sudo docker compose version >/dev/null 2>&1; then
    echo "[INFO] Docker Compose не установлен. Устанавливаем..."
    sudo apt-get update

    if apt-cache show docker-compose-v2 >/dev/null 2>&1; then
        sudo apt-get install -y docker-compose-v2
    elif apt-cache show docker-compose-plugin >/dev/null 2>&1; then
        sudo apt-get install -y docker-compose-plugin
    else
        echo "[ERROR] Не удалось найти пакет Docker Compose v2."
        exit 1
    fi
fi

# Проверка конфигурации проекта
if [[ ! -f "$PROJECT_DIR/docker-compose.yml" && \
      ! -f "$PROJECT_DIR/compose.yml" && \
      ! -f "$PROJECT_DIR/compose.yaml" ]]; then
    echo "[ERROR] Не найден файл Docker Compose."
    exit 1
fi

# Запуск Docker
echo
echo "[INFO] Запускаем Docker..."
sudo systemctl enable --now docker

# Проверка конфигурации Compose
echo
echo "[INFO] Проверяем конфигурацию..."
sudo docker compose config -q

# Проверка наличия сервиса Grafana
if ! sudo docker compose config --services | grep -qx grafana; then
    echo "[ERROR] Сервис grafana не найден в Docker Compose."
    exit 1
fi

# Сборка и запуск Grafana
echo
echo "[INFO] Собираем и запускаем Grafana..."
sudo docker compose up -d --build grafana

# Проверка состояния
echo
echo "=========================================="
echo "          СОСТОЯНИЕ GRAFANA"
echo "=========================================="

sudo docker compose ps grafana

echo
echo "=========================================="
echo "          ПОСЛЕДНИЕ ЛОГИ"
echo "=========================================="

sudo docker compose logs --tail=40 grafana

echo
echo "=========================================="
echo "[OK] Команда запуска Grafana выполнена."
echo "Адрес: http://localhost:3000"
echo "=========================================="
