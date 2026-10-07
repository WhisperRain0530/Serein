FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY web/package*.json ./
RUN npm ci --ignore-scripts
COPY web ./
RUN npm run build

FROM python:3.13-slim-bookworm
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libstdc++6 tini \
    && rm -rf /var/lib/apt/lists/*
COPY --from=web /usr/local/bin/node /usr/local/bin/node
COPY pyproject.toml ./
RUN python -c "import tomllib; p=tomllib.load(open('pyproject.toml','rb')); r=p['build-system']['requires']+['wheel']+p['project']['dependencies']; r += [d for e in ('http','embedding','mcp','background') for d in p['project']['optional-dependencies'][e]]; print('\n'.join(dict.fromkeys(r)))" > /tmp/serein-requirements.txt \
    && pip install --no-cache-dir -r /tmp/serein-requirements.txt \
    && rm /tmp/serein-requirements.txt
COPY README.md LICENSE config.example.toml ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps --no-build-isolation . && pip check
COPY --from=web /web ./web
COPY scripts/zeabur_runtime.py ./scripts/zeabur_runtime.py
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=5s --start-period=60s --retries=3 \
    CMD python /app/scripts/zeabur_runtime.py healthcheck
ENTRYPOINT ["/usr/bin/tini", "--", "python", "/app/scripts/zeabur_runtime.py"]
CMD ["start"]
