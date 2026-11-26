# BZHD Bot — серверное развёртывание

Telegram-бот для поиска железнодорожных билетов БЖД, мониторинга свободных мест и автоматизации бронирования.

Серверная конфигурация рассчитана на Docker Compose **ровно с двумя сервисами**:

- `bot` — приложение Telegram-бота;
- `postgres` — база данных PostgreSQL.

Grafana, Loki, Promtail и другие внешние системы мониторинга не используются. Порты на хост не публикуются. Бот сохраняет исходящие подключения к Telegram и `pass.rw.by`, необходимые для работы.

## Состав и хранение данных

| Компонент | Назначение | Хранение |
|---|---|---|
| `bot` | Telegram, поиск, мониторинг, оформление | Образ Docker |
| `postgres` | Пользователи, мониторинги, задачи бронирования | Docker volume `postgres_data` |
| Журналы бота | JSON-журнал приложения | Docker volume `bot_logs` |
| `data/` | Локальные файлы приложения | Каталог на сервере |

PostgreSQL доступен только внутри приватной сети Docker Compose. В `docker-compose.yml` нет секций `ports` и нет опубликованных портов у обоих сервисов.

Бот должен иметь исходящий доступ в интернет. Не задавайте сети Docker параметр `internal: true`: он заблокирует необходимый исходящий трафик.

## Требования

- Linux-сервер;
- Docker Engine;
- Docker Compose Plugin (`docker compose`);
- Git;
- исходящий доступ к GitHub, Telegram и сайту `pass.rw.by`.

## 1. Получение проекта

Можно развернуть проект одним из двух способов.

**Вариант А — Git-клон (рекомендуется):**

```bash
sudo mkdir -p /opt
sudo git clone https://github.com/bzd-bot-booting/bzd-bot.git /opt/bzhd-bot
sudo chown -R "$USER":"$USER" /opt/bzhd-bot
cd /opt/bzhd-bot
```

**Вариант Б — из подготовленного ZIP-архива:**

Передай архив на сервер и распакуй его непосредственно в `/opt/bzhd-bot`, чтобы `docker-compose.yml` находился по пути `/opt/bzhd-bot/docker-compose.yml`:

```bash
sudo mkdir -p /opt/bzhd-bot
sudo unzip bzhd_bot_server.zip -d /opt/bzhd-bot
sudo chown -R "$USER":"$USER" /opt/bzhd-bot
cd /opt/bzhd-bot
```

Обновление приложения выполняется вручную через Docker Hub. Серверу не требуется получать исходный код из GitHub или запускать systemd updater.

## 2. Настройка `.env`

Создайте конфигурацию:

```bash
cp .env.example .env
nano .env
```

Обязательные параметры:

| Переменная | Назначение |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота |
| `ADMIN_CHAT_IDS` | Telegram ID администраторов через запятую |
| `POSTGRES_DB` | Имя базы данных |
| `POSTGRES_USER` | Пользователь PostgreSQL |
| `POSTGRES_PASSWORD` | Пароль PostgreSQL |
| `DATABASE_URL` | Строка подключения бота к сервису `postgres` |
| `BOOKING_ENCRYPTION_KEY` | Ключ Fernet для шифрования данных бронирования |

Сгенерируйте URL-безопасный пароль PostgreSQL:

```bash
openssl rand -hex 32
```

Используйте одно и то же значение в `POSTGRES_PASSWORD` и в пароле внутри `DATABASE_URL`. В примере пароль состоит из шестнадцатеричных символов, поэтому его не нужно дополнительно кодировать для URL.

Строка подключения должна иметь вид:

```text
DATABASE_URL=postgresql+asyncpg://bzhd_bot:ВАШ_ПАРОЛЬ@postgres:5432/bzhd_bot
```

Сгенерируйте ключ шифрования:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Вставьте результат в `BOOKING_ENCRYPTION_KEY`.

**Важно:** после начала работы бота не меняйте `BOOKING_ENCRYPTION_KEY` без отдельной процедуры миграции зашифрованных данных. Старые записи бронирования зашифрованы исходным ключом.

Укажите Telegram ID администратора в `ADMIN_CHAT_IDS`, например:

```text
ADMIN_CHAT_IDS=123456789
```

Несколько ID указываются через запятую. Значение должно быть числовым Telegram ID пользователя, который будет нажимать административные кнопки.

Закройте доступ к конфигурации:

```bash
chmod 600 .env
mkdir -p data
chmod 750 data
```

Не отправляйте `.env` в Git и не публикуйте его содержимое.

## 3. Проверка Compose и запуск

Убедитесь, что Compose содержит только два сервиса:

```bash
docker compose config --services
```

Ожидаемый вывод:

```text
postgres
bot
```

Проверьте конфигурацию и соберите образ:

```bash
docker compose config --quiet
docker compose build bot
```

Запустите сервисы:

```bash
docker compose up -d
```

Проверьте состояние:

```bash
docker compose ps
docker compose logs --tail=150 bot
```

При запуске приложение применяет миграции Alembic, проверяет соединение с PostgreSQL и восстанавливает активные мониторинги из базы данных.

## 4. Административные журналы в Telegram

Если Telegram ID администратора указан в `ADMIN_CHAT_IDS`, в главном меню появятся две дополнительные кнопки:

- **📄 Логи за 24 часа** — файл со всеми доступными записями журнала за последние 24 часа;
- **⚠️ Ошибки за 24 часа** — отдельный файл только с событиями уровней `ERROR` и `CRITICAL`.

Файлы отправляются непосредственно в Telegram. При подготовке выгрузки:
- читается активный журнал и ротированные файлы;
- выбираются записи за последние 24 часа;
- чувствительные значения маскируются;
- технические идентификаторы пользователей и задач не включаются;
- размер файла ограничивается безопасным пределом.

Журнал приложения хранится в `/var/log/bzhd_bot/app.json.log` внутри контейнера и в Docker volume `bot_logs`. Ротация настроена на 20 МБ с семью резервными файлами.

## 5. Ручное обновление через Docker Hub

Образ приложения публикуется в Docker Hub: `bzhdbot/bzhd-bot`.

### 5.1. Сборка и публикация на компьютере разработчика

В каталоге проекта выполни:

    docker login -u bzhdbot
    docker build --pull -t bzhdbot/bzhd-bot:latest .
    docker push bzhdbot/bzhd-bot:latest

При входе используй пароль или Personal Access Token Docker Hub. Не записывай учётные данные в файлы проекта.

### 5.2. Обновление на сервере

В каталоге с `docker-compose.yml` и `.env` выполни:

    docker compose pull bot
    docker compose up -d --no-deps --force-recreate bot

Обновляется только контейнер бота. Сервис PostgreSQL и его volume не затрагиваются.

### 5.3. Проверка

    docker compose ps
    docker compose logs --tail=100 bot

Убедись, что контейнер бота работает и в логах нет ошибок.

## 6. Полезные команды

```bash
# Состояние двух сервисов
docker compose ps

# Логи бота
docker compose logs -f --tail=200 bot

# Логи PostgreSQL
docker compose logs -f --tail=100 postgres

# Перезапуск только бота
docker compose restart bot

# Остановка сервисов без удаления томов
docker compose stop

# Повторная сборка бота вручную
docker compose build bot
docker compose up -d --no-deps --force-recreate bot
```

Не используйте `docker compose down -v`: ключ `-v` удаляет тома, в том числе данные PostgreSQL.

## 7. Переменные приложения

Дополнительные настройки:

| Переменная | Значение по умолчанию | Назначение |
|---|---:|---|
| `CHECK_INTERVAL` | `30` | Интервал проверок мониторинга, секунд |
| `PLAYWRIGHT_HEADLESS` | `true` | Безголовый режим браузера |
| `PAGE_TIMEOUT_MS` | `90000` | Тайм-аут операций браузера |
| `PAGE_WAIT_MS` | `2000` | Дополнительное ожидание страницы |
| `MAX_TICKETS_PER_REQUEST` | `9` | Максимум билетов в одном запросе |
| `HEALTH_INTERVAL_SECONDS` | `30` | Частота обновления health-файла |
| `LOG_LEVEL` | `INFO` | Уровень журналирования |
| `TZ` | `Europe/Vilnius` | Часовой пояс контейнеров |

SMTP-переменные в `.env.example` необязательны. Если SMTP не настроен, отправка почтовых отчётов не используется.

## Безопасность

- Не публикуйте порты бота или PostgreSQL на хост.
- Не монтируйте Docker socket внутрь контейнера бота.
- Ограничьте права доступа к `.env`.
- Не добавляйте `.env`, `data/` и журналы в Git.
- Сохраняйте `postgres_data` и `bot_logs` при обновлении образа.
- Не меняйте ключ шифрования без миграции уже зашифрованных данных.
- Не запускайте `docker compose down -v` на рабочем сервере.
