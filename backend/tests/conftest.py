"""pytest 全局设置（在收集用例前执行）。

这里做一件事：**让自动化测试不依赖任何外部服务**。

为什么必须做（这是一个真实的副作用）：
    一旦 `.env` 里配好了重排密钥（生产上当然要配），
    测试里所有走到检索链路的用例都会**真的去调百炼接口**。后果有三个：
      ① 每次跑测试都要花钱（虽然很少，但没必要）；
      ② 测试变慢，而且结果受网络波动影响 —— 网络抖一下测试就红，
         这种"假红"会让人开始不信任测试，是很危险的开始；
      ③ 上游限流或欠费时，整个测试套件全军覆没，跟代码对错完全无关。

所以这里在测试会话里把"对外调用"相关的开关统一关掉。
**需要验证重排本身的用例自己显式打开**：
  · tests/test_rerank.py        —— 用本地假服务，不碰真接口
  · tests/_verify_rerank_real.py —— 真实账号实测脚本（名字以下划线开头，pytest 不会收集它）

写入方式说明：直接改全局 settings 单例的属性（而不是改 .env），
这样**只影响当前测试进程**，不会把配置改动写到磁盘上。
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402

# 1) 关掉重排：避免测试真的去调外部重排接口
settings.rerank_api_key = ""
settings.rerank_model = ""

# 2) 关掉问题缓存：缓存是跨用例共享状态的，开着会让"用例执行顺序"影响结果，
#    出现"单独跑通过、一起跑失败"这种最难查的问题
settings.qa_cache_enabled = False

# 3) 关掉分钟级限流：否则同一批用例跑得快时会被自己限流，报出一堆无关的 429
settings.ai_rate_limit_per_minute = 0
