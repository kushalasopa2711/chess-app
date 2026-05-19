# WebSocket game rooms live in process memory — use WEB_CONCURRENCY=1 in production
# unless you add a cross-worker pub/sub layer; otherwise live moves miss other players.
web: gunicorn -k uvicorn.workers.UvicornWorker main:app --bind 0.0.0.0:$PORT --workers ${WEB_CONCURRENCY:-1} --timeout 60 --graceful-timeout 30 --keep-alive 30
