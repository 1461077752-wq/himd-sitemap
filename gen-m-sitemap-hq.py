#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
m.himd.com 高质量 sitemap（/sitemap_bd_s_37.xml）

固定区块顺序（前面的优先保留，尾部按目标条数截断）：
  1. 首页
  2. CMS 分类页（categories.status = 1，且未被 Astro 页覆盖）
  3. Astro 已发布页（疾病页 + 药品页，含覆盖同名分类页的 URL）
  4. 「健康热点 /health-discover」论文解读文章（categories.id = 391）
  5. 「健康新闻 /health-news」文章（categories.id = 392）
  6. 「科普文章 /article」文章（categories.id = 394）

默认写到 40000 条；区块内按 lastmod 从新到旧排序；同一 URL 只出现一次。
不收录疾病/药品问答等其余 CMS 内容；无时间且无文章的空分类页也不收录。

数据准备（采集端，只读）：
  ssh -i ~/.ssh/himd_old_deploy_ed25519 root@47.100.54.41 \
      'cd /www/wwwroot/himd.com && python3 -' > /tmp/categories.tsv <<'PY'
  import json, subprocess, os
  cfg = json.load(open("config.json"))["mysql"]
  env = dict(os.environ, MYSQL_PWD=cfg["password"])
  sql = ("SELECT c.id, c.url_token, "
         "COALESCE(NULLIF(c.updated_time,0), c.created_time, f.ts), c.status "
         "FROM categories c "
         "LEFT JOIN ("
         "  SELECT a.category_id, MAX(COALESCE(NULLIF(a.updated_time,0), a.created_time)) ts "
         "  FROM archives a "
         "  WHERE a.category_id IN (SELECT id FROM categories WHERE status=1 AND type<>3 "
         "    AND COALESCE(NULLIF(updated_time,0), created_time) IS NULL) "
         "  GROUP BY a.category_id"
         ") f ON f.category_id=c.id "
         "WHERE c.status=1 AND c.type<>3 ORDER BY c.id ASC")
  out = subprocess.check_output(["/usr/bin/mysql", "-u", cfg["user"], cfg["database"],
                                 "-N", "-B", "--default-character-set=utf8mb4", "-e", sql], env=env)
  print(out.decode(), end="")
  PY

  ssh -i ~/.ssh/himd_old_deploy_ed25519 root@47.100.54.41 \
      'cd /www/wwwroot/himd.com && python3 -' > /tmp/health-discover.tsv <<'PY'
  import json, subprocess, os
  cfg = json.load(open("config.json"))["mysql"]
  env = dict(os.environ, MYSQL_PWD=cfg["password"])
  sql = ("SELECT id, COALESCE(NULLIF(updated_time,0), created_time), COALESCE(fixed_link, '') "
         "FROM archives WHERE category_id=391 ORDER BY 2 DESC, id DESC")
  out = subprocess.check_output(["/usr/bin/mysql", "-u", cfg["user"], cfg["database"],
                                 "-N", "-B", "--default-character-set=utf8mb4", "-e", sql], env=env)
  print(out.decode(), end="")
  PY

  # 健康新闻（id=392）与科普文章（id=394）；科普只取足够填满 4 万条的前 19200 条。
  ssh -i ~/.ssh/himd_old_deploy_ed25519 root@47.100.54.41 \
      'cd /www/wwwroot/himd.com && python3 -' > /tmp/health-news.tsv <<'PY'
  import json, subprocess, os
  cfg = json.load(open("config.json"))["mysql"]
  env = dict(os.environ, MYSQL_PWD=cfg["password"])
  sql = ("SELECT id, COALESCE(NULLIF(updated_time,0), created_time), COALESCE(fixed_link, '') "
         "FROM archives WHERE category_id=392 ORDER BY 2 DESC, id DESC")
  out = subprocess.check_output(["/usr/bin/mysql", "-u", cfg["user"], cfg["database"],
                                 "-N", "-B", "--default-character-set=utf8mb4", "-e", sql], env=env)
  print(out.decode(), end="")
  PY

  ssh -i ~/.ssh/himd_old_deploy_ed25519 root@47.100.54.41 \
      'cd /www/wwwroot/himd.com && python3 -' > /tmp/popular.tsv <<'PY'
  import json, subprocess, os
  cfg = json.load(open("config.json"))["mysql"]
  env = dict(os.environ, MYSQL_PWD=cfg["password"])
  sql = ("SELECT id, COALESCE(NULLIF(updated_time,0), created_time), COALESCE(fixed_link, '') "
         "FROM archives WHERE category_id=394 ORDER BY 2 DESC, id DESC LIMIT 19200")
  out = subprocess.check_output(["/usr/bin/mysql", "-u", cfg["user"], cfg["database"],
                                 "-N", "-B", "--default-character-set=utf8mb4", "-e", sql], env=env)
  print(out.decode(), end="")
  PY

用法：
  python3 deploy/gen-m-sitemap-hq.py \
      --categories-tsv /tmp/categories.tsv \
      --articles-tsv /tmp/health-discover.tsv \
      --news-tsv /tmp/health-news.tsv \
      --popular-tsv /tmp/popular.tsv \
      --astro-sitemap himd-astro/dist/sitemap-astro.xml \
      --target-total 40000 \
      --out /tmp/sitemap_bd_s_37.xml

输出为百度移动 sitemap（含 mobile:mobile），单文件 ≤ 50000 条且 ≤ 10MB。
"""
import argparse
import datetime as dt
import os
import re
import sys
from urllib.parse import quote

BASE = "https://m.himd.com"
MAX_URLS = 50000
MAX_BYTES = 9900000


def day(ts):
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def load_astro(path):
    text = open(path, encoding="utf-8").read()
    out = []
    seen = set()
    for m in re.finditer(r"<loc>([^<]+)</loc>(?:\s*<lastmod>([^<]+)</lastmod>)?", text):
        url = m.group(1).strip()
        lastmod = (m.group(2) or "").strip()
        if not url.startswith("https://www.himd.com/"):
            continue
        path_only = url[len("https://www.himd.com"):]
        if path_only in seen:
            continue
        seen.add(path_only)
        out.append((path_only, lastmod))
    return out


def load_categories(path):
    out = []
    seen = set()
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            raise SystemExit("bad category TSV line: %r" % line)
        cid, token = parts[0], parts[1].rstrip("/")
        ts = parts[2] if len(parts) > 2 else ""
        if not token.strip():
            continue
        p = quote("/" + token, safe="/")
        if p in seen:
            raise SystemExit("duplicate category path: %s" % p)
        seen.add(p)
        out.append((p, day(ts), int(cid)))
    return out


def load_articles(path, prefix):
    out = []
    seen = set()
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            raise SystemExit("bad TSV line: %r" % line)
        aid, ts = parts[0], parts[1]
        fixed = parts[2].strip() if len(parts) > 2 else ""
        page = fixed if fixed.startswith("/") else "%s/%s" % (prefix, aid)
        if page in seen:
            raise SystemExit("duplicate article path: %s" % page)
        seen.add(page)
        out.append((page, day(ts), int(aid)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--categories-tsv", required=True)
    ap.add_argument("--articles-tsv", required=True)
    ap.add_argument("--news-tsv")
    ap.add_argument("--popular-tsv")
    ap.add_argument("--astro-sitemap", required=True)
    ap.add_argument("--target-total", type=int, default=40000)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if args.target_total <= 0 or args.target_total > MAX_URLS:
        raise SystemExit("target-total must be 1..%d" % MAX_URLS)

    astro = load_astro(args.astro_sitemap)
    astro_paths = set(p for p, _ in astro)
    # 无分类时间且分类下无文章的空分类页不收录（lastmod 为空即此类）。
    cats = [c for c in load_categories(args.categories_tsv) if c[1]]
    papers = load_articles(args.articles_tsv, "/health-discover")
    news = load_articles(args.news_tsv, "/health-news") if args.news_tsv else []
    popular = load_articles(args.popular_tsv, "/article") if args.popular_tsv else []

    # 被 Astro 页覆盖的分类 URL 只留在 Astro 区块，避免重复。
    cats_section = [c for c in cats if c[0] not in astro_paths]
    shadowed = len(cats) - len(cats_section)

    # 区块内日期倒序；同日按原顺序（分类 id / Astro 路径 / 文章 id）稳定排列。
    # 首页 canonical 带尾斜杠（https://m.himd.com/），sitemap 用同一形态。
    home = [("/", dt.date.today().strftime("%Y-%m-%d"))]
    cats_section.sort(key=lambda x: x[2])
    cats_section.sort(key=lambda x: x[1] or "0000-00-00", reverse=True)
    astro.sort(key=lambda x: x[0])
    astro.sort(key=lambda x: x[1] or "0000-00-00", reverse=True)
    papers.sort(key=lambda x: (x[1] or "0000-00-00", x[2]), reverse=True)
    news.sort(key=lambda x: (x[1] or "0000-00-00", x[2]), reverse=True)
    popular.sort(key=lambda x: (x[1] or "0000-00-00", x[2]), reverse=True)

    sections = [
        ("home", home),
        ("categories", [(p, d) for p, d, _ in cats_section]),
        ("astro", astro),
        ("papers", [(p, d) for p, d, _ in papers]),
        ("news", [(p, d) for p, d, _ in news]),
        ("popular", [(p, d) for p, d, _ in popular]),
    ]

    entries = []
    seen = set()
    counts = {}
    for name, section in sections:
        n = 0
        for path, lastmod in section:
            if len(entries) >= args.target_total:
                break
            if path in seen:
                continue
            seen.add(path)
            entries.append((path, lastmod))
            n += 1
        counts[name] = n

    if len(entries) > MAX_URLS:
        raise SystemExit("over %d urls: %d" % (MAX_URLS, len(entries)))

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
        ' xmlns:mobile="http://www.baidu.com/schemas/sitemap-mobile/1/">',
    ]
    for path, lastmod in entries:
        parts.append("<url>")
        parts.append("  <loc>%s%s</loc>" % (BASE, path))
        parts.append('  <mobile:mobile type="mobile"/>')
        if lastmod:
            parts.append("  <lastmod>%s</lastmod>" % lastmod)
        parts.append("</url>")
    parts.append("</urlset>")
    xml = "\n".join(parts) + "\n"

    size = len(xml.encode("utf-8"))
    if size > MAX_BYTES:
        raise SystemExit("over %d bytes: %d" % (MAX_BYTES, size))

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(xml)

    dates = [d for _, d in entries if d]
    print("wrote %s" % args.out)
    print("target=%d home=%d categories=%d (astro shadowed %d) astro=%d papers=%d news=%d popular=%d"
          % (args.target_total, counts["home"], counts["categories"], shadowed,
             counts["astro"], counts["papers"], counts["news"], counts["popular"]))
    print("total=%d bytes=%d" % (len(entries), size))
    print("date range: %s .. %s" % (min(dates), max(dates)))
    if len(entries) < args.target_total:
        print("note: only %d entries available, below target %d" % (len(entries), args.target_total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
