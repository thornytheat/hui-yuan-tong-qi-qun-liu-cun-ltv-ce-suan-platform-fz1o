"""stored-value wallet: order types, refunds, member balances

储值制改造：
- member_orders 增加 order_type / refund_order_no / refund_type 与金额符号、退款关联约束；
- retention_cells 增加充值/退款/待消费余额字段；
- 新增 member_balances 会员待消费余额台账。

demo 环境由 scripts/bootstrap_db.py 直接 create_all；本迁移用于正式环境对齐。

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-12
"""
from __future__ import annotations

from alembic import op

from src.config.settings import settings
from src.models import Base

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{settings.db_schema}"')
    # 与 0001 同策略：模型是唯一事实来源，create_all 幂等补齐新表/新列（demo 库直接重建）
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        f'DROP TABLE IF EXISTS "{settings.db_schema}".member_balances'
    )
