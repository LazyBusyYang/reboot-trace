FROM node:24-alpine AS builder
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts && npm rebuild esbuild
COPY frontend/ ./
RUN npm run build

FROM nginx:1.27-alpine
LABEL org.opencontainers.image.title="Reboot Trace Frontend" \
      org.opencontainers.image.description="Multi-backend reboot evidence Vue dashboard" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/LazyBusyYang/reboot-trace"
COPY frontend/nginx.conf /etc/nginx/nginx.conf
COPY --from=builder /app/dist /usr/share/nginx/html
RUN chown -R nginx:nginx /usr/share/nginx/html /var/cache/nginx /var/run
USER nginx
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 CMD wget -q -O /dev/null http://127.0.0.1:8080/
