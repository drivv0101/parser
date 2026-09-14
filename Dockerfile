FROM python:3.12-slim

# lxml из requirements ставится готовым колесом, компилятор не нужен
WORKDIR /srv/app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    # База — на подключаемом томе, чтобы переживать пересборку контейнера
    STROY_DB_PATH=/srv/data/prices.db \
    # В контейнере логи идут в stdout (docker logs), файл не нужен
    STROY_LOG_TO_FILE=0

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

RUN mkdir -p /srv/data && useradd --system --home /srv --shell /usr/sbin/nologin stroy \
    && chown -R stroy:stroy /srv
USER stroy

VOLUME ["/srv/data"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

# Один воркер: внутри него живёт планировщик обхода, при нескольких он бы размножился.
# Заголовки прокси не доверяем намеренно: /api/refresh без токена пускает только
# localhost, и подделанный X-Forwarded-For не должен это обходить.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
