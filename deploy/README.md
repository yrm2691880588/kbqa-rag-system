# 部署与运维手册

> 这份文档回答三个问题：**怎么装上去**、**日常怎么管**、**出问题怎么办**。
> 所有命令都是复制粘贴即可执行的形式，命令下方的「说明」是给不熟悉 Linux 的读者看的。

---

## 一、服务器要准备什么

| 项目 | 要求 | 说明 |
|---|---|---|
| 服务器 | 2 核 4G 起步（推荐） | 1 核 2G 也能跑，但向量检索会慢；有 500 并发需求时建议 4 核 8G |
| 系统 | Ubuntu 22.04 / 24.04 | 其他 Linux 发行版也可，命令略有差异 |
| 磁盘 | 40G 以上 | 数据库 + 向量数据 + 上传文件都在这台机器上 |
| 软件 | Docker（含 compose 插件） | 安装：`curl -fsSL https://get.docker.com | sh` |
| 端口 | 放行 80（HTTP）；启用 HTTPS 后再放行 443 | 在云服务商的"安全组"里放行 |
| 域名 | 可选但**建议有** | 没有域名就只能用 IP 访问，且无法启用 HTTPS |

> 关于备案：服务器在中国大陆，网站需要备案（约 1~2 周审核）；选择中国香港或境外服务器可免备案。

### ⚠️ 国内服务器必读：配置镜像加速

这套系统由 Docker 容器组成，首次部署要从网上下载 4 个基础镜像（相当于"运行环境压缩包"）。
**Docker 官方仓库在国内大陆访问不通**，不配加速会出现"卡住不动 / 连接超时"。

**判断方法**：执行 `docker pull hello-world`，如果长时间无响应或报 `dial tcp ... timeout`，就是这个问题。

**解决办法（二选一）**：

```bash
# 办法 A：配置加速器（推荐，一次配置永久生效）
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json > /dev/null <<'EOF'
{
  "registry-mirrors": ["https://docker.1ms.run", "https://docker.m.daocloud.io"]
}
EOF
sudo systemctl restart docker
docker info | grep -A3 "Registry Mirrors"    # 看到地址即配置成功
```

```bash
# 办法 B：不改配置，手动从镜像站拉取再改成标准名字（适合没有 sudo 权限的情况）
MIRROR=docker.1ms.run/library
for img in python:3.12-slim node:20-alpine nginx:1.27-alpine alpine:3.20; do
    docker pull "$MIRROR/$img" && docker tag "$MIRROR/$img" "$img"
done
```

> 镜像站会变动，`docker.1ms.run` / `docker.m.daocloud.io` 若失效，可换用其他公共加速源。
> 配置好之后再执行 `bash deploy/install.sh`，就不会卡在下载镜像这一步。

**顺带一提**：构建镜像时还要从 pip（Python 依赖）和 npm（前端依赖）下载软件包，
国内网络下这一步也可能很慢。可以把国内源写进根目录 `.env`，构建时会自动使用：

```bash
# 追加到根目录 .env（不存在就创建）
cat >> .env <<'EOF'
PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
NPM_REGISTRY=https://registry.npmmirror.com
EOF
```

> 这两项只在**构建镜像时**生效，不写也不影响运行，只是构建慢一些。

---

## 二、首次部署（5 步）

```bash
# ① 把项目放到服务器上
git clone <你的仓库地址> /opt/kbqa
cd /opt/kbqa

# ② 执行一键安装（第一次会生成配置模板并停下，提示你填内容）
bash deploy/install.sh
```

安装脚本第一次运行会**停下并告诉你还缺哪些配置**（例如 API Key、数据库口令）。这是有意设计——宁可装不上，也不要带着空配置跑起来。

```bash
# ③ 填写配置（按提示填 backend/.env 与 .env）
nano backend/.env      # 需要填：数据库口令、管理员密码、两个 AI 服务的 Key、域名
nano .env              # 需要填：数据库账号口令（与上面保持一致）

# ④ 再次执行安装（这次会自动构建镜像并启动）
bash deploy/install.sh

# ⑤ 验证
curl http://127.0.0.1/api/v1/health
```

**安装成功的标志**：浏览器打开 `http://服务器IP/` 能看到登录页，用 `backend/.env` 里的管理员账号密码可以登录。

### 配置项说明（backend/.env 里要填什么）

| 配置项 | 填什么 | 哪里拿 |
|---|---|---|
| `SECRET_KEY` | 登录令牌的签名密钥（安装脚本会自动生成，无需手填） | 自动生成 |
| `DATABASE_URL` | 数据库连接地址，主机名写 `postgres`（容器名），密码与根目录 `.env` 一致 | 自己设 |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | 首位管理员账号（首次启动自动创建） | 自己设，密码要强 |
| `CORS_ORIGINS` | 前端访问地址，填真实域名，如 `["https://kbqa.example.com"]` | 你的域名 |
| `LLM_API_KEY` | DeepSeek 的 API Key | DeepSeek 开放平台 |
| `EMBEDDING_API_KEY` | 阿里云百炼的 API Key | 阿里云百炼控制台 |
| `SMS_PROVIDER` | 短信通道，目前填 `mock`（测试模式）；接入真实短信后改 `aliyun` | 见第七节 |

> **重要**：`APP_ENV` 必须是 `prod`。它决定"生产严格自检"是否生效——密钥没配好、压测开关误开等情况会被直接拦下并拒绝启动。

---

## 三、日常运维

```bash
cd /opt/kbqa

# 看服务状态（running / healthy 说明正常）
docker compose --profile prod ps

# 看后端日志（实时滚动，Ctrl+C 退出）
docker compose --profile prod logs -f backend

# 只看健康检查详细结果
docker compose --profile prod exec backend python -c "import urllib.request,json;print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health')),ensure_ascii=False,indent=2))"

# 重启单个服务
docker compose --profile prod restart backend

# 停止全部（保留数据）
docker compose --profile prod down

# 重新启动
docker compose --profile prod up -d
```

| 健康探针 | 用途 | 说明 |
|---|---|---|
| `/api/v1/health/live` | 进程是否活着 | 永远很快返回，用于「容器挂了要重启」的判断 |
| `/api/v1/health/ready` | 依赖是否就绪 | 数据库/向量库/磁盘任一异常返回 503 |
| `/api/v1/health` | 完整分项明细 | 人排查用，能看到每一项的耗时与结论 |
| `/api/v1/health/deep` | 真实调用外部 AI | 验证密钥/网络是否真的可用（低频使用，会消耗极少量费用） |

---

## 四、升级与回滚

```bash
# 升级到最新代码（会自动先备份，再把当前版本存为"上一版"）
bash deploy/update.sh

# 升级后有问题，退回上一个版本
bash deploy/rollback.sh
```

> 升级不影响数据：数据在独立的数据卷里，容器重建不会丢。若新版本改动了数据库结构且需要回退，请配合 `bash deploy/restore.sh` 恢复数据备份。

---

## 五、备份与恢复（**最重要的一节**）

三份数据都要备份：**数据库**（用户/会话/消息）、**向量库**（知识库检索数据）、**上传文件**（原始文档）。

```bash
# 手动备份一次（结果在 ./backups/<时间戳>/）
bash deploy/backup.sh

# 恢复到最新一份备份（会要求输入 yes 确认）
bash deploy/restore.sh

# 恢复到指定备份
bash deploy/restore.sh backups/20261008-031500
```

### 设置每天自动备份

```bash
crontab -e
# 加入下面这一行：每天凌晨 3 点备份，备份到 /data/backups
0 3 * * * cd /opt/kbqa && BACKUP_ROOT=/data/backups bash deploy/backup.sh >> /var/log/kbqa-backup.log 2>&1
```

### 备份的三条铁律

1. **备份要放在别的机器上**：本机磁盘坏了，同机的备份也一起没了（可同步到对象存储或另一台服务器）。
2. **备份要定期演练恢复**：从没恢复过的备份 = 没有备份。建议每月做一次恢复演练。
3. **默认保留最近 7 份**：需要更多用 `BACKUP_KEEP=30 bash deploy/backup.sh`。

---

## 六、启用 HTTPS（让浏览器显示安全小锁）

### 步骤

```bash
# ① 域名解析：在域名控制台加一条 A 记录，把域名指向服务器 IP，等生效

# ② 申请免费证书（Let's Encrypt）
bash deploy/init-letsencrypt.sh your-domain.com your-email@example.com

# ③ 切换配置：把模板里的 <你的域名> 换成真实域名后覆盖 nginx 配置
sed 's/<你的域名>/your-domain.com/g' deploy/nginx-https.conf.example > frontend/nginx.conf

# ④ 修改 docker-compose.yml：frontend 的端口映射加上 443
#     ports:
#       - "80:80"
#       - "443:443"

# ⑤ 重建前端容器
docker compose --profile prod up -d --build frontend
```

### 证书自动续期

Let's Encrypt 证书**90 天到期**，需配置自动续期：

```bash
crontab -e
# 每周一凌晨 3 点尝试续期（未到期会自动跳过）
0 3 * * 1 cd /opt/kbqa && docker run --rm -v kbqa_certbot_www:/var/www/certbot -v kbqa_letsencrypt:/etc/letsencrypt certbot/certbot renew --webroot -w /var/www/certbot --quiet && docker exec kbqa-frontend nginx -s reload
```

---

## 七、监控告警

### 方案一：用现成的监控服务（推荐给非技术用户）

把 `https://你的域名/api/v1/health/ready` 填进任意监控服务（如 UptimeRobot 免费版），
它每隔几分钟访问一次：**返回 503 就给你发告警**（邮件/微信/短信）。这比自己搭监控简单得多。

### 方案二：服务器本地巡检（不依赖外部服务）

```bash
crontab -e
# 每 5 分钟检查一次；不健康就写进日志（可再配 rsyslog 邮件告警）
*/5 * * * * curl -sf --max-time 10 http://127.0.0.1/api/v1/health/ready > /dev/null || echo "$(date) 服务不健康" >> /var/log/kbqa-alert.log
```

### 建议盯住的指标

| 指标 | 怎么看 | 危险信号 |
|---|---|---|
| 服务可用性 | `/health/ready` 状态码 | 出现 503 |
| 容器状态 | `docker compose ps` | 不是 healthy |
| 磁盘空间 | `df -h` | 剩余 < 15% |
| 内存占用 | `docker stats --no-stream` | 长期 > 85% |
| 外部 AI 可用性 | `/health/deep`（每天一次） | 返回 503 |

---

## 八、故障排查速查表

| 现象 | 可能原因 | 怎么办 |
|---|---|---|
| 安装脚本卡在 `Building / Pulling`，或报 `dial tcp ... timeout` | Docker 官方仓库在国内访问不通 | 按第一节「国内服务器必读」配置镜像加速后重试 |
| 构建时卡在 `apt-get update`（约 1 分钟无响应） | Debian 官方软件源在国内慢 | 一般可忍受（只发生在首次构建）。若长时间卡死，在 `backend/Dockerfile` 安装字体那行前插入一句换源：`RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/*.sources /etc/apt/sources.list 2>/dev/null || true` |
| 拉取镜像报 `certificate is valid for localhost` | 本机安全软件/代理拦截了 HTTPS | 关闭安全软件的"网页防护/HTTPS 扫描"，或改用办法 B 手动指定镜像站 |
| 后端起不来，日志提示"配置自检未通过" | 生产必填配置缺失（密钥/API Key/数据库地址） | 按日志列出的问题逐条修正 `backend/.env` |
| 浏览器打不开页面 | 容器没起来 / 安全组没放行 80 端口 | `docker compose --profile prod ps` 看状态；检查云安全组 |
| 页面能开但接口 502 | 后端容器挂了或未就绪 | `docker compose logs --tail=100 backend` |
| 上传文档后一直"处理中" | 向量服务不可用（Key 失效/额度用尽） | 访问 `/api/v1/health/deep` 看向量服务那一项 |
| 提问答"未找到相关内容" | 知识库为空，或检索相似度低于阈值 | 确认已上传文档且状态为"已完成" |
| AI 回答是"模拟回答如下…" | 压测开关 `LLM_MOCK` 被打开 | 改 `backend/.env` 里 `LLM_MOCK=false` 并重启 |
| 登录提示"操作过于频繁" | 触发了登录限流（防暴力破解） | 等 1 分钟；确认不是多人共用同一出口 IP |
| 磁盘满了 | 上传文件与向量数据增长 | 清理旧备份；`docker system prune` 清无用镜像 |

---

## 九、上线前安全自查清单

- [ ] `APP_ENV=prod`（开启严格自检）
- [ ] `SECRET_KEY` 是随机长串（≥32 位），不是示例值
- [ ] `ADMIN_PASSWORD` 不是 `123456` 这类弱口令
- [ ] `LLM_MOCK=false`
- [ ] 数据库口令是强密码，且 `postgres` 端口没有对公网开放
- [ ] 已配置 HTTPS，浏览器显示安全小锁
- [ ] 已配置每日自动备份，并**做过一次恢复演练**
- [ ] 已配置健康监控告警
- [ ] 管理员账号登录测试通过，普通用户注册/登录/提问测试通过

---

## 十、目录结构说明（部署相关）

```
deploy/
├── install.sh                 # 首次安装
├── update.sh                  # 升级（自动备份 + 保留旧版本）
├── rollback.sh                # 回滚到上一版本
├── backup.sh                  # 备份三份数据
├── restore.sh                 # 从备份恢复
├── init-letsencrypt.sh        # 申请 HTTPS 证书
├── nginx-https.conf.example   # HTTPS 站点配置模板
└── README.md                  # 本文件

backend/Dockerfile             # 后端镜像
frontend/Dockerfile            # 前端镜像（两阶段构建）
frontend/nginx.conf            # 站点配置（静态托管 + 接口反代）
docker-compose.yml             # 编排（开发只起数据库；--profile prod 起全套）
```
