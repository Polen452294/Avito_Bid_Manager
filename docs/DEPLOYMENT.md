# Развёртывание на Linux VPS

1. Скопируйте проект на VPS с Docker Engine и Compose. Используйте локальный persistent volume для SQLite.
2. Создайте `.env` из `.env.example` и ограничьте его права до 0600. Для первого запуска оставьте DRY_RUN, пустые Avito credentials и выключенный сбор сетевой статистики.
3. Выполните команды ниже из корня проекта.

```sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose exec manager python -m app.main health
docker compose exec manager python -m app.cli readiness
docker compose logs --tail=100 manager
```

Образ запускается с UID 10001 и umask 077. Docker volume сохраняет базу и резервные копии. Для существующего или подключённого вручную тома проверьте доступ UID 10001. `.env` и данные исключены из контекста образа. Официальные OpenAPI-файлы при подключении аккаунта передавайте в контейнер отдельным read-only mount, используя пути внутри контейнера в `AVITO_OFFICIAL_SPEC_PATHS`.

Сервис автоматически перезапускается по политике `unless-stopped`. Healthcheck проверяет живой экземпляр и heartbeat, статус unhealthy Docker показывает в `ps`. Контейнер пишет ротируемые логи, до трёх файлов по 10 МБ. Для штатной остановки используйте `docker compose stop`. `docker compose down -v` удаляет том.

Ежедневные копии создаются в 03:00 по TIMEZONE. Проверенные копии перед изменением схемы также сохраняются в `/app/data/backups`. BACKUP_RETENTION ограничивает каждый вид копий отдельно. На перенос на отдельный носитель ориентируйтесь по [инструкции восстановления](INCIDENT_RECOVERY.md).

На Windows команды запускайте из корня проекта, где находится `alembic.ini`. Для проверки чистого запуска задайте `DATABASE_URL` на новый файл и выполните `python -m app.main serve --migrate`.

Compose прошёл статическую валидацию на Windows. Сборка и запуск контейнера на Linux ожидают проверки с доступным Docker Engine.
