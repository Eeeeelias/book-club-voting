FROM python:3.12-slim-bookworm
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DB_PATH=/data/books.db
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# unprivileged user; /data is where the SQLite database lives
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin app && mkdir -p /data && chown app:app /data
COPY app.py index.html ./
USER app
VOLUME /data
EXPOSE 5000
# One worker: SQLite + in-memory rate limiter; threads handle concurrent requests
CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "1", "--threads", "4", "--timeout", "30", "app:app"]
