"""公开目录条目服务：登记接口 + 挂载点（栏目/页码/入口位置）的校准、分页查询、按栏目汇总。

两张表的分工：
- `catalog_items`：按地址登记的目录注册表（人工登记 / seed 的口径底账）；
- `catalog_mounts`：每次巡检校准后的「当前挂载点」，同一地址可有多行（多栏目/多页），
  栏目明细、目录列表与三向对账的 A 集合都以它为准（无挂载 = 未收录）。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.models.catalog_item import CatalogItem
from src.models.catalog_mount import CatalogMount
from src.models.site import Site
from src.services.url_normalize import normalize_url
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_utc

MOUNT_FIELDS = ("column_name", "page_url", "entry_url", "source_href", "title", "discovered_from")


def serialize(item: CatalogItem, site_name: str | None = None, site_code: str | None = None) -> dict:
    return {
        "id": item.id,
        "site_id": item.site_id,
        "site_name": site_name,
        "site_code": site_code,
        "column_code": item.column_code,
        "column_name": item.column_name,
        "entry_url": item.entry_url,
        "canonical_url": item.canonical_url,
        "page_no": item.page_no,
        "item_no": item.item_no,
        "depth": item.depth,
        "discovered_from": item.discovered_from,
        "title": item.title,
        "enabled": item.enabled,
        "created_at": iso_utc(item.created_at),
    }


def serialize_mount(mount: CatalogMount, site_name: str | None = None, site_code: str | None = None) -> dict:
    return {
        "id": mount.id,
        "site_id": mount.site_id,
        "site_name": site_name,
        "site_code": site_code,
        "column_code": mount.column_code,
        "column_name": mount.column_name,
        "entry_url": mount.entry_url,
        "canonical_url": mount.canonical_url,
        "page_no": mount.page_no,
        "page_url": mount.page_url,
        "source_href": mount.source_href,
        "discovered_from": mount.discovered_from,
        "title": mount.title,
        "created_at": iso_utc(mount.created_at),
    }


def mount_key(mount: dict) -> tuple[str, str, int]:
    """挂载点自然键（站点内）：同一地址在同一栏目同一页只算一个位置。"""
    return (
        str(mount.get("canonical_url") or ""),
        str(mount.get("column_code") or ""),
        int(mount.get("page_no") or 1),
    )


def _normalize_mount(mount: dict) -> dict | None:
    """统一挂载点字段口径（长度/默认值），插入与更新走同一份，避免重跑抖动。"""
    canonical = normalize_url(mount.get("canonical_url") or mount.get("entry_url"))
    if not canonical:
        return None
    return {
        "canonical_url": canonical,
        "column_code": str(mount.get("column_code") or "")[:60],
        "page_no": int(mount.get("page_no") or 1),
        "column_name": str(mount.get("column_name") or "")[:120],
        "entry_url": str(mount.get("entry_url") or canonical)[:300],
        "page_url": str(mount.get("page_url") or "")[:300],
        "source_href": str(mount.get("source_href") or "")[:300],
        "title": str(mount.get("title") or "")[:200],
        "discovered_from": str(mount.get("discovered_from") or "rel_next")[:20],
    }


def sync_mounts(db: Session, site_id: int, mounts: list[dict]) -> dict:
    """把一次巡检看到的挂载点全量校准到 `catalog_mounts`（只动本站点）。

    - 本次仍存在的位置：保留原行（只刷新易变字段，不换 id、不新增重复记录）；
    - 本次新出现的位置：插入；
    - 本次消失的位置：删除；其他站点的挂载一律不碰（不误删）。
    返回 {added, updated, removed, total}，total 为校准后的挂载点总数。
    """
    current: dict[tuple[str, str, int], dict] = {}
    for raw in mounts:
        mount = _normalize_mount(raw)
        if mount is None:
            continue
        current.setdefault(mount_key(mount), mount)

    rows = db.scalars(select(CatalogMount).where(CatalogMount.site_id == site_id)).all()
    existing = {(row.canonical_url, row.column_code, row.page_no): row for row in rows}

    added = updated = removed = 0
    for key, mount in current.items():
        row = existing.get(key)
        if row is None:
            db.add(CatalogMount(site_id=site_id, **mount))
            added += 1
            continue
        changed = False
        for field in MOUNT_FIELDS:
            if getattr(row, field) != mount[field]:
                setattr(row, field, mount[field])
                changed = True
        if changed:
            updated += 1

    for key, row in existing.items():
        if key not in current:
            db.delete(row)
            removed += 1
    db.flush()
    return {"added": added, "updated": updated, "removed": removed, "total": len(current)}


def list_mounts(db: Session, site_id: int) -> list[dict]:
    """该站点当前全部挂载点（三向对账的 A 集合来源；同一地址多行 = 多个挂载）。"""
    rows = db.scalars(
        select(CatalogMount)
        .where(CatalogMount.site_id == site_id)
        .order_by(CatalogMount.column_code, CatalogMount.page_no, CatalogMount.canonical_url)
    ).all()
    return [
        {
            "entry_url": row.entry_url,
            "canonical_url": row.canonical_url,
            "column_name": row.column_name,
            "column_code": row.column_code,
            "page_no": row.page_no,
            "page_url": row.page_url,
            "title": row.title,
        }
        for row in rows
    ]


def list_items(
    db: Session,
    *,
    site_id: int | None = None,
    column_code: str | None = None,
    q: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """目录明细 = 当前挂载点列表：同一地址挂在多个栏目时逐行展开，便于核对全部位置。"""
    stmt = select(CatalogMount)
    count_stmt = select(func.count()).select_from(CatalogMount)
    if site_id:
        stmt = stmt.where(CatalogMount.site_id == site_id)
        count_stmt = count_stmt.where(CatalogMount.site_id == site_id)
    if column_code:
        stmt = stmt.where(CatalogMount.column_code == column_code)
        count_stmt = count_stmt.where(CatalogMount.column_code == column_code)
    if q:
        like = f"%{q.strip()}%"
        clause = CatalogMount.title.ilike(like) | CatalogMount.canonical_url.ilike(like)
        stmt = stmt.where(clause)
        count_stmt = count_stmt.where(clause)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(
        stmt.order_by(CatalogMount.site_id, CatalogMount.column_code, CatalogMount.page_no, CatalogMount.canonical_url)
        .limit(limit)
        .offset(offset)
    ).all()
    sites = {site.id: site for site in db.scalars(select(Site)).all()}
    return {
        "items": [
            serialize_mount(
                row,
                (sites.get(row.site_id).name if sites.get(row.site_id) else None),
                (sites.get(row.site_id).code if sites.get(row.site_id) else None),
            )
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def column_summary(db: Session, site_id: int) -> list[dict]:
    """栏目明细：按栏目汇总当前挂载点（条目数 = 挂载点数，多栏目挂载的文件在每个栏目都计入）。"""
    rows = db.execute(
        select(
            CatalogMount.column_code,
            CatalogMount.column_name,
            func.count(),
            func.max(CatalogMount.page_no),
        )
        .where(CatalogMount.site_id == site_id)
        .group_by(CatalogMount.column_code, CatalogMount.column_name)
        .order_by(CatalogMount.column_code)
    ).all()
    return [
        {
            "column_code": code,
            "column_name": name,
            "entries": int(count),
            "pages": int(pages or 0),
        }
        for code, name, count, pages in rows
    ]


def create_item(db: Session, payload: dict) -> CatalogItem:
    site = db.get(Site, int(payload["site_id"]))
    if site is None:
        raise not_found("site", payload.get("site_id"))
    entry_url = payload["entry_url"].strip()
    exists = db.scalar(select(CatalogItem).where(CatalogItem.site_id == site.id, CatalogItem.entry_url == entry_url))
    if exists is not None:
        raise AppError("CONFLICT", 409, {"entry_url": entry_url})
    canonical = normalize_url(payload.get("canonical_url") or entry_url)
    item = CatalogItem(
        site_id=site.id,
        column_code=payload.get("column_code") or "other",
        column_name=payload.get("column_name") or "其他栏目",
        entry_url=entry_url,
        canonical_url=canonical,
        page_no=int(payload.get("page_no") or 1),
        item_no=int(payload.get("item_no") or 0),
        depth=int(payload.get("depth") or 2),
        discovered_from=payload.get("discovered_from") or "rel_next",
        title=payload.get("title") or "",
        enabled=payload.get("enabled", True),
    )
    db.add(item)
    db.flush()
    return item
