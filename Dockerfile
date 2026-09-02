# effective-scale-OS — minimal runtime image (stdlib-only: no pip installs, no OS deps)
FROM python:3.11-slim AS base
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/

# offline-friendly: no package downloads at build time
ENV PYTHONPATH=/app/src
EXPOSE 8080
VOLUME ["/data"]
ENTRYPOINT ["python", "-m", "effective_scale"]
CMD ["--store", "/data/effective_scale.db", "--listen", "0.0.0.0:8080", "--demo"]
