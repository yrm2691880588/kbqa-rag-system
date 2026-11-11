# 参与贡献指南

感谢你对本项目的兴趣。这份文档说明**怎么改代码才能被顺利合并**，
以及本地怎么验证自己的改动。

---

## 一、快速上手

```bash
# 1. 克隆并进入项目
git clone <仓库地址>
cd kbqa-rag-system

# 2. 准备后端环境
cd backend
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # macOS / Linux

# 3. 配置密钥（必须，否则后端起不来）
cp .env.example .env
# 然后编辑 .env，填入自己的 EMBEDDING_API_KEY 与 LLM_API_KEY

# 4. 起数据库（PostgreSQL + Qdrant）
cd ..
docker compose up -d

# 5. 起后端
cd backend
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000

# 6. 起前端（另开终端）
cd frontend
npm install
npm run dev
```

打开 <http://127.0.0.1:5173> 即可。后端接口文档在 <http://127.0.0.1:8000/docs>。

---

## 二、提交前必须做的验证

**任何改动提交前，请自行跑通下面两项。** CI 会做同样的检查，
本地不通过的话推上去也是红的。

### 后端测试

```bash
cd backend
python -m unittest discover -s tests -t . -v
```

当前基线：**511 个用例，应全部通过**（本地无 PostgreSQL 时会跳过 3 个需真库的用例）。

### 前端测试

```bash
cd frontend
npm test          # Node 内置测试运行器，无需额外依赖
```

当前基线：**9 个用例**。目前覆盖 401 分类逻辑（这段逻辑出过三次同类 bug，必须能测）。

> 前端此前完全没有测试设施，所以"同一个 bug 修了三次还在漏"没人拦。
> 现在至少有了一层防护，后续修前端逻辑时请顺手补测试。

如果需要跑并发额度测试（可选，需要真实 PostgreSQL）：

```bash
# 单独起一个测试库，不要用生产库
docker run -d --name kbqa-test-pg \
  -e POSTGRES_USER=kbqa -e POSTGRES_PASSWORD=testpw \
  -e POSTGRES_DB=kbqa_test -p 127.0.0.1:55432:5432 postgres:16-alpine

export KBQA_TEST_PG_URL="postgresql+asyncpg://kbqa:testpw@127.0.0.1:55432/kbqa_test"
python -m unittest tests.test_quota_concurrency -v
```

### 前端构建

```bash
cd frontend
npm run build          # 含 vue-tsc 类型检查 + vite 打包
```

---

## 三、写代码的约定

### 注释要说"为什么"，不要复述"做了什么"

这个项目的注释有个明确取向：**解释决策原因和踩过的坑**，而不是翻译代码。

```python
# ❌ 不要这样（代码已经说了）
# 把 limit 设为 None
limit = None

# ✅ 应该这样（说明为什么）
# 用 None 而不是 0 表示"不限"：0 会被 `if limit:` 判成假值，
# 结果"停用"变成"随便用"，正好反了。
limit = None
```

### 测试要测"出问题会怎样"，不要只测"正常路径"

```python
# ❌ 弱：只验证正常情况
def test_login(self):
    self.assertEqual(login("u", "p").status, 200)

# ✅ 强：把"不该发生的事"也锁死
async def test_member_cannot_list_users(self):
    """普通成员调管理员接口必须 403 —— 前端藏按钮不算防护。"""
    r = await self.client.get("/api/v1/users", headers=self.member_headers)
    self.assertEqual(r.status_code, 403)
```

### 涉及安全的改动，必须同时加测试

以下改动**不接受"只改代码不加测试"**：

| 类别 | 例子 |
|---|---|
| 鉴权/权限 | 路由守卫、角色判断、租户过滤 |
| 令牌与会话 | 登录、刷新、登出、Cookie 属性 |
| 输入校验 | 文件上传、问题长度、参数格式 |
| 金额/额度 | 扣减逻辑、并发计数 |

因为这类 bug 的共同特征是：**功能看起来正常，出事时才发现**。

---

## 四、提交信息格式

```
<type>(<scope>): <一句话摘要>

为什么：
  <问题背景，是什么触发了这次改动>

改了什么：
  - <关键点 1>
  - <关键点 2>

验证：
  - <跑过什么命令、结果如何>
```

`type` 取值：`feat` / `fix` / `docs` / `refactor` / `test` / `chore` / `ci` / `perf`

**不要**写 `update`、`修改`、`fix bug` 这类看不出信息的摘要。

---

## 五、不要提交这些

| 文件/目录 | 原因 |
|---|---|
| `.env`、`.env.*` | 含真实密钥。只提交 `.env.example` 模板 |
| `data/`、`backend/data/` | 本地上传的文件与测试数据库 |
| `node_modules/`、`dist/` | 依赖与构建产物 |
| `__pycache__/`、`*.pyc` | Python 字节码 |
| 任何含真实密钥、真实手机号、本机绝对路径的文件 | 见下节 |

`.gitignore` 已经覆盖了上面这些。如果不确定某个文件该不该提交：

```bash
git check-ignore -v <文件路径>
# 有输出 = 已被忽略；无输出 = 会被提交，请再确认它是否安全
```

---

## 六、安全相关的硬性要求

1. **密钥绝不进仓库**。本地用 `.env`，仓库里只放占位模板。
   如果你不小心提交了密钥：立刻去对应平台**吊销并重新生成**——
   删除提交记录不代表密钥失效。
2. **不要把内部错误原文返回给用户**。用一句通用提示，
   完整堆栈记到日志里（参考 `services/rag.py` 的异常分支）。
3. **越权判断只在一处实现**。租户隔离统一走 `core/tenancy.py`，
   不要在接口里各写一遍。
4. **前端隐藏按钮不算防护**。所有权限校验必须在服务端再做一次。

---

## 七、提 PR / Issue

- **Issue**：写清楚复现步骤、预期行为、实际行为。能附截图/日志更好。
- **Pull Request**：说明解决了什么问题、怎么验证的。小而聚焦的 PR 更容易合并。
- **安全漏洞**：**不要**公开提 Issue，处理方式见 [SECURITY.md](SECURITY.md)。

---

## 八、需要帮助

不确定某个改动方式对不对，先开 Issue 讨论再动手，
比写完几百行才发现方向不对要省事得多。
