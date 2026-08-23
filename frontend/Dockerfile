# RAG v14 Frontend — Next.js 14 standalone 模式
# 构建: docker build -t rag-v14-frontend .
# 运行: docker run -p 3001:3001 rag-v14-frontend

# ── Stage 1: 构建阶段 ──────────────────────────────────────────
FROM node:20-alpine AS builder

WORKDIR /app

# 先复制依赖文件（利用 Docker 层缓存）
COPY package.json package-lock.json ./
RUN npm ci --production=false

# 复制源码并构建
COPY . .
# NEXT_PUBLIC_* 在构建时内联进 JS 束：SSO 按钮跳转的 Keycloak 地址（deploy 注入；
# 缺省回退 window.location.origin）。否则构建时为空 → 登录页走同源 /realms。
ARG NEXT_PUBLIC_KEYCLOAK_URL=""
ARG NEXT_PUBLIC_KEYCLOAK_REALM=""
ARG NEXT_PUBLIC_KEYCLOAK_CLIENT_ID=""
ENV NEXT_PUBLIC_KEYCLOAK_URL=$NEXT_PUBLIC_KEYCLOAK_URL \
    NEXT_PUBLIC_KEYCLOAK_REALM=$NEXT_PUBLIC_KEYCLOAK_REALM \
    NEXT_PUBLIC_KEYCLOAK_CLIENT_ID=$NEXT_PUBLIC_KEYCLOAK_CLIENT_ID
RUN npm run build

# ── Stage 2: 生产运行阶段 ──────────────────────────────────────
FROM node:20-alpine AS runner

WORKDIR /app

ENV NODE_ENV=production
ENV PORT=3001

RUN addgroup --system --gid 1001 appgroup && \
    adduser --system --uid 1001 appuser

# 复制 standalone 输出（Next.js output: "standalone" 自动包含所需文件）
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public

# Next.js standalone 模式已自动打包 app/ 下的静态资源（fonts、favicon 等），无需额外复制

RUN chown -R appuser:appgroup /app
USER appuser

EXPOSE 3001

CMD ["node", "server.js"]
