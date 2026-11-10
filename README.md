# KBQA-RAG · 企业级电商知识库问答系统

一个从 0 到 1 独立搭建的 **RAG（检索增强生成）智能问答系统**：把商品说明书、价格表、营销文档等资料传进知识库，AI 就能基于这些资料准确回答用户问题，并像打字机一样实时输出回答。

> 面向「知识库问答」场景的企业级落地实践，覆盖 账号体系 → 文档解析 → 向量检索 → AI 生成回答 的完整链路。

---

## ✨ 功能亮点

| 模块 | 能力 | 说明 |
|------|------|------|
| 🔐 账号体系 | 注册 / 登录 / 刷新令牌 / 改密 | JWT 双令牌（Access + Refresh），密码 argon2 加密存储 |
| 📚 知识库管理 | 建库 / 传文档 / 入库解析 | 一个账号可建多个知识库，文档按库隔离 |
| 📄 多格式解析 | Word / PDF / Excel / Markdown / 网页 | 自动抽取正文，表格行也参与检索 |
| 🧠 向量化入库 | 智能切块 + 嵌入向量 | 阿里云百炼 `text-embedding-v4`（1024 维） |
| 🔍 精准召回 | 多路检索 + 文档级去噪 | 相似度阈值 0.45 + 相对过滤，避免"张冠李戴"混入无关文档 |
| 💬 流式问答 | SSE 打字机效果 | 实时返回「思考 → 回答」，带来源依据 |
| 🧼 对话体验 | 闲聊识别 / 意图识别 | 寒暄不查库；问"有什么商品"这类开放问题，直接列出商品清单 |
| 🖥️ 管理后台 | 会话管理 / 知识库管理 / 用户管理 | 前后端分离，接口幂等设计 |

---

## 🛠️ 技术栈

| 层次 | 技术 | 用途 |
|------|------|------|
| 后端框架 | **FastAPI** + Uvicorn | 高性能异步 API 服务 |
| 数据库 | **PostgreSQL 16**（Docker） | 用户 / 会话 / 消息 / 知识库元数据 |
| 向量库 | **Qdrant**（Docker） | 存储文档切块的高维向量，做相似度检索 |
| ORM | SQLAlchemy 2.0（async） | 异步数据库访问 |
| 文档解析 | PyMuPDF / python-docx / openpyxl / BeautifulSoup | PDF / Word / Excel / HTML 解析 |
| 切块 | langchain-text-splitters | 长文档智能分块 |
| 向量模型 | 阿里云百炼 DashScope `text-embedding-v4` | 文本 → 1024 维向量 |
| 对话模型 | DeepSeek `deepseek-v4-flash` | 基于检索结果生成回答 |
| 前端 | **Vue 3** + TypeScript + Vite | SPA 界面 |
| UI 组件 | Element Plus + Pinia + Vue Router + Axios | 组件库 / 状态管理 / 路由 / 请求 |
| 鉴权 | JWT（PyJWT）+ argon2-cffi | 双令牌刷新机制 |

---

## 🏗️ 系统架构

```
┌─────────────────────────────────────────────────────────┐
│  前端 Vue3 (Vite)           http://127.0.0.1:5173       │
│  登录 / 知识库 / 聊天界面                                 │
└──────────────────────┬──────────────────────────────────┘
                       │ REST + SSE(流式)
┌──────────────────────▼──────────────────────────────────┐
│  后端 FastAPI           http://127.0.0.1:8000  /docs    │
│                                                         │
│  ① 文档入库链路:  解析 → 切块 → 向量化 → 写入 Qdrant      │
│  ② 问答链路:      问题 → 向量检索 → 文档去噪 → 生成回答    │
│  ③ 体验模块:      闲聊识别 / 开放意图识别 / 会话记忆        │
└───────┬───────────────────────────┬─────────────────────┘
        │                           │
┌───────▼─────────┐      ┌──────────▼──────────┐
│  PostgreSQL 16  │      │  Qdrant 向量库       │
│  业务数据        │      │  文档向量 + 检索      │
└─────────────────┘      └─────────────────────┘
```

**问答核心流程（RAG）**

1. 用户提问 → 后端先做**意图判断**（寒暄？开放列举？还是真问题？）
2. 真问题 → 问题文本转成向量 → 到 Qdrant 里取回最相似的候选片段
3. **文档级去噪**：候选片段先按"文档"归类，剔除明显低于同文档最佳命中的噪声片段
4. 命中片段 + 系统提示词 拼给 DeepSeek → 生成带依据的回答，SSE 流式返回
5. 没命中 → 明确回答"知识库中没有相关信息"，不乱编

---

## 📁 目录结构

```
├── backend/                  # FastAPI 后端
│   ├── app/
│   │   ├── api/v1/           # 路由：认证/知识库/文档/聊天/会话/用户
│   │   ├── core/             # 配置/安全/依赖注入
│   │   ├── db/               # 数据库会话与初始化
│   │   ├── models/           # ORM 模型（用户/会话/消息/文档…）
│   │   ├── schemas/          # 请求/响应数据模型
│   │   └── services/         # 核心业务
│   │       ├── retriever.py      # 向量检索 + 文档级去噪
│   │       ├── rag.py            # RAG 生成主流程
│   │       ├── chunker.py        # 智能切块
│   │       ├── smalltalk.py      # 闲聊识别
│   │       ├── open_intent.py    # 开放意图识别
│   │       └── vectorizer.py     # 向量化(百炼嵌入模型)
│   ├── tests/                # 单元测试（51 个用例，unittest）
│   ├── requirements.txt      # Python 依赖
│   └── .env.example          # 环境变量模板（复制为 .env）
├── frontend/                 # Vue3 前端
│   └── src/
│       ├── views/            # 页面（登录/注册/聊天/后台）
│       ├── api/              # 接口封装（含 SSE 流式）
│       ├── stores/           # Pinia 状态（登录态等）
│       └── router/           # 路由守卫
├── scripts/                  # 一键启动脚本(.bat)
│   ├── start-docker.bat      # ① 启动数据库容器
│   ├── start-backend.bat     # ② 启动后端  (127.0.0.1:8000)
│   ├── start-frontend.bat    # ③ 启动前端  (127.0.0.1:5173)
│   └── stop-docker.bat       # 停止数据库容器
├── 样例资料/                  # 可直接上传测试的示例文档
└── docker-compose.yml        # PostgreSQL + Qdrant 编排
```

---

## 🚀 快速启动（本地开发）

> 共 4 步，按顺序执行即可。启动脚本已备好：`scripts/` 目录。

### 0️⃣ 环境要求

| 依赖 | 版本 | 用途 |
|------|------|------|
| Docker Desktop | 任意较新版本 | 跑 PostgreSQL + Qdrant |
| Python | 3.11+（实测 3.12） | 后端 |
| Node.js | 18+ | 前端构建 |

### 1️⃣ 配置密钥（只需一次）

```bash
cd backend
copy .env.example .env        # Windows
# 然后把 .env 里的两个 Key 填成你自己的：
#   EMBEDDING_API_KEY = 阿里云百炼 Key（向量模型，必填）
#   LLM_API_KEY       = DeepSeek Key（对话模型，必填）
# 申请地址见 .env.example 内注释
```

### 2️⃣ 启动数据库（Docker）

```bash
docker compose up -d
# 或双击 scripts\start-docker.bat
```

会启动两个容器：`kbqa-postgres`（5432）与 `kbqa-qdrant`（6333/6334）。

### 3️⃣ 启动后端

```bash
# 建议先建虚拟环境并装依赖：
python -m venv .venv
.venv\Scripts\pip install -r backend\requirements.txt

# 启动（或双击 scripts\start-backend.bat）
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

后端就绪后：
- API 文档（Swagger）：http://127.0.0.1:8000/docs
- 健康检查：http://127.0.0.1:8000/api/v1/health

### 4️⃣ 启动前端

```bash
cd frontend
npm install
npm run dev
# 或双击 scripts\start-frontend.bat
```

浏览器打开 **http://127.0.0.1:5173** 即可使用。

> 💡 也可以直接双击 `scripts\start-all.bat` 一次弹出前后端两个窗口。

---

## 🧪 单元测试

```bash
cd backend
.venv\Scripts\python -m unittest discover -s tests -v
```

共 **51 个用例**，覆盖：切块逻辑、向量检索去噪、闲聊识别、开放意图识别等核心模块。

---

## 📝 使用流程（第一次体验）

1. 打开 http://127.0.0.1:5173 → **注册账号**
2. 进入知识库 → **新建知识库**（如"商品资料库"）
3. 在知识库内**上传文档**：可直接使用 `样例资料/` 目录里的文件
   （`商品总览.md`、`星翼智能手表Watch2手册.pdf`、`星翼蓝牙耳机Pro说明书.docx`、`星耀空气净化器A8.md`、`星翼数码商品价格表.xlsx` 等）
4. 等文档状态变为"已入库" → 进入**聊天**，选择该知识库
5. 开始提问，例如：
   - "Watch2 的电池续航怎么样？"（具体问答）
   - "介绍一下本商品的型号和价格"（开放意图 → 自动列出商品清单）
   - "你好"（闲聊 → 不查库，正常打招呼）

---

## 🗺️ 开发路线（M1 → M4）

| 里程碑 | 内容 | 状态 |
|--------|------|------|
| **M1** | 项目脚手架 / 账号体系 / Docker 底座（PG+Qdrant） | ✅ 完成 |
| **M2** | 文档多格式解析 / 智能切块 / 向量化入库 | ✅ 完成 |
| **M3** | RAG 问答链路 / SSE 流式 / 会话记忆 / 检索去噪调优 | ✅ 完成 |
| **M4** | 重排模型（Rerank）精度优化 / 生产部署 | ⏳ 规划中 |

---

## ⚠️ 已知说明

- `.env` 含真实密钥，已被 `.gitignore` 排除，**不会**上传到仓库；提交的仅为 `.env.example` 模板。
- 本地上传的文档存于 `backend/data/`（已 gitignore）。
- 轻量体验：若不想装 Docker，可将 `DATABASE_URL` 切换为 SQLite 降级方案（见 `.env.example` 注释），但向量库 Qdrant 仍需 Docker。

---

## 🤝 参与贡献

本项目主要用于学习与交流，欢迎提 Issue 讨论。提交代码前请先看
[docs/README.md](docs/README.md) 了解项目结构与约定。

如果发现安全问题，请**不要**公开提 Issue，处理方式见 [SECURITY.md](.github/SECURITY.md)。

---

## 📄 License

本项目基于 **MIT 许可证**开源，详见 [LICENSE](LICENSE)。

你可以自由使用、修改、分发本项目代码，包括商业用途，只需保留原始的版权声明。

