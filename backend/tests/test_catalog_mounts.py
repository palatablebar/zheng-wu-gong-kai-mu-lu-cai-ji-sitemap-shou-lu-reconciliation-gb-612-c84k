"""挂载点口径：同一文件挂多个栏目 → 对账按地址算一份、证据/明细展开全部当前挂载；
重跑只保留本次仍存在的位置（不误删、不重复）；无挂载文件不算已收录。
"""
from __future__ import annotations

import shutil

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from src.constants.demo_data import SITES
from src.models import Base
from src.models.catalog_mount import CatalogMount
from src.models.crawl_run import CrawlRun
from src.models.defect import Defect
from src.models.site import Site
from src.models.sitemap_entry import SitemapEntry
from src.services import catalog_service, crawl_run_service, fixture_site, reconcile_engine

HOME = "https://e2e.gov.cn"
DOC1 = f"{HOME}/xxgk/alpha/doc-001.html"
DOC2 = f"{HOME}/xxgk/alpha/doc-002.html"


def _db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def _mount_rows(db, site_id):
    return db.scalars(
        select(CatalogMount).where(CatalogMount.site_id == site_id).order_by(CatalogMount.id)
    ).all()


def _mount(db, site_id, canonical, column_code, page_no, **extra):
    row = {
        "canonical_url": canonical,
        "column_code": column_code,
        "column_name": extra.pop("column_name", column_code),
        "page_no": page_no,
        "page_url": f"{HOME}/xxgk/{column_code}/index.html",
        "entry_url": canonical,
        "title": "文件",
    }
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# 算法 B：对账按地址算一份，证据展开全部当前挂载
# ---------------------------------------------------------------------------


def test_reconcile_counts_address_once_and_expands_mounts():
    items = [
        {"entry_url": DOC1, "canonical_url": DOC1, "column_name": "政策文件", "column_code": "zcwj", "page_no": 3},
        {"entry_url": DOC1, "canonical_url": DOC1, "column_name": "重大项目", "column_code": "zdxm", "page_no": 1},
    ]
    result = reconcile_engine.reconcile(
        site_code="t", catalog_items=items, sitemap_locs=[], probes={DOC1: {"status": 200}}
    )
    # 对账仍按地址算一份
    assert result["stats"]["catalog_total"] == 1
    assert len(result["defects"]) == 1
    defect = result["defects"][0]
    assert defect["category"] == "only_in_catalog"
    # 证据展开全部当前挂载（按栏目代码排序，确定性）
    mounts = defect["evidence"]["mounts"]
    assert [(m["column_code"], m["page_no"]) for m in mounts] == [("zcwj", 3), ("zdxm", 1)]
    assert defect["evidence"]["mount_count"] == 2
    # 兼容字段取排序后的首个挂载
    assert defect["evidence"]["column"] == "政策文件"
    assert defect["evidence"]["page_no"] == 3
    assert defect["column_name"] == "政策文件"
    assert "2 处挂载位置" in defect["summary"]


def test_reconcile_mount_order_is_input_independent():
    first = {"entry_url": DOC1, "canonical_url": DOC1, "column_name": "政策文件", "column_code": "zcwj", "page_no": 3}
    second = {"entry_url": DOC1, "canonical_url": DOC1, "column_name": "重大项目", "column_code": "zdxm", "page_no": 1}
    forward = reconcile_engine.reconcile(
        site_code="t", catalog_items=[first, second], sitemap_locs=[], probes={DOC1: {"status": 200}}
    )
    backward = reconcile_engine.reconcile(
        site_code="t", catalog_items=[second, first], sitemap_locs=[], probes={DOC1: {"status": 200}}
    )
    assert forward == backward


def test_reconcile_multi_mount_same_entry_is_not_duplicate():
    """同一地址挂在两个栏目（入口写法相同）→ 不是重复 slug，duplicates 仍为 0。"""
    items = [
        {"entry_url": DOC1, "canonical_url": DOC1, "column_name": "栏目甲", "column_code": "alpha", "page_no": 1},
        {"entry_url": DOC1, "canonical_url": DOC1, "column_name": "栏目乙", "column_code": "beta", "page_no": 2},
    ]
    result = reconcile_engine.reconcile(
        site_code="t", catalog_items=items, sitemap_locs=[], probes={DOC1: {"status": 200}}
    )
    assert result["stats"]["duplicates"] == 0
    assert all(defect["category"] != "duplicate_slug" for defect in result["defects"])


# ---------------------------------------------------------------------------
# 挂载点校准：重跑仅保留本次仍存在的位置，不新增重复、不误删仍存在
# ---------------------------------------------------------------------------


def test_sync_mounts_idempotent_no_duplicate_records():
    db = _db()
    site = Site(code="s1", name="s1", home_url="https://s1.gov.cn")
    db.add(site)
    db.flush()
    mounts = [_mount(db, site.id, DOC1, "alpha", 1), _mount(db, site.id, DOC1, "beta", 1)]
    first = catalog_service.sync_mounts(db, site.id, mounts)
    assert first == {"added": 2, "updated": 0, "removed": 0, "total": 2}
    again = catalog_service.sync_mounts(db, site.id, mounts)
    assert again == {"added": 0, "updated": 0, "removed": 0, "total": 2}
    assert db.scalar(select(func.count()).select_from(CatalogMount)) == 2


def test_sync_mounts_prunes_disappeared_and_keeps_survivors():
    db = _db()
    site = Site(code="s1", name="s1", home_url="https://s1.gov.cn")
    db.add(site)
    db.flush()
    catalog_service.sync_mounts(
        db, site.id, [_mount(db, site.id, DOC1, "alpha", 1), _mount(db, site.id, DOC1, "beta", 1)]
    )
    ids_before = {(row.canonical_url, row.column_code): row.id for row in _mount_rows(db, site.id)}

    stats = catalog_service.sync_mounts(db, site.id, [_mount(db, site.id, DOC1, "alpha", 1)])
    assert stats["removed"] == 1
    assert stats["added"] == 0
    rows = _mount_rows(db, site.id)
    assert [(row.canonical_url, row.column_code) for row in rows] == [(DOC1, "alpha")]
    # 仍存在的位置保留原行（不是删了重建）
    assert rows[0].id == ids_before[(DOC1, "alpha")]


def test_sync_mounts_scoped_per_site():
    db = _db()
    site_a = Site(code="sa", name="sa", home_url="https://sa.gov.cn")
    site_b = Site(code="sb", name="sb", home_url="https://sb.gov.cn")
    db.add_all([site_a, site_b])
    db.flush()
    catalog_service.sync_mounts(db, site_a.id, [_mount(db, site_a.id, DOC1, "alpha", 1)])
    catalog_service.sync_mounts(db, site_b.id, [_mount(db, site_b.id, DOC2, "alpha", 1)])

    # A 站重跑后 B 站挂载原样保留（不误删其他站点的位置）
    catalog_service.sync_mounts(db, site_a.id, [])
    assert _mount_rows(db, site_a.id) == []
    assert len(_mount_rows(db, site_b.id)) == 1


def test_sync_mounts_updates_in_place():
    db = _db()
    site = Site(code="s1", name="s1", home_url="https://s1.gov.cn")
    db.add(site)
    db.flush()
    catalog_service.sync_mounts(db, site.id, [_mount(db, site.id, DOC1, "alpha", 1, title="旧标题")])
    row_id = _mount_rows(db, site.id)[0].id
    stats = catalog_service.sync_mounts(db, site.id, [_mount(db, site.id, DOC1, "alpha", 1, title="新标题")])
    assert stats == {"added": 0, "updated": 1, "removed": 0, "total": 1}
    row = _mount_rows(db, site.id)[0]
    assert row.id == row_id
    assert row.title == "新标题"


def test_unmounted_file_is_not_counted_as_cataloged():
    """挂载全部消失后，该地址不再进入对账 A 集合（无挂载不算已收录）。"""
    db = _db()
    site = Site(code="s1", name="s1", home_url="https://s1.gov.cn")
    db.add(site)
    db.flush()
    catalog_service.sync_mounts(
        db, site.id, [_mount(db, site.id, DOC1, "alpha", 1), _mount(db, site.id, DOC2, "alpha", 2)]
    )
    catalog_service.sync_mounts(db, site.id, [_mount(db, site.id, DOC1, "alpha", 1)])
    payload = crawl_run_service._catalog_payload(db, site.id)
    assert {row["canonical_url"] for row in payload} == {DOC1}


# ---------------------------------------------------------------------------
# 算法 A → 挂载点：同一位置只留一条，跨栏目位置都保留
# ---------------------------------------------------------------------------


def test_mounts_from_crawl_dedupes_same_position_keeps_cross_column():
    crawl = {
        "links": [
            {"canonical": DOC1, "column_url": f"{HOME}/xxgk/alpha/", "column_name": "栏目甲", "page_no": 1,
             "page_url": f"{HOME}/xxgk/alpha/index.html", "raw": "doc-001.html", "title": "文件一", "discovered_from": "home_link"},
            {"canonical": DOC1, "column_url": f"{HOME}/xxgk/alpha/", "column_name": "栏目甲", "page_no": 1,
             "page_url": f"{HOME}/xxgk/alpha/index.html", "raw": "doc-001.html?utm_source=wechat", "title": "文件一", "discovered_from": "home_link"},
            {"canonical": DOC1, "column_url": f"{HOME}/xxgk/beta/", "column_name": "栏目乙", "page_no": 2,
             "page_url": f"{HOME}/xxgk/beta/index_2.html", "raw": "/xxgk/alpha/doc-001.html", "title": "文件一", "discovered_from": "rel_next"},
        ]
    }
    mounts = crawl_run_service._mounts_from_crawl(crawl)
    assert [(m["canonical_url"], m["column_code"], m["page_no"]) for m in mounts] == [
        (DOC1, "alpha", 1),
        (DOC1, "beta", 2),
    ]


# ---------------------------------------------------------------------------
# 端到端：巡检落库挂载点，重跑幂等，撤下即删、无挂载即不算已收录
# ---------------------------------------------------------------------------


def _write(root, relative, html):
    target = root / relative.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")


def build_e2e_fixture(root, *, beta_lists_doc1: bool, alpha_pages: int) -> None:
    """alpha 栏目第 1 页挂 doc-001（第 2 页挂 doc-002）；beta 栏目可选挂 doc-001。"""
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    _write(root, "/index.html", (
        "<html><body>"
        f'<a href="{HOME}/xxgk/alpha/index.html">栏目甲</a>'
        f'<a href="{HOME}/xxgk/beta/index.html">栏目乙</a>'
        "</body></html>"
    ))
    rel_next = '<link rel="next" href="index_2.html" />' if alpha_pages > 1 else ""
    _write(root, "/xxgk/alpha/index.html", (
        f"<html><head>{rel_next}</head><body>"
        '<a href="doc-001.html">文件一</a>'
        "</body></html>"
    ))
    if alpha_pages > 1:
        _write(root, "/xxgk/alpha/index_2.html", (
            "<html><body><a href=\"doc-002.html\">文件二</a></body></html>"
        ))
    beta_item = f'<a href="/xxgk/alpha/doc-001.html">文件一（转载）</a>' if beta_lists_doc1 else ""
    _write(root, "/xxgk/beta/index.html", f"<html><body>{beta_item}</body></html>")
    _write(root, "/xxgk/alpha/doc-001.html", "<html><body>文件一</body></html>")
    _write(root, "/xxgk/alpha/doc-002.html", "<html><body>文件二</body></html>")


def _run(db, site, run_no):
    run = CrawlRun(run_no=run_no, trigger="manual", status="queued", operator_email="t@e2e.io")
    db.add(run)
    db.flush()
    crawl_run_service.execute_run(db, run, [site])
    assert run.status == "success", run.log_text
    return run


def test_execute_run_tracks_mounts_across_reruns(tmp_path, monkeypatch):
    monkeypatch.setattr(fixture_site, "FIXTURE_BASE", tmp_path)
    root = tmp_path / "e2e-site"
    build_e2e_fixture(root, beta_lists_doc1=True, alpha_pages=2)
    db = _db()
    site = Site(code="e2e-site", name="E2E 站", home_url=HOME, page_size=12, max_depth=3)
    db.add(site)
    db.flush()

    # 第一次巡检：doc-001 挂在 alpha+beta 两个栏目，doc-002 挂在 alpha 第 2 页
    run1 = _run(db, site, "T0001")
    rows = _mount_rows(db, site.id)
    assert {(row.canonical_url, row.column_code, row.page_no) for row in rows} == {
        (DOC1, "alpha", 1),
        (DOC1, "beta", 1),
        (DOC2, "alpha", 2),
    }
    assert run1.catalog_count == 2  # 对账按地址算一份
    assert run1.metrics["sites"][0]["mounts"] == 3
    defects1 = db.scalars(select(Defect).where(Defect.run_id == run1.id)).all()
    assert len(defects1) == 2
    doc1_defect = next(d for d in defects1 if d.canonical_url == DOC1)
    assert {(m["column_code"], m["page_no"]) for m in doc1_defect.evidence["mounts"]} == {
        ("alpha", 1),
        ("beta", 1),
    }

    # 重跑：不新增重复记录、不误删仍存在的位置
    ids_before = {row.id for row in _mount_rows(db, site.id)}
    run2 = _run(db, site, "T0002")
    assert run2.metrics["sites"][0]["mounts_added"] == 0
    assert run2.metrics["sites"][0]["mounts_removed"] == 0
    assert {row.id for row in _mount_rows(db, site.id)} == ids_before

    # beta 栏目撤下 doc-001：只删这一个挂载点，alpha 的保留
    build_e2e_fixture(root, beta_lists_doc1=False, alpha_pages=2)
    run3 = _run(db, site, "T0003")
    assert run3.metrics["sites"][0]["mounts_removed"] == 1
    rows3 = _mount_rows(db, site.id)
    assert {(row.canonical_url, row.column_code, row.page_no) for row in rows3} == {
        (DOC1, "alpha", 1),
        (DOC2, "alpha", 2),
    }
    doc1_defect3 = next(d for d in db.scalars(select(Defect).where(Defect.run_id == run3.id)) if d.canonical_url == DOC1)
    assert [(m["column_code"], m["page_no"]) for m in doc1_defect3.evidence["mounts"]] == [("alpha", 1)]

    # alpha 第 2 页也撤下：doc-002 无挂载 → 不再算已收录
    build_e2e_fixture(root, beta_lists_doc1=False, alpha_pages=1)
    run4 = _run(db, site, "T0004")
    assert run4.catalog_count == 1
    assert {(row.canonical_url, row.column_code, row.page_no) for row in _mount_rows(db, site.id)} == {
        (DOC1, "alpha", 1)
    }
    assert {d.canonical_url for d in db.scalars(select(Defect).where(Defect.run_id == run4.id))} == {DOC1}


def test_execute_run_matches_seed_acceptance(tmp_path, monkeypatch):
    """demo 三站全量巡检：挂载口径下数量与 batch-plan 验收值一致，重跑幂等。"""
    monkeypatch.setattr(fixture_site, "FIXTURE_BASE", tmp_path)
    for spec in SITES:
        fixture_site.build_site(spec, fixture_site.fixture_root(spec["code"], base=tmp_path))
    db = _db()
    sites = []
    for spec in SITES:
        site = Site(
            code=spec["code"],
            name=spec["name"],
            home_url=spec["home_url"],
            sitemap_url=spec["sitemap_url"],
            page_size=12,
            max_depth=3,
        )
        db.add(site)
        sites.append(site)
    db.flush()
    for spec, site in zip(SITES, sites, strict=True):
        for row in fixture_site.sitemap_rows(spec):
            db.add(
                SitemapEntry(
                    site_id=site.id,
                    loc=row["loc"],
                    canonical_url=row["canonical_url"],
                    lastmod=row["lastmod"],
                    changefreq=row["changefreq"],
                    priority=row["priority"],
                    source_file=row["source_file"],
                )
            )
    db.flush()

    run = CrawlRun(run_no="SEED-1", trigger="seed", status="queued", operator_email="seed@e2e.io")
    db.add(run)
    db.flush()
    crawl_run_service.execute_run(db, run, sites)
    assert run.status == "success", run.log_text
    assert run.catalog_count == 360
    assert run.sitemap_count == 362
    assert run.defect_count == 31
    assert (run.p0_count, run.p1_count, run.p2_count) == (14, 12, 5)
    assert run.coverage == 0.9667
    assert db.scalar(select(func.count()).select_from(CatalogMount)) == 360

    run2 = CrawlRun(run_no="SEED-2", trigger="manual", status="queued", operator_email="seed@e2e.io")
    db.add(run2)
    db.flush()
    crawl_run_service.execute_run(db, run2, sites)
    assert run2.status == "success", run2.log_text
    assert run2.metrics["totals"]["mounts_added"] == 0
    assert run2.metrics["totals"]["mounts_removed"] == 0
    assert db.scalar(select(func.count()).select_from(CatalogMount)) == 360
    assert run2.defect_count == 31
