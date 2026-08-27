FROM python:3.13-slim

LABEL org.opencontainers.image.title="Museletter" \
      org.opencontainers.image.description="Headless, agent-first newsletter engine. One container, one SQLite database, and your own email provider: Amazon SES or Cloudflare Email Service." \
      org.opencontainers.image.source="https://github.com/sanketsaurav/museletter"

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV MUSELETTER_DB_PATH=/data/museletter.db
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["museletter", "serve", "--host", "0.0.0.0", "--port", "8000"]
