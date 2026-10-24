#!/bin/bash
# Запускает сайт и Telegram-бота в одном контейнере: им нужна одна общая база SQLite,
# а диск на Render можно подключить только к одному сервису.
PORT="${PORT:-8000}"

# Адрес для бота: сначала WEBAPP_URL из env, иначе адрес, который Render выдаёт сам
export WEBAPP_URL="${WEBAPP_URL:-$RENDER_EXTERNAL_URL}"
WEBAPP_URL="${WEBAPP_URL%/}"

if [ -n "$BOT_TOKEN" ] && [ -n "$WEBAPP_URL" ]; then
  (
    # ждём, пока сайт создаст таблицы в базе
    until python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${PORT}/health', timeout=2)" 2>/dev/null; do
      sleep 1
    done
    while true; do
      echo "Запускаю бота, WEBAPP_URL=$WEBAPP_URL"
      python bot.py
      echo "Бот остановился, перезапуск через 5 секунд"
      sleep 5
    done
  ) &
else
  echo "БОТ НЕ ЗАПУЩЕН: задайте BOT_TOKEN и WEBAPP_URL в переменных окружения"
fi

exec uvicorn server.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers --forwarded-allow-ips="*"
