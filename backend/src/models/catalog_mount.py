"""目录挂载点：同一公开文件在某个栏目某页清单上的「当前位置」（按站点保存，每次巡检全量校准）。

口径（与 README「挂载点口径」一致）：
- 一个 canonical 地址可同时挂在多个栏目 / 多页清单下，一行 = 一个当前挂载点；
- 自然键 (site_id, canonical_url, column_code, page_no)：重跑巡检按自然键 upsert，不新增重复记录；
- 每次巡检结束时以「本次抓取到的位置」为准做差集：消失的删除、仍在的原样保留（不误删）；
- 三向对账的 A 集合 = 当前仍有挂载的地址（按地址算一份）；无挂载的文件不算已收录。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class CatalogMount(Base):
    __tablename__ = "catalog_mounts"
    __table_args__ = (
        UniqueConstraint("site_id", "canonical_url", "column_code", "page_no", name="uq_mount_site_position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    canonical_url: Mapped[str] = mapped_column(String(300), index=True)
    column_code: Mapped[str] = mapped_column(String(60), index=True)
    column_name: Mapped[str] = mapped_column(String(120), default="")
    page_no: Mapped[int] = mapped_column(Integer, default=1)
    entry_url: Mapped[str] = mapped_column(String(300), default="")
    page_url: Mapped[str] = mapped_column(String(300), default="")
    source_href: Mapped[str] = mapped_column(String(300), default="")
    title: Mapped[str] = mapped_column(String(200), default="")
    discovered_from: Mapped[str] = mapped_column(String(20), default="rel_next")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
