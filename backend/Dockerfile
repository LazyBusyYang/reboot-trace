FROM python:3.11-slim AS builder
WORKDIR /build
COPY backend/requirements.lock ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.lock

FROM python:3.11-slim
LABEL org.opencontainers.image.title="Reboot Trace Backend" \
      org.opencontainers.image.description="Bounded Linux reboot evidence collector and JSON API" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/LazyBusyYang/reboot-trace"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --system reboot-trace && useradd --system --gid reboot-trace --home /nonexistent reboot-trace
COPY --from=builder /install /usr/local
WORKDIR /app
COPY backend/reboot_trace ./reboot_trace
RUN mkdir -p /var/lib/reboot-trace && chown reboot-trace:reboot-trace /var/lib/reboot-trace
USER reboot-trace
EXPOSE 8080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready',timeout=2)"
ENTRYPOINT ["uvicorn","reboot_trace.main:app","--host","0.0.0.0","--port","8080"]
