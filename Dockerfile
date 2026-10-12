# effective-scale-OS — minimal runtime image (stdlib-only: no pip installs, no OS deps)
FROM python:3.11-slim AS base
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/

# offline-friendly: no package downloads at build time
ENV PYTHONPATH=/app/src
EXPOSE 8080
VOLUME ["/data"]
# readiness is end-to-end (kernel can commit), liveness stays up through store death
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/v1/health/ready', timeout=2).status==200 else 1)"
ENTRYPOINT ["python", "-m", "effective_scale"]
CMD ["--store", "/data/effective_scale.db", "--listen", "0.0.0.0:8080", "--demo"]
