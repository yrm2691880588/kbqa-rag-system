# ============================================================
# 前端镜像：两阶段构建
#   阶段一：用 Node 把 Vue 源码编译成静态文件（HTML/CSS/JS）
#   阶段二：只把编译产物放进 nginx 镜像（最终镜像不含 Node 和源码，体积小）
# ============================================================

# ---- 阶段一：构建 ----
FROM node:20-alpine AS builder
WORKDIR /app

# 先拷依赖清单并安装（利用缓存：依赖没变就不重装）
# 可选：指定 npm 源。国内构建从官方源装依赖慢，可传入国内镜像，例如：
#   docker compose --profile prod build \
#     --build-arg NPM_REGISTRY=https://registry.npmmirror.com frontend
ARG NPM_REGISTRY=""
COPY package.json package-lock.json ./
RUN if [ -n "${NPM_REGISTRY}" ]; then npm config set registry "${NPM_REGISTRY}"; fi \
 && npm ci

# 再拷源码并编译
COPY . .
RUN npm run build

# ---- 阶段二：托管 ----
FROM nginx:1.27-alpine

# 用自定义站点配置替换默认配置（含 SPA 回退、接口反代、流式响应设置）
COPY nginx.conf /etc/nginx/conf.d/default.conf

# 只把编译产物拷进来
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1
