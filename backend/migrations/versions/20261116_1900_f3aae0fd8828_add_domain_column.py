"""add domain column to knowledge_bases and sessions

## 为什么加这两个字段

领域切换后，用户不应该看到**其他领域**的知识库和历史会话。

改造前的情况：领域只影响"提示词和文案"，不影响数据。
切到财经领域，左侧仍列出电商时建的会话和知识库 ——
用户会困惑"我明明选的是财经，怎么还有电商的东西"。

这两个字段让数据层也能按领域隔离。

## 存量数据怎么办

回填为 `generic`（通用领域）。

为什么不回填成 `ecommerce`：
    那样会"假设"所有旧数据都是电商的。虽然当前确实只有电商数据，
    但假设将来被推翻时很难纠正。`generic` 是中性选择 ——
    用户切到通用领域能看到旧数据，切到其他领域看不到，
    需要时再手动迁移（属于用户自己的数据决策）。

## 索引

`domain` 建索引。列表查询都是 `WHERE domain = ?` 的形式，
不建索引会全表扫描。数据量不大时影响不明显，但这是标准做法。

Revision ID: f3aae0fd8828
Revises: 29c50d4ee624
Create Date: 2026-11-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f3aae0fd8828"
down_revision: Union[str, None] = "29c50d4ee624"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 用 batch_alter_table 兼容 SQLite（它不支持部分 ALTER 语法）。
    #
    # server_default 必须给：已有行需要一个值，否则 NOT NULL 加不上去。
    # 加完后保留 server_default 也无害（新插入没显式给值时自动用 generic）。
    for table in ("knowledge_bases", "sessions"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "domain",
                    sa.String(length=32),
                    nullable=False,
                    server_default="generic",
                )
            )
            batch_op.create_index(
                batch_op.f(f"ix_{table}_domain"), ["domain"], unique=False
            )

    # ---- 知识库的唯一约束要加上 domain ----
    #
    # 原来是 (org_id, name)：电商领域建了「产品库」后，
    # 财经领域再也建不了同名库 —— 报 UNIQUE 冲突，
    # 但用户在财经领域根本看不到那个库。
    # 改成 (org_id, domain, name) 后，不同领域可以有同名库。
    with op.batch_alter_table("knowledge_bases", schema=None) as batch_op:
        # drop 旧约束；SQLite 下 batch 模式会重建表，PostgreSQL 下走 ALTER
        try:
            batch_op.drop_constraint("uq_knowledge_bases_org_name", type_="unique")
        except Exception:  # noqa: BLE001 - 约束名可能不存在（老库形态不同）
            pass
        batch_op.create_unique_constraint(
            "uq_knowledge_bases_org_domain_name", ["org_id", "domain", "name"]
        )


def downgrade() -> None:
    # 先把唯一约束改回两列，否则删列时约束还引用着 domain
    with op.batch_alter_table("knowledge_bases", schema=None) as batch_op:
        try:
            batch_op.drop_constraint("uq_knowledge_bases_org_domain_name", type_="unique")
        except Exception:  # noqa: BLE001
            pass
        batch_op.create_unique_constraint(
            "uq_knowledge_bases_org_name", ["org_id", "name"]
        )

    for table in ("sessions", "knowledge_bases"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_index(batch_op.f(f"ix_{table}_domain"))
            batch_op.drop_column("domain")
