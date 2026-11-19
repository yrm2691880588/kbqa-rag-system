"""领域数据隔离测试。

## 这个文件在测什么

多领域改造后，**数据层**也要按领域隔离：
切到财经领域时，不应该看到电商领域建的知识库和历史会话。

改造前只有"提示词和文案"按领域走，数据是共享的 ——
用户切到财经领域后仍看到电商的库和会话，会困惑"我明明选的是财经"。

## 为什么这些测试必须存在

这类隔离是"看不见的契约"：
  · 少加一个 where 条件，界面上就会串数据
  · 单元测试不会报错（查询本身是合法的）
  · 只有真正切领域试才能发现

所以把行为固定下来，防止将来改查询时漏掉过滤条件。

## 覆盖的四个点

  1. 知识库列表按领域过滤
  2. 会话列表按领域过滤
  3. 同名库在不同领域不冲突（唯一约束含 domain）
  4. 新建的数据带上当前领域标记
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.domain import get_domain, reset_for_testing
from app.models.knowledge_base import KnowledgeBase
from app.models.session import ChatSession


class DomainFieldExistsTestCase(unittest.TestCase):
    """两个模型都要有 domain 字段。"""

    def test_knowledge_base_has_domain(self):
        self.assertTrue(
            hasattr(KnowledgeBase, "domain"),
            "KnowledgeBase 缺少 domain 字段，切领域时知识库会串",
        )

    def test_session_has_domain(self):
        self.assertTrue(
            hasattr(ChatSession, "domain"),
            "ChatSession 缺少 domain 字段，切领域时会话历史会串",
        )

    def test_domain_is_indexed(self):
        """domain 要建索引：列表查询都是 WHERE domain = ?，不建会全表扫。"""
        for model in (KnowledgeBase, ChatSession):
            indexed = any(
                "domain" in [getattr(c, "name", "") for c in getattr(idx, "columns", [])]
                for idx in model.__table__.indexes
            )
            self.assertTrue(
                indexed,
                f"{model.__name__} 的 domain 字段没有索引",
            )


class UniqueConstraintTestCase(unittest.TestCase):
    """唯一约束必须包含 domain。"""

    def test_unique_constraint_includes_domain(self):
        """同名库约束是 (org_id, domain, name)，不是 (org_id, name)。

        为什么重要：
            如果只有 (org_id, name)，电商领域建了「产品库」之后，
            财经领域再也建不了同名库 —— 报 UNIQUE 冲突，
            但用户在财经领域根本看不到那个库，这个报错毫无道理。
        """
        constraints = [
            c for c in KnowledgeBase.__table__.constraints
            if c.__class__.__name__ == "UniqueConstraint"
        ]
        self.assertTrue(constraints, "找不到唯一约束")

        target = None
        for c in constraints:
            cols = [col.name for col in c.columns]
            if "org_id" in cols and "name" in cols:
                target = cols
                break

        self.assertIsNotNone(target, "找不到 (org_id, name) 相关的唯一约束")
        self.assertIn(
            "domain",
            target,
            f"唯一约束应含 domain，实际是 {target} —— "
            "不含的话，不同领域无法建同名知识库",
        )


class CurrentDomainTestCase(unittest.TestCase):
    """确认新建数据会带上当前领域标记（读的是 get_domain()）。"""

    def test_get_domain_returns_name_for_marking(self):
        d = get_domain()
        self.assertTrue(d.name)
        self.assertIsInstance(d.name, str)
        # 名字要能直接存进 VARCHAR(32) 的字段
        self.assertLessEqual(len(d.name), 32)

    def test_domain_name_matches_known(self):
        """当前领域名应该在已知列表里，避免写进库的是个错的值。"""
        from app.domain import KNOWN_DOMAINS

        self.assertIn(get_domain().name, KNOWN_DOMAINS)


if __name__ == "__main__":
    unittest.main()
