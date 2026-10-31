#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
himd.com sitemap 生成器（替换原 incremental_sitemap_update.py）

输出顺序（重要度优先，超出上限时从尾部截断）：
  1. 首页
  2. 各分类页（categories.status = 1）
  3. Astro 页面（以 nginx 实际 alias 的 = /slug 精准匹配为准，只收上线中的）
  4. 旧内容：archives 按 id 倒序（最新在前）

产物（/www/wwwroot/himd.com/public/）：
  sitemap.xml / baidu-sitemap.xml / m-sitemap.xml / m-baidu-sitemap.xml / sitemap.txt / m-sitemap.txt

限制：单文件 ≤ 50000 条，且 ≤ 10MB（百度规则，按 UTF-8 字节数控制在 9.9MB 以内）。
站点：www.himd.com（CMS 站），m.himd.com（移动同站）。Astro 页由 nginx alias 直出。

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
    """分类页 + 归档 URL 所需的 token 映射。"""
    rows = mysql_rows(
        "SELECT id, url_token, updated_time, created_time, status FROM categories ORDER BY id ASC"
    )
    cats, token_map = [], {}
    for cid, token, updated, created, status in rows:
        token = token.rstrip("/")
        if not token.strip():
            continue
        # 注意：token 首尾空格是线上 URL 的一部分（如 " foretinib" 实际是 /%20foretinib），
        # 不能 strip，只能按 URL 规则编码。
        token_map[int(cid)] = token
        if status == "1":
            cats.append((quote("/" + token, safe="/"), pick_day(updated, created), "weekly", "0.9"))
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
    ap.add_argument("--out-dir", default="/tmp/sitemap-preview")
    args = ap.parse_args()

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

    print("output: %s %s" % (out_dir, "(dry-run)" if args.dry_run else ""))
    print("sections: home=%d categories=%d (其中线上 Astro 覆盖 %d) astro_only=%d archives=%d"
          % (len(home), len(sections[0][1]), merged, len(sections[1][1]), len(archives)))
    for name, count, size in stats:
        print("  %-20s %6d 条  %8.2f MB" % (name, count, size / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())