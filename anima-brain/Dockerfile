# Stage 1: Build official Companion Studio React web console
FROM node:22-slim AS web-builder
WORKDIR /build/examples/showcase
COPY examples/showcase/package.json examples/showcase/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY docs /build/docs
COPY examples/showcase/ ./
RUN npm run build

# Stage 2: Lightweight Python 3.12 runtime for Anima Brain + Companion Studio + XiaoZhi Bridge
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app
COPY pyproject.toml README.md README.zh-CN.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY src ./src
COPY ui ./ui
RUN pip install --no-cache-dir -e ".[http,calls]"

COPY --from=web-builder /build/examples/showcase/dist /app/examples/showcase/dist
COPY xiaozhi_anima_service.py /app/xiaozhi_anima_service.py

EXPOSE 18787
CMD ["python", "/app/xiaozhi_anima_service.py"]
