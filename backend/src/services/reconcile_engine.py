"""算法 B｜三向对账（栏目链接 A / sitemap loc B / 实际可达 C）。

分类与严重度（batch-plan 算法 B）：
- `only_in_sitemap_dead`（B 中 404）→ P0
- `only_in_catalog`（A 不在 B）→ P1
- `duplicate_slug`（多个原始 URL 归一后撞车）→ P1
- `redirect_chain`（入口 301）→ P2
- `canonical_mismatch`（rel=canonical 指向别处）→ P2

覆盖率 `|A∩B∩C| / |A|`；不可达率 `|A\\C| / |A|`。

挂载点口径：
- `catalog_items` 每行 = 一个当前挂载位置（同一 canonical 挂在多个栏目/页码时有多行）；
- 对账仍按地址算一份：A 集合、覆盖率、缺陷单都按 canonical 去重；
- 缺陷证据展开该地址的全部当前挂载（`evidence.mounts`），整改时能核对每个栏目/页码；
- 没有挂载位置的地址不会出现在 catalog_items 里（无挂载不算已收录）。

纯函数：输入集合 + 探测结果，输出缺陷与统计，两次调用完全一致。
"""
from __future__ import annotations

from src.constants.enums import CATEGORY_SEVERITY, DefectCategory
from src.services.url_normalize import normalize_url

CATEGORY_TITLES: dict[str, str] = {
    DefectCategory.ONLY_IN_SITEMAP_DEAD.value: "sitemap 指向已下线页面（404）",
    DefectCategory.ONLY_IN_CATALOG.value: "目录已发布但 sitemap 未收录",
    DefectCategory.REDIRECT_CHAIN.value: "入口存在 301 跳转链",
    DefectCategory.CANONICAL_MISMATCH.value: "页面 canonical 与自身 URL 不一致",
    DefectCategory.DUPLICATE_SLUG.value: "同一条目被登记成多个 URL",
}


def _round(value: float) -> float:
    return round(float(value), 4)


def _page_no(item: dict) -> int:
    try:
        return int(item.get("page_no") or 1)
    except (TypeError, ValueError):
        return 1


def _mount_views(items: list[dict]) -> list[dict]:
    """同一 canonical 的全部当前挂载位置（栏目/页码/入口），去重并按确定顺序排序。"""
    mounts: list[dict] = []
    seen: set[tuple] = set()
    for item in items:
        mount = {
            "column": str(item.get("column_name") or ""),
            "column_code": str(item.get("column_code") or ""),
            "page_no": _page_no(item),
            "entry_url": str(item.get("entry_url") or ""),
            "page_url": str(item.get("page_url") or ""),
        }
        key = (mount["column_code"], mount["column"], mount["page_no"], mount["entry_url"], mount["page_url"])
        if key in seen:
            continue
        seen.add(key)
        mounts.append(mount)
    mounts.sort(key=lambda mount: (mount["column_code"], mount["page_no"], mount["entry_url"], mount["column"]))
    return mounts


def _distinct_entry_urls(items: list[dict]) -> list[str]:
    return sorted({str(item.get("entry_url") or "") for item in items if item.get("entry_url")})


def reconcile(
    *,
    site_code: str,
    catalog_items: list[dict],
    sitemap_locs: list[dict],
    probes: dict[str, dict],
) -> dict:
    """返回 {defects, stats, sets}。defects 按 (severity, category, url) 排序。"""
    catalog_map: dict[str, list[dict]] = {}
    for item in catalog_items:
        canonical = normalize_url(item.get("canonical_url") or item.get("entry_url"))
        if not canonical:
            continue
        catalog_map.setdefault(canonical, []).append(item)

    sitemap_map: dict[str, dict] = {}
    for entry in sitemap_locs:
        canonical = normalize_url(entry.get("canonical_url") or entry.get("loc"))
        if canonical and canonical not in sitemap_map:
            sitemap_map[canonical] = entry

    set_a = set(catalog_map)
    set_b = set(sitemap_map)
    set_c = {canonical for canonical in set_a if (probes.get(canonical) or {}).get("status") == 200}

    defects: list[dict] = []
    live_sitemap_extras: list[str] = []

    def push(category: str, canonical: str, *, title: str, summary: str, evidence: dict, column_name: str = "", display_url: str | None = None) -> None:
        defects.append(
            {
                "site_code": site_code,
                "category": category,
                "severity": CATEGORY_SEVERITY[category],
                "fingerprint": f"{category}:{canonical}"[:155],
                "url": (display_url or canonical)[:300],
                "canonical_url": canonical[:300],
                "column_name": column_name[:120],
                "title": title[:200],
                "summary": summary,
                "evidence": evidence,
            }
        )

    # 1) A 有 B 没有 → 收录缺失
    for canonical in sorted(set_a - set_b):
        mounts = _mount_views(catalog_map[canonical])
        first = mounts[0]
        probe = probes.get(canonical) or {}
        where = f"栏目「{first['column'] or '未分类'}」第 {first['page_no']} 页"
        if len(mounts) > 1:
            where = f"{where} 等 {len(mounts)} 处挂载位置"
        push(
            DefectCategory.ONLY_IN_CATALOG.value,
            canonical,
            title=CATEGORY_TITLES[DefectCategory.ONLY_IN_CATALOG.value],
            summary=(
                f"{where}已发布该条目，"
                f"但 sitemap.xml 中没有对应 loc；搜索引擎无法从 sitemap 发现它。"
            ),
            evidence={
                "column": first["column"],
                "page_no": first["page_no"],
                "entry_url": first["entry_url"] or canonical,
                "mounts": mounts,
                "mount_count": len(mounts),
                "in_catalog": True,
                "in_sitemap": False,
                "http_status": probe.get("status"),
                "action": "把该 canonical URL 补进 sitemap.xml，lastmod 用详情页最后更新时间",
            },
            column_name=first["column"],
        )

    # 2) B 有 A 没有 → 死链 / 纯 sitemap 条目
    for canonical in sorted(set_b - set_a):
        probe = probes.get(canonical) or {}
        status = probe.get("status")
        entry = sitemap_map[canonical]
        if status == 200:
            live_sitemap_extras.append(canonical)
            continue
        push(
            DefectCategory.ONLY_IN_SITEMAP_DEAD.value,
            canonical,
            title=CATEGORY_TITLES[DefectCategory.ONLY_IN_SITEMAP_DEAD.value],
            summary=(
                f"sitemap.xml 收录了该 loc，探测返回 HTTP {status}；详情页已下线但 sitemap 未清理，"
                f"搜索引擎会持续抓到 404。"
            ),
            evidence={
                "sitemap_loc": entry.get("loc") or canonical,
                "lastmod": entry.get("lastmod"),
                "source_file": entry.get("source_file") or "sitemap.xml",
                "in_catalog": False,
                "http_status": status,
                "redirect_chain": probe.get("redirect_chain") or [],
                "action": "从 sitemap.xml 删除该 loc，并在栏目页同步撤下入口链接",
            },
        )

    # 3) A ∩ B → 不可达（同样算死链）/ 重定向链 / canonical 口径
    for canonical in sorted(set_a & set_b):
        mounts = _mount_views(catalog_map[canonical])
        first = mounts[0]
        probe = probes.get(canonical) or {}
        status = probe.get("status")
        if status != 200:
            push(
                DefectCategory.ONLY_IN_SITEMAP_DEAD.value,
                canonical,
                title=CATEGORY_TITLES[DefectCategory.ONLY_IN_SITEMAP_DEAD.value],
                summary=(
                    f"该条目同时出现在目录与 sitemap 中，但探测返回 HTTP {status}；"
                    f"目录侧要撤下入口、sitemap 侧要删除 loc。"
                ),
                evidence={
                    "sitemap_loc": sitemap_map[canonical].get("loc") or canonical,
                    "lastmod": sitemap_map[canonical].get("lastmod"),
                    "source_file": sitemap_map[canonical].get("source_file") or "sitemap.xml",
                    "in_catalog": True,
                    "mounts": mounts,
                    "mount_count": len(mounts),
                    "http_status": status,
                    "redirect_chain": probe.get("redirect_chain") or [],
                    "action": "同时从 sitemap.xml 与栏目页撤下该 URL",
                },
                column_name=first["column"],
            )
            continue
        chain = probe.get("redirect_chain") or []
        canonical_tag = probe.get("canonical_tag")
        if chain:
            hops = " → ".join([chain[0]["from"]] + [hop["to"] for hop in chain]) if chain else canonical
            push(
                DefectCategory.REDIRECT_CHAIN.value,
                canonical,
                title=CATEGORY_TITLES[DefectCategory.REDIRECT_CHAIN.value],
                summary=f"入口 {len(chain)} 跳 301 后才到达 {probe.get('final_url')}；sitemap 应直接写最终 URL。",
                evidence={
                    "redirect_chain": chain,
                    "final_url": probe.get("final_url"),
                    "hops": len(chain),
                    "chain_text": hops,
                    "mounts": mounts,
                    "mount_count": len(mounts),
                    "action": "sitemap 与栏目入口统一改成 301 的最终 URL，并保留旧 URL 的 301 至少 6 个月",
                },
                column_name=first["column"],
            )
            continue
        if canonical_tag:
            tag_canonical = normalize_url(canonical_tag)
            if tag_canonical and tag_canonical != canonical:
                push(
                    DefectCategory.CANONICAL_MISMATCH.value,
                    canonical,
                    title=CATEGORY_TITLES[DefectCategory.CANONICAL_MISMATCH.value],
                    summary=f"页面 <link rel=canonical> 指向 {tag_canonical}，与自身 URL 不同，权重会被算到别的页面。",
                    evidence={
                        "page_canonical": tag_canonical,
                        "expected_canonical": canonical,
                        "canonical_tag_raw": canonical_tag,
                        "mounts": mounts,
                        "mount_count": len(mounts),
                        "action": "把 rel=canonical 改成页面自身 canonical URL",
                    },
                    column_name=first["column"],
                )

    # 4) 同一 canonical 被多条原始 URL 登记 → 重复 slug
    for canonical in sorted(set_a):
        raws = catalog_map[canonical]
        if len(raws) < 2:
            continue
        entry_urls = _distinct_entry_urls(raws)
        if len(entry_urls) < 2:
            continue
        mounts = _mount_views(raws)
        push(
            DefectCategory.DUPLICATE_SLUG.value,
            canonical,
            title=CATEGORY_TITLES[DefectCategory.DUPLICATE_SLUG.value],
            summary=f"{len(entry_urls)} 条不同写法的入口指向同一个 canonical URL，sitemap 容易出现重复 loc。",
            evidence={
                "entry_urls": entry_urls,
                "canonical": canonical,
                "mounts": mounts,
                "mount_count": len(mounts),
                "action": "只保留一个入口写法，其余改成 301 指到 canonical",
            },
            column_name=mounts[0]["column"] if mounts else "",
        )

    severity_rank = {"p0": 0, "p1": 1, "p2": 2}
    defects.sort(key=lambda item: (severity_rank.get(item["severity"], 9), item["category"], item["url"]))

    catalog_total = len(set_a)
    sitemap_total = len(set_b)
    hit = len(set_a & set_b & set_c)
    coverage = _round(hit / catalog_total) if catalog_total else 0.0
    unreachable = len(set_a - set_c)
    unreachable_rate = _round(unreachable / catalog_total) if catalog_total else 0.0
    dead_count = sum(1 for defect in defects if defect["category"] == DefectCategory.ONLY_IN_SITEMAP_DEAD.value)
    sitemap_dead_rate = _round(dead_count / sitemap_total) if sitemap_total else 0.0

    stats = {
        "catalog_total": catalog_total,
        "sitemap_total": sitemap_total,
        "reachable": len(set_c),
        "catalog_in_sitemap": len(set_a & set_b),
        "hit": hit,
        "coverage": coverage,
        "unreachable": unreachable,
        "unreachable_rate": unreachable_rate,
        "dead": dead_count,
        "sitemap_dead_rate": sitemap_dead_rate,
        "sitemap_extra_live": len(live_sitemap_extras),
        "duplicates": sum(1 for canonical in set_a if len(_distinct_entry_urls(catalog_map[canonical])) > 1),
        "p0": sum(1 for defect in defects if defect["severity"] == "p0"),
        "p1": sum(1 for defect in defects if defect["severity"] == "p1"),
        "p2": sum(1 for defect in defects if defect["severity"] == "p2"),
        "by_category": {
            category.value: sum(1 for defect in defects if defect["category"] == category.value)
            for category in DefectCategory
        },
    }
    return {
        "defects": defects,
        "stats": stats,
        "sets": {
            "only_in_catalog": sorted(set_a - set_b),
            "only_in_sitemap": sorted(set_b - set_a),
            "reachable": sorted(set_c),
            "sitemap_extra_live": live_sitemap_extras,
        },
    }
