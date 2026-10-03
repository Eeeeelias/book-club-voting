FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 DB_PATH=/data/books.db
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py index.html ./
RUN mkdir -p /data
VOLUME /data
EXPOSE 5000
# One worker: SQLite + simple app; threads handle concurrent requests
CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "1", "--threads", "4", "app:app"]
