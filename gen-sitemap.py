#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
himd.com sitemap 生成器（替换原 incremental_sitemap_update.py）

输出：
  sitemap.xml / baidu-sitemap.xml / m-sitemap.xml / m-baidu-sitemap.xml / sitemap.txt / m-sitemap.txt
  sitemap_bd_s_37.xml（m 站高质量版，40,000 条，按下面的区块顺序从尾部截断）

sitemap_bd_s_37.xml 的区块顺序：
  1. 首页
  2. 各分类页（categories.status = 1 且 type<>3，未被 Astro 页覆盖；空分类不收）
  3. Astro 已发布页（读服务器 nginx 的 location = /slug alias，即线上实际直出清单）
  4. 健康热点 /health-discover 论文解读（categories.id = 391）
  5. 健康新闻 /health-news（categories.id = 392）
  6. 科普文章 /article（categories.id = 394，按日期倒序补足到 40,000 条）

其余 6 个文件的旧逻辑不变：
  首页 → 各分类页 → Astro 页 → archives 按 id 倒序。

限制：单文件 ≤ 50000 条，且 ≤ 10MB（百度规则，按 UTF-8 字节数控制在 9.9MB 以内）。
站点：www.himd.com（CMS 站），m.himd.com（移动同站）。Astro 页由 nginx alias 直出。

sitemap_bd_s_37.xml 的 Astro 输入：
  Astro 段以服务器 nginx 的 alias 为准（当前 43 条），不依赖本地 himd-astro 构建，
  换机器也能直接跑。若服务器上存在 /tmp/sitemap-astro.xml，只用它覆盖同 slug 的
  lastmod（更准），缺失或解析失败都不影响出文件。

本文件只存本地仓库（deploy/），不在服务器留副本、不进宝塔计划任务。
运行时把脚本经 ssh 喂给远端 python，源文件不落盘：

  # 预演（只写服务器 /tmp，不动线上文件）
  ssh -i ~/.ssh/himd_old_deploy_ed25519 root@47.100.54.41 \
      'python3 - --dry-run' < deploy/gen-sitemap.py

  # 正式生成（写入 public/，原子替换；可重复执行）
  ssh -i ~/.ssh/himd_old_deploy_ed25519 root@47.100.54.41 \
      'python3 -' < deploy/gen-sitemap.py

需要定时刷新又不走宝塔时，用 systemd timer 调用上面同一条命令的远端版本（需另行把脚本同步到服务器）。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote

SITE_ROOT = "/www/wwwroot/himd.com"
PUBLIC_DIR = os.path.join(SITE_ROOT, "public")
CONFIG_FILE = os.path.join(SITE_ROOT, "config.json")
WWW = "https://www.himd.com"
MOBILE = "https://m.himd.com"

ASTRO_CONFS = [
    "/www/server/panel/vhost/nginx/extension/himd_com/himd-diseases.conf",
    "/www/server/panel/vhost/nginx/extension/himd_com/himd-drug-osimertinib.conf",
    "/www/server/panel/vhost/rewrite/go_himd_com.conf",
]

MAX_URLS = 40000
MAX_BYTES = 9900000  # 留余量，百度上限 10485760


def mysql_rows(sql):
    """用 mysql CLI 查询，返回 list[list[str]]。密码来自 config.json，不进命令行。"""
    cfg = json.load(open(CONFIG_FILE))["mysql"]
    env = dict(os.environ, MYSQL_PWD=cfg["password"])
    cmd = [
        "/usr/bin/mysql", "-u", cfg["user"], cfg["database"],
        "-N", "-B", "--default-character-set=utf8mb4", "-e", sql,
    ]
    out = subprocess.check_output(cmd, env=env)
    rows = []
    for line in out.decode("utf-8").splitlines():
        if line.strip() == "":
            continue
        rows.append(line.split("\t"))
    return rows


def day(ts):
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def pick_day(*stamps):
    """取第一个有效时间戳（NULL/0 视为无效）转成日期。"""
    for ts in stamps:
        try:
            if int(ts) > 0:
                return day(ts)
        except (TypeError, ValueError):
            continue
    return ""


def load_categories():
    """分类页 + 归档 URL 所需的 token 映射。

    lastmod：分类自身 updated/created 优先，缺失时用该分类最新文章时间兜底，
    与 load_m_categories 同规则（避免同一分类在旧 6 件套里没日期）。
    """
    rows = mysql_rows(
        "SELECT c.id, c.url_token, c.status, "
        "COALESCE(NULLIF(c.updated_time,0), c.created_time, f.ts) "
        "FROM categories c "
        "LEFT JOIN ("
        "  SELECT a.category_id, MAX(COALESCE(NULLIF(a.updated_time,0), a.created_time)) ts "
        "  FROM archives a "
        "  WHERE a.category_id IN ("
        "    SELECT id FROM categories WHERE status=1 AND type<>3 "
        "    AND COALESCE(NULLIF(updated_time,0), created_time) IS NULL"
        "  ) GROUP BY a.category_id"
        ") f ON f.category_id=c.id "
        "ORDER BY c.id ASC"
    )
    cats, token_map = [], {}
    for cid, token, status, ts in rows:
        token = token.rstrip("/")
        if not token.strip():
            continue
        # 注意：token 首尾空格是线上 URL 的一部分（如 " foretinib" 实际是 /%20foretinib），
        # 不能 strip，只能按 URL 规则编码。
        token_map[int(cid)] = token
        if status == "1":
            cats.append((quote("/" + token, safe="/"), pick_day(ts), "weekly", "0.9"))
    return cats, token_map


def load_archives(limit, token_map):
    rows = mysql_rows(
        "SELECT id, category_id, updated_time, created_time, fixed_link FROM archives "
        "ORDER BY id DESC LIMIT %d" % limit
    )
    out = []
    for aid, cid, updated, created, fixed in rows:
        if fixed:
            out.append((quote(fixed, safe="/"), pick_day(updated, created), "daily", "0.8"))
            continue
        token = token_map.get(int(cid or 0), "")
        if not token.strip():
            continue
        out.append((quote("/%s/%s" % (token, aid), safe="/"), pick_day(updated, created), "daily", "0.8"))
    return out


def load_astro_pages():
    """从 nginx 精准匹配 = /slug 中取当前真正上线的 Astro 页面。"""
    pages = {}
    for conf in ASTRO_CONFS:
        if not os.path.exists(conf):
            continue
        text = open(conf, encoding="utf-8", errors="ignore").read()
        for m in re.finditer(r"location\s*=\s*/([A-Za-z0-9_-]+)\s*\{(.*?)\}", text, re.S):
            slug, block = m.group(1), m.group(2)
            am = re.search(r"alias\s+([^;]+);", block)
            if not am:
                continue
            path = am.group(1).strip()
            lastmod = day(os.path.getmtime(path)) if os.path.exists(path) else ""
            pages[slug] = lastmod
    return [("/" + slug, lastmod, "monthly", "0.9") for slug, lastmod in sorted(pages.items())]


def load_m_categories():
    """m 站分类区块：只收有时间的分类页；无时间的用该分类最新文章时间兜底。

    返回 [(path, lastmod, id)]，已按 id 升序。
    """
    rows = mysql_rows(
        "SELECT c.id, c.url_token, "
        "COALESCE(NULLIF(c.updated_time,0), c.created_time, f.ts) "
        "FROM categories c "
        "LEFT JOIN ("
        "  SELECT a.category_id, MAX(COALESCE(NULLIF(a.updated_time,0), a.created_time)) ts "
        "  FROM archives a "
        "  WHERE a.category_id IN ("
        "    SELECT id FROM categories WHERE status=1 AND type<>3 "
        "    AND COALESCE(NULLIF(updated_time,0), created_time) IS NULL"
        "  ) GROUP BY a.category_id"
        ") f ON f.category_id=c.id "
        "WHERE c.status=1 AND c.type<>3 ORDER BY c.id ASC"
    )
    out = []
    for cid, token, ts in rows:
        token = token.rstrip("/")
        lastmod = pick_day(ts)
        if not token.strip() or not lastmod:
            continue
        out.append((quote("/" + token, safe="/"), lastmod, int(cid)))
    return out


def load_category_articles(cid, prefix, limit=None):
    """按分类取文章页：优先 fixed_link，否则 /<prefix>/<id>；按日期倒序。"""
    sql = (
        "SELECT id, COALESCE(NULLIF(updated_time,0), created_time), "
        "COALESCE(fixed_link, '') FROM archives WHERE category_id=%d "
        "ORDER BY 2 DESC, id DESC" % int(cid)
    )
    if limit:
        sql += " LIMIT %d" % int(limit)
    out = []
    for aid, ts, fixed in mysql_rows(sql):
        path = fixed if fixed.startswith("/") else "/%s/%s" % (prefix, aid)
        out.append((path, pick_day(ts), int(aid)))
    return out


def load_astro_xml(path):
    """可选：读 Astro 构建产物 sitemap-astro.xml，返回 [(path, lastmod)]。

    仅用于给 nginx alias 清单覆盖 lastmod，缺失/损坏都返回空列表。
    """
    if not path or not os.path.exists(path):
        return []
    import xml.etree.ElementTree as ET

    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    try:
        root = ET.parse(path).getroot()
    except Exception as exc:
        print("warn: %s 解析失败（%s），按 alias mtime 取 lastmod" % (path, exc))
        return []
    out, seen = [], set()
    for url in root.findall(ns + "url"):
        loc = (url.findtext(ns + "loc") or "").strip()
        lastmod = (url.findtext(ns + "lastmod") or "").strip()[:10]
        if not loc.startswith(WWW + "/"):
            continue
        path = loc[len(WWW):]
        if path in seen:
            continue
        seen.add(path)
        out.append((path, lastmod))
    return out


def render_xml_mobile(entries, base=MOBILE):
    """sitemap_bd_s_37.xml 的最小格式：loc + mobile:mobile + lastmod。"""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
        ' xmlns:mobile="http://www.baidu.com/schemas/sitemap-mobile/1/">',
    ]
    for path, lastmod in entries:
        parts.append("<url>")
        parts.append("  <loc>%s%s</loc>" % (base, path))
        parts.append('  <mobile:mobile type="mobile"/>')
        if lastmod:
            parts.append("  <lastmod>%s</lastmod>" % lastmod)
        parts.append("</url>")
    parts.append("</urlset>")
    return "\n".join(parts) + "\n"


def build_m_quality_entries(astro_xml_path, target):
    """按 首页 → 分类 → Astro → 论文解读 → 健康新闻 → 科普文章 组装，截断到 target。

    Astro 段以服务器 nginx 的 location = /slug 为准（线上实际直出页）；
    sitemap-astro.xml 只用于覆盖同 slug 的 lastmod，不是必需输入。
    """
    astro = load_astro_pages()
    if not astro:
        print("warn: 未从 nginx 配置读到 Astro 页面，Astro 段为空")
    else:
        overlay = dict(load_astro_xml(astro_xml_path))
        overlaid = 0
        merged_astro = []
        for path, lastmod, cf, pr in astro:
            if overlay.get(path):
                lastmod = overlay[path]
                overlaid += 1
            merged_astro.append((path, lastmod, cf, pr))
        astro = merged_astro
        if overlaid:
            print("info: %d 条 Astro lastmod 取自 %s" % (overlaid, astro_xml_path))
    astro_paths = set(p for p, *_ in astro)

    home = [("/", day(time.time()))]
    cats = [c for c in load_m_categories() if c[0] not in astro_paths]
    cats.sort(key=lambda x: x[2])
    cats.sort(key=lambda x: x[1] or "0000-00-00", reverse=True)
    astro.sort(key=lambda x: x[0])
    astro.sort(key=lambda x: x[1] or "0000-00-00", reverse=True)
    papers = load_category_articles(391, "health-discover")
    news = load_category_articles(392, "health-news")
    papers.sort(key=lambda x: (x[1] or "0000-00-00", x[2]), reverse=True)
    news.sort(key=lambda x: (x[1] or "0000-00-00", x[2]), reverse=True)

    sections = [
        ("home", home),
        ("categories", [(p, d) for p, d, _ in cats]),
        ("astro", [(p, d) for p, d, *_ in astro]),
        ("papers", [(p, d) for p, d, _ in papers]),
        ("news", [(p, d) for p, d, _ in news]),
    ]
    entries, seen, counts = [], set(), {}
    for name, group in sections:
        n = 0
        for path, lastmod in group:
            if len(entries) >= target:
                break
            if path in seen:
                continue
            seen.add(path)
            entries.append((path, lastmod))
            n += 1
        counts[name] = n

    remaining = target - len(entries)
    if remaining > 0:
        popular = load_category_articles(394, "article", limit=remaining + 50)
        popular.sort(key=lambda x: (x[1] or "0000-00-00", x[2]), reverse=True)
        n = 0
        for path, lastmod, _ in popular:
            if len(entries) >= target:
                break
            if path in seen:
                continue
            seen.add(path)
            entries.append((path, lastmod))
            n += 1
        counts["popular"] = n
    else:
        counts["popular"] = 0
    return entries, counts


def render_xml(entries, base, baidu):
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
        + (' xmlns:mobile="http://www.baidu.com/schemas/sitemap-mobile/1/"' if baidu else "")
        + ">",
    ]
    for path, lastmod, cf, pr in entries:
        parts.append("  <url>")
        parts.append("    <loc>%s%s</loc>" % (base, path))
        if baidu:
            parts.append('    <mobile:mobile type="pc,mobile"/>')
        if lastmod:
            parts.append("    <lastmod>%s</lastmod>" % lastmod)
        parts.append("    <changefreq>%s</changefreq>" % cf)
        parts.append("    <priority>%s</priority>" % pr)
        parts.append("  </url>")
    parts.append("</urlset>")
    return "\n".join(parts) + "\n"


def render_txt(entries, base):
    return "".join(base + e[0] + "\n" for e in entries)


def fit(entries, render, max_bytes=MAX_BYTES, max_urls=MAX_URLS):
    """按优先级顺序保留，二分找出条数与字节都合规的最大前缀。"""
    lo, hi, best = 0, min(len(entries), max_urls), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if len(render(entries[:mid]).encode("utf-8")) <= max_bytes:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return entries[:best]


def write_atomic(path, content, mode=0o644):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".sitemap-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, mode)
        try:
            shutil.chown(tmp, "www", "www")  # 与现有文件属主一致，失败不致命
        except (LookupError, PermissionError):
            pass
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-urls", type=int, default=MAX_URLS, help="单文件条数上限，默认 40000")
    ap.add_argument("--m-target", type=int, default=40000, help="sitemap_bd_s_37.xml 目标条数")
    ap.add_argument("--astro-sitemap", default="/tmp/sitemap-astro.xml",
                    help="可选：用 himd-astro 构建产物覆盖同 slug 的 lastmod；缺失不影响生成")
    ap.add_argument("--out-dir", default="/tmp/sitemap-preview")
    args = ap.parse_args()
    if args.m_target <= 0 or args.m_target > MAX_URLS:
        ap.error("--m-target must be 1..%d" % MAX_URLS)

    cats, token_map = load_categories()
    astro = load_astro_pages()
    home = [("", day(time.time()), "always", "1.0")]

    # 同一个 slug 只出现一次：Astro 页在线上覆盖同名分类页，lastmod 以 Astro 为准，
    # 但仍排在分类区块，保证"分类 → Astro → 旧内容"的顺序不变。
    astro_map = dict((a[0], a) for a in astro)
    merged = 0
    new_cats = []
    for c in cats:
        a = astro_map.get(c[0])
        if a:
            merged += 1
            # 保留分类页的排序位，用 Astro 页的 lastmod/频次
            new_cats.append((c[0], a[1], "monthly", "0.9"))
        else:
            new_cats.append(c)
    cats = new_cats
    astro_only = [a for a in astro if a[0] not in set(x[0] for x in cats)]

    seen = set(e[0] for e in home)
    sections = []
    for name, group in (("categories", cats), ("astro", astro_only)):
        uniq = []
        for e in group:
            if e[0] in seen:
                continue
            seen.add(e[0])
            uniq.append(e)
        sections.append((name, uniq))

    fixed = list(home) + [x for _, g in sections for x in g]
    capacity = args.max_urls - len(fixed)
    archives = load_archives(capacity + 1000, token_map)
    archives = [e for e in archives if e[0] not in seen]
    entries = fixed + archives

    out_dir = args.out_dir if args.dry_run else PUBLIC_DIR
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    variants = [
        ("sitemap.xml", WWW, False),
        ("baidu-sitemap.xml", WWW, True),
        ("m-sitemap.xml", MOBILE, False),
        ("m-baidu-sitemap.xml", MOBILE, True),
    ]
    stats = []
    for name, base, baidu in variants:
        kept = fit(entries, lambda es: render_xml(es, base, baidu), max_urls=args.max_urls)
        xml = render_xml(kept, base, baidu)
        write_atomic(os.path.join(out_dir, name), xml, mode=0o755 if name == "sitemap.xml" else 0o644)
        stats.append((name, len(kept), len(xml.encode("utf-8"))))

    for name, base in (("sitemap.txt", WWW), ("m-sitemap.txt", MOBILE)):
        kept = fit(entries, lambda es: render_txt(es, base), max_urls=args.max_urls)
        txt = render_txt(kept, base)
        write_atomic(os.path.join(out_dir, name), txt, mode=0o755)
        stats.append((name, len(kept), len(txt.encode("utf-8"))))

    m_entries, m_counts = build_m_quality_entries(args.astro_sitemap, args.m_target)
    if m_entries is not None:
        m_kept = fit(m_entries, render_xml_mobile, max_urls=args.m_target)
        m_xml = render_xml_mobile(m_kept)
        write_atomic(os.path.join(out_dir, "sitemap_bd_s_37.xml"), m_xml, mode=0o644)
        stats.append(("sitemap_bd_s_37.xml", len(m_kept), len(m_xml.encode("utf-8"))))

    print("output: %s %s" % (out_dir, "(dry-run)" if args.dry_run else ""))
    print("sections: home=%d categories=%d (其中线上 Astro 覆盖 %d) astro_only=%d archives=%d"
          % (len(home), len(sections[0][1]), merged, len(sections[1][1]), len(archives)))
    if m_entries is not None:
        print("m quality: home=%d categories=%d astro=%d papers=%d news=%d popular=%d target=%d"
              % (m_counts["home"], m_counts["categories"], m_counts["astro"],
                 m_counts["papers"], m_counts["news"], m_counts["popular"], args.m_target))
    for name, count, size in stats:
        print("  %-20s %6d 条  %8.2f MB" % (name, count, size / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())