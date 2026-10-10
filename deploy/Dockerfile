# Nova Home — server image: dashboard API + CRM + chats (Python) and the
# WhatsApp QR bridge (Node). One container, one volume for data.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY wa-bridge/package.json wa-bridge/package-lock.json wa-bridge/
RUN cd wa-bridge && npm ci --omit=dev
COPY . .

# data that must survive updates: SQLite DB, chat media, WhatsApp/Telegram sessions, logs
ENV DB_PATH=/data/nova_dashboard.db INBOX_MEDIA_DIR=/data/inbox_media PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8000
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
CMD ["/entrypoint.sh"]
