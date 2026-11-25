# Optional domestic mirror prefix & package registries (empty defaults keep exact upstream digests & registries)
ARG DOCKER_MIRROR_PREFIX=

# Stage 1: Build official Companion Studio React web console AND MCP UI static assets (H-2, H-4)
FROM ${DOCKER_MIRROR_PREFIX}node:22-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee AS web-builder
ARG NPM_REGISTRY=

WORKDIR /build/examples/showcase
COPY examples/showcase/package.json examples/showcase/package-lock.json ./
RUN if [ -n "${NPM_REGISTRY}" ]; then \
        npm ci --no-audit --no-fund --registry="${NPM_REGISTRY}"; \
    else \
        npm ci --no-audit --no-fund; \
    fi
COPY docs /build/docs
COPY examples/showcase/ ./
RUN npm run build

WORKDIR /build/ui
COPY ui/package.json ui/package-lock.json ./
RUN if [ -n "${NPM_REGISTRY}" ]; then \
        npm ci --no-audit --no-fund --registry="${NPM_REGISTRY}"; \
    else \
        npm ci --no-audit --no-fund; \
    fi
COPY ui/ ./
RUN mkdir -p /build/src/companion_engine/static && npm run build

# Stage 2: Lightweight reproducible Python 3.12 runtime for Anima Brain + Companion Studio + XiaoZhi Bridge (H-2)
FROM ${DOCKER_MIRROR_PREFIX}python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ARG PIP_INDEX_URL=
ARG PIP_TRUSTED_HOST=

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    ANIMA_BUILT_STATIC_DIR=/app/built_static

WORKDIR /app

# Layer 1: Install pinned runtime & build dependencies from pyproject.toml + constraints.txt (Py 3.12)
COPY pyproject.toml constraints.txt ./
RUN set -- --no-cache-dir -c constraints.txt; \
    if [ -n "${PIP_INDEX_URL}" ]; then set -- "$@" -i "${PIP_INDEX_URL}"; fi; \
    if [ -n "${PIP_TRUSTED_HOST}" ]; then set -- "$@" --trusted-host "${PIP_TRUSTED_HOST}"; fi; \
    python -c "import tomllib, pathlib; d=tomllib.loads(pathlib.Path('pyproject.toml').read_text(encoding='utf-8'))['project']; deps=list(d.get('dependencies',[]))+list(d.get('optional-dependencies',{}).get('http',[]))+list(d.get('optional-dependencies',{}).get('calls',[])); pathlib.Path('/tmp/reqs.txt').write_text('\n'.join(deps)+'\n', encoding='utf-8')" \
    && pip install "$@" hatchling -r /tmp/reqs.txt \
    && rm -f /tmp/reqs.txt

# Layer 2: Copy source + built static UI assets and install non-editable wheel (no -e, offline)
COPY README.md README.zh-CN.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY src ./src
COPY --from=web-builder /build/src/companion_engine/static/voice-player.html /app/src/companion_engine/static/voice-player.html
COPY --from=web-builder /build/src/companion_engine/static/voice-player-notices.txt /app/src/companion_engine/static/voice-player-notices.txt
COPY --from=web-builder /build/src/companion_engine/static/memory-panel.html /app/src/companion_engine/static/memory-panel.html
COPY --from=web-builder /build/src/companion_engine/static/provider-settings.html /app/src/companion_engine/static/provider-settings.html
RUN pip install --no-cache-dir --no-build-isolation --no-deps . \
    && rm -rf /app/src

COPY --from=web-builder /build/examples/showcase/dist /app/examples/showcase/dist
COPY --from=web-builder /build/src/companion_engine/static/voice-player.html /app/built_static/voice-player.html
COPY --from=web-builder /build/src/companion_engine/static/voice-player-notices.txt /app/built_static/voice-player-notices.txt
COPY --from=web-builder /build/src/companion_engine/static/memory-panel.html /app/built_static/memory-panel.html
COPY --from=web-builder /build/src/companion_engine/static/provider-settings.html /app/built_static/provider-settings.html
COPY recall_sanitizer.py /app/recall_sanitizer.py
COPY dream_executor.py /app/dream_executor.py
COPY proactive_state.py /app/proactive_state.py
COPY obsidian_inbox.py /app/obsidian_inbox.py
COPY obsidian_exporter.py /app/obsidian_exporter.py
COPY xiaozhi_anima_service.py /app/xiaozhi_anima_service.py

# Non-root runtime user with explicit ownership of /app, /data/anima, and /obsidian (H-2)
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data/anima /obsidian \
    && chown -R app:app /app /data/anima /obsidian

# Place volatile build metadata LAST so changing BUILD_SHA never invalidates pip/src layers (H-2)
ARG BUILD_SHA=dev
LABEL org.opencontainers.image.revision="${BUILD_SHA}"
ENV ANIMA_BUILD_VERSION="${BUILD_SHA}"

USER app
EXPOSE 18787
CMD ["python", "/app/xiaozhi_anima_service.py"]
