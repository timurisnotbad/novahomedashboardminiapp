# Nova Home — server image: dashboard API + CRM + chats + bot (Python) and the
# WhatsApp QR bridge (Node). One container; everything that must survive an
# update lives in /data (a folder of the host, see docker-compose.yml).
FROM python:3.12-slim

# The whole product thinks in Tashkent local time (schedules, reports, "today").
ENV TZ=Asia/Tashkent PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg tzdata procps git \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Chromium for the «Цены» tab / the 09:00 Booking.com price report. Optional:
# build with --build-arg WITH_BROWSER=0 to skip (~450 MB, a few minutes).
ARG WITH_BROWSER=1
RUN if [ "$WITH_BROWSER" = "1" ]; then python -m playwright install --with-deps chromium || echo "chromium not installed — the prices tab stays off"; fi

COPY wa-bridge/package.json wa-bridge/package-lock.json wa-bridge/
RUN cd wa-bridge && npm ci --omit=dev
COPY . .

ENV DB_PATH=/data/nova_dashboard.db INBOX_MEDIA_DIR=/data/inbox_media NOVA_CLOUD=1
EXPOSE 8000
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
CMD ["/entrypoint.sh"]
