#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
himd.com sitemap 生成器（2026-11-09 版，与百度站长平台槽位一一对应）

输出 5 个文件：
  www sitemap.xml            首页 + 入口分类 + 最新文章，40000 条封顶（PC 槽 1）
  www sitemap-diseases.xml   全部内容页：疾病 + 药品 + Astro 直出页（PC 槽 2）
  www sitemap-drugs.xml      药品页单独一份（PC 槽 3，与槽 2 重复，留作备用）
  www sitemap-services.xml   服务页 3 条：/recruitment/ /consult /medicine-guide（PC 槽 4）
  m   sitemap_bd_s_37.xml    m 站唯一槽位：首页+服务+分类+Astro+论文+新闻+科普，40000 条封顶

百度规则：不支持索引型 sitemap；单文件 ≤ 50000 条且 < 10MB；URL 必须属于已验证主域。
站点只提交 sitemap.xml 和 sitemap-diseases.xml，所以槽 2 承载全部内容页，按 slug 去重；
新上线的疾病/药品页由索引 JSON 或 nginx alias 自动进入下一次生成，不需要再改脚本。
药品/服务另有独立文件，避免全部页面淹在 4 万条文章里，也便于按文件看抓取/索引数据。

lastmod 口径：
  - 疾病 / 药品：读 public/static/himd1/{cancer,drug}-index.json 的 updated（内容更新时间）
  - 文章 / 分类：MySQL archives/categories 的 updated_time，缺失用 created_time
  - Astro 页：/tmp/sitemap-astro.xml（可选构建产物）> 索引 JSON > nginx alias 文件 mtime
  - 服务页：/recruitment/ 取 recruitment/index.html mtime；/medicine-guide 取分类更新时间；
    /consult 无可靠来源，不写 lastmod

主 sitemap 不再收录：疾病 82、药品 220、服务 3（在各自专项文件里）、/price 子页、
type=3 单页（/about、/user-agreement）、隐藏分类的文章。

生成后先全量校验（域名、条数、字节、必含页、重复、禁收项），全部通过才原子替换线上文件；
任一失败只写 /tmp 并以非 0 退出，线上文件保持原样。

运行方式（仓库源文件经 ssh 喂给远端 python，不在服务器留临时副本）：
  # 预演（写 /tmp/sitemap-preview）
  ssh himd-web-01 'python3 - --dry-run' < deploy/gen-sitemap.py
  # 正式（备份到 /data/www/himd/backup/sitemaps/ 后原子替换）
  ssh himd-web-01 'python3 -' < deploy/gen-sitemap.py

定时：服务器 /usr/local/sbin/himd-gen-sitemap.py 由 himd-sitemap.timer 每日 03:40 调用。
改了本文件后要同步：scp deploy/gen-sitemap.py himd-web-01:/usr/local/sbin/himd-gen-sitemap.py
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
import xml.etree.ElementTree as ET
from urllib.parse import quote

SITE_ROOT = "/data/www/himd/cms"
PUBLIC_DIR = os.path.join(SITE_ROOT, "public")
INDEX_DIR = os.path.join(PUBLIC_DIR, "static", "himd1")
CONFIG_FILE = os.path.join(SITE_ROOT, "config.json")
RECRUITMENT_INDEX = "/data/www/himd/recruitment/index.html"
BACKUP_ROOT = "/data/www/himd/backup/sitemaps"
WWW = "https://www.himd.com"
MOBILE = "https://m.himd.com"

ASTRO_CONFS = [
    "/etc/nginx/himd-ext/30-astro-diseases.conf",
    "/etc/nginx/himd-ext/31-astro-drugs.conf",
]

MAX_URLS = 40000
MAX_BYTES = 9900000  # 留余量，百度上限 10485760
MIN_DISEASES = 80
MIN_DRUGS = 200
KEEP_BACKUPS = 7
PRICE_CATEGORY_ID = 395

SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def fatal(msg):
    print("error: " + msg, file=sys.stderr)
    sys.exit(2)


def mysql_rows(sql):
    """用 mysql CLI 查询，返回 list[list[str]]。密码写进 600 权限临时文件，不进命令行。"""
    cfg = json.load(open(CONFIG_FILE))["mysql"]
    fd, pwfile = tempfile.mkstemp(prefix=".my-", text=True)
    try:
        os.chmod(pwfile, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("[client]\nuser=%s\npassword=\"%s\"\n" % (cfg["user"], cfg["password"]))
        cmd = [
            "/usr/bin/mysql", "--defaults-extra-file=" + pwfile, cfg["database"],
            "-N", "-B", "--default-character-set=utf8mb4", "-e", sql,
        ]
        out = subprocess.check_output(cmd)
    finally:
        os.remove(pwfile)
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


def load_index(filename, key):
    """读站内索引 JSON，返回 [(path, lastmod)]，按 path 排序。

    这是疾病 82 / 药品 220 的唯一权威清单，同时也是它们的内容更新时间来源。
    """
    path = os.path.join(INDEX_DIR, filename)
    if not os.path.exists(path):
        fatal("%s 不存在，专项 sitemap 无法生成" % path)
    try:
        data = json.load(open(path, encoding="utf-8")).get(key) or []
    except ValueError as exc:
        fatal("%s 解析失败：%s" % (path, exc))
    out = []
    for item in data:
        url = (item.get("url") or "").strip()
        if not url.startswith(WWW + "/"):
            continue
        rel = url[len(WWW):].rstrip("/")
        # 只收一级路径，避免把异常 URL 带进 sitemap
        if rel.count("/") != 1:
            continue
        lastmod = (item.get("updated") or "").strip()[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", lastmod):
            lastmod = ""
        out.append((rel, lastmod))
    out.sort()
    return out


def load_categories():
    """分类页 + 归档 URL 所需的 token 映射。

    只收 status=1 且 type<>3（单页不进 sitemap）；隐藏/单页分类的文章也不进 token_map。
    lastmod：分类自身 updated/created 优先，缺失时用该分类最新文章时间兜底。
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
        "WHERE c.status=1 AND c.type<>3 "
        "ORDER BY c.id ASC"
    )
    cats, token_map = [], {}
    for cid, token, ts in rows:
        token = token.rstrip("/")
        if not token.strip():
            continue
        # 注意：token 首尾空格是线上 URL 的一部分（如 " foretinib" 实际是 /%20foretinib），
        # 不能 strip，只能按 URL 规则编码。
        token_map[int(cid)] = token
        cats.append((quote("/" + token, safe="/"), pick_day(ts), "weekly", "0.9"))
    return cats, token_map


def load_archives(limit, token_map):
    """最新文章，按 id 倒序。排除价格库子页与 type=3 单页分类的文章。"""
    rows = mysql_rows(
        "SELECT id, category_id, updated_time, created_time, fixed_link FROM archives "
        "WHERE category_id <> %d "
        "AND category_id NOT IN (SELECT id FROM categories WHERE type=3) "
        "ORDER BY id DESC LIMIT %d" % (PRICE_CATEGORY_ID, limit)
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


def load_services():
    """服务页 3 条：/recruitment/ /consult /medicine-guide，lastmod 取可得的最佳来源。"""
    lastmod = day(os.path.getmtime(RECRUITMENT_INDEX)) if os.path.exists(RECRUITMENT_INDEX) else ""
    out = [("/recruitment/", lastmod)]
    out.append(("/consult", ""))
    rows = mysql_rows(
        "SELECT COALESCE(NULLIF(updated_time,0), created_time) FROM categories "
        "WHERE url_token='medicine-guide' AND status=1 AND type<>3 LIMIT 1"
    )
    out.append(("/medicine-guide", pick_day(rows[0][0]) if rows else ""))
    return out


def load_astro_xml(path):
    """可选：读 Astro 构建产物 sitemap-astro.xml，返回 [(path, lastmod)]。

    仅用于给 nginx alias 清单覆盖 lastmod，缺失/损坏都返回空列表。
    """
    if not path or not os.path.exists(path):
        return []
    try:
        root = ET.parse(path).getroot()
    except Exception as exc:
        print("warn: %s 解析失败（%s），改用索引 JSON / alias mtime" % (path, exc))
        return []
    out, seen = [], set()
    for url in root.findall(SITEMAP_NS + "url"):
        loc = (url.findtext(SITEMAP_NS + "loc") or "").strip()
        lastmod = (url.findtext(SITEMAP_NS + "lastmod") or "").strip()[:10]
        if not loc.startswith(WWW + "/"):
            continue
        rel = loc[len(WWW):]
        if rel in seen:
            continue
        seen.add(rel)
        out.append((rel, lastmod))
    return out


def render_xml(entries, base=WWW):
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for path, lastmod, cf, pr in entries:
        parts.append("  <url>")
        parts.append("    <loc>%s%s</loc>" % (base, path))
        if lastmod:
            parts.append("    <lastmod>%s</lastmod>" % lastmod)
        parts.append("    <changefreq>%s</changefreq>" % cf)
        parts.append("    <priority>%s</priority>" % pr)
        parts.append("  </url>")
    parts.append("</urlset>")
    return "\n".join(parts) + "\n"


def render_simple(entries, changefreq, base=WWW):
    """专项 sitemap：loc + lastmod(可选) + changefreq，一行一条。

    条目可以是 (path, lastmod) 或 (path, lastmod, changefreq)，后者优先用自己的频率。
    """
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for item in entries:
        path, lastmod = item[0], item[1]
        cf = item[2] if len(item) > 2 else changefreq
        line = "<url><loc>%s%s</loc>" % (base, path)
        if lastmod:
            line += "<lastmod>%s</lastmod>" % lastmod
        line += "<changefreq>%s</changefreq></url>" % cf
        parts.append(line)
    parts.append("</urlset>")
    return "\n".join(parts) + "\n"


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


def build_m_quality_entries(astro_xml_path, services, content_lastmod, target):
    """按 首页 → 服务 → 分类 → Astro → 论文解读 → 健康新闻 → 科普文章 组装，截断到 target。

    m 站没有多余槽位，服务页只能放进这一个文件，所以紧跟首页，避免被截断。
    Astro 段以服务器 nginx 的 location = /slug 为准（线上实际直出页）。
    """
    astro_raw = load_astro_pages()
    if not astro_raw:
        print("warn: 未从 nginx 配置读到 Astro 页面，Astro 段为空")
    overlay = dict(load_astro_xml(astro_xml_path))
    astro = [(p, overlay.get(p) or content_lastmod.get(p) or lm, cf, pr)
             for p, lm, cf, pr in astro_raw]
    astro_paths = set(p for p, *_ in astro)

    home = [("/", day(time.time()))]
    cats = [(p, content_lastmod.get(p) or d, cid)
            for p, d, cid in load_m_categories() if p not in astro_paths]
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
        ("services", services),
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


def verify(name, xml, base, expected=None, minimum=0, must_contain=(), forbidden=()):
    """生成后自检：域名、条数、字节、必含页、重复、禁收项。返回错误列表。"""
    errs = []
    size = len(xml.encode("utf-8"))
    if size > MAX_BYTES:
        errs.append("%s 体积 %.2f MB 超过上限" % (name, size / 1048576.0))
    try:
        root = ET.fromstring(xml.encode("utf-8"))
    except ET.ParseError as exc:
        return ["%s XML 解析失败：%s" % (name, exc)]

    locs = []
    for u in root.findall(SITEMAP_NS + "url"):
        locs.append((u.findtext(SITEMAP_NS + "loc") or "").strip())

    if expected is not None and len(locs) != expected:
        errs.append("%s 条数 %d，预期 %d" % (name, len(locs), expected))
    if len(locs) < minimum:
        errs.append("%s 条数 %d 低于下限 %d" % (name, len(locs), minimum))
    if len(locs) > MAX_URLS:
        errs.append("%s 条数 %d 超过上限 %d" % (name, len(locs), MAX_URLS))

    paths, bad_host = [], []
    for loc in locs:
        if not loc.startswith(base + "/"):
            bad_host.append(loc)
        else:
            paths.append(loc[len(base):])
    path_set = set(paths)
    if bad_host:
        errs.append("%s 有 %d 条不属于 %s（例：%s）" % (name, len(bad_host), base, bad_host[0]))
    if len(paths) != len(path_set):
        errs.append("%s 存在重复 loc %d 条" % (name, len(paths) - len(path_set)))
    for p in must_contain:
        if p not in path_set:
            errs.append("%s 缺少必含页 %s" % (name, p))
    for p in forbidden:
        if p in path_set:
            errs.append("%s 不应包含 %s" % (name, p))
    if any(p.startswith("/price/") for p in path_set):
        errs.append("%s 包含 /price 子页" % name)
    for p in ("/about", "/user-agreement"):
        if p in path_set:
            errs.append("%s 包含 type=3 单页 %s" % (name, p))
    return errs


def write_atomic(path, content, mode=0o644):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".sitemap-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, mode)
        try:
            shutil.chown(tmp, "himd-cms", "himd-cms")
        except (LookupError, PermissionError):
            try:
                shutil.chown(tmp, "www", "www")
            except (LookupError, PermissionError):
                pass
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def backup_and_commit(outputs):
    """先备份线上 5 个文件（保留最近 KEEP_BACKUPS 份），再逐个原子替换。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(BACKUP_ROOT, stamp)
    os.makedirs(bdir, exist_ok=True)
    for name, _ in outputs:
        src = os.path.join(PUBLIC_DIR, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(bdir, name))
    backups = sorted(d for d in os.listdir(BACKUP_ROOT)
                     if os.path.isdir(os.path.join(BACKUP_ROOT, d)))
    for old in backups[:-KEEP_BACKUPS]:
        shutil.rmtree(os.path.join(BACKUP_ROOT, old), ignore_errors=True)
    for name, xml in outputs:
        write_atomic(os.path.join(PUBLIC_DIR, name), xml)


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

    diseases = load_index("cancer-index.json", "diseases")
    drugs = load_index("drug-index.json", "drugs")
    if len(diseases) < MIN_DISEASES:
        fatal("cancer-index.json 只有 %d 条（下限 %d）" % (len(diseases), MIN_DISEASES))
    if len(drugs) < MIN_DRUGS:
        fatal("drug-index.json 只有 %d 条（下限 %d）" % (len(drugs), MIN_DRUGS))
    services = load_services()
    content_lastmod = dict(diseases)
    content_lastmod.update(drugs)
    special = set(content_lastmod) | set(p for p, _ in services)

    cats, token_map = load_categories()
    cats = [(p, content_lastmod.get(p) or d, cf, pr) for p, d, cf, pr in cats if p not in special]
    astro_raw = load_astro_pages()
    overlay = dict(load_astro_xml(args.astro_sitemap))
    astro = [(p, overlay.get(p) or content_lastmod.get(p) or lm, cf, pr)
             for p, lm, cf, pr in astro_raw]

    # 首页 loc 带尾斜杠，与线上 canonical（https://www.himd.com/）同一形态
    home = [("/", day(time.time()), "always", "1.0")]
    # 同一个 slug 只出现一次：Astro 页在线上覆盖同名分类页，lastmod 以 Astro 为准
    astro_map = dict((a[0], a) for a in astro)
    merged = 0
    new_cats = []
    for c in cats:
        a = astro_map.get(c[0])
        if a:
            merged += 1
            new_cats.append((c[0], a[1], "monthly", "0.9"))
        else:
            new_cats.append(c)
    cats = new_cats
    cat_paths = set(c[0] for c in cats)
    # 专项文件已收录的页面不再进主 sitemap
    astro_only = [a for a in astro if a[0] not in cat_paths and a[0] not in special]

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
    kept = fit(entries, render_xml, max_urls=args.max_urls)
    main_xml = render_xml(kept, WWW)

    # 槽 2 承载全部内容页：疾病 + 药品 + Astro 直出页，按 slug 去重；
    # lastmod 取 Astro 段（构建产物 > 索引 JSON > alias mtime），没有 Astro 页的用索引时间
    disease_paths = set(p for p, _ in diseases)
    content_lastmod_map = dict((p, lm) for p, lm, _cf, _pr in astro)
    for path, lastmod in diseases + drugs:
        content_lastmod_map[path] = content_lastmod_map.get(path) or lastmod
    content_entries = [(p, content_lastmod_map[p], "weekly" if p in disease_paths else "monthly")
                       for p in sorted(content_lastmod_map)]

    diseases_xml = render_simple(content_entries, "weekly", WWW)
    drugs_xml = render_simple(drugs, "monthly", WWW)
    services_xml = render_simple(services, "weekly", WWW)

    m_entries, m_counts = build_m_quality_entries(args.astro_sitemap, services, content_lastmod, args.m_target)
    m_kept = fit(m_entries, render_xml_mobile, max_urls=args.m_target)
    m_xml = render_xml_mobile(m_kept)

    outputs = [
        ("sitemap.xml", main_xml),
        ("sitemap-diseases.xml", diseases_xml),
        ("sitemap-drugs.xml", drugs_xml),
        ("sitemap-services.xml", services_xml),
        ("sitemap_bd_s_37.xml", m_xml),
    ]

    errors = []
    errors += verify("sitemap.xml", main_xml, WWW, minimum=30000, must_contain=["/"], forbidden=special)
    errors += verify("sitemap-diseases.xml", diseases_xml, WWW,
                     expected=len(content_entries), minimum=MIN_DISEASES + MIN_DRUGS,
                     must_contain=["/breast-cancer", "/osimertinib"])
    errors += verify("sitemap-drugs.xml", drugs_xml, WWW,
                     expected=len(drugs), minimum=MIN_DRUGS, must_contain=["/osimertinib"])
    errors += verify("sitemap-services.xml", services_xml, WWW,
                     expected=len(services), must_contain=["/recruitment/", "/consult", "/medicine-guide"])
    errors += verify("sitemap_bd_s_37.xml", m_xml, MOBILE, minimum=30000,
                     must_contain=["/", "/recruitment/", "/consult", "/medicine-guide"])

    print("main: home=%d categories=%d (Astro 覆盖 %d) astro_only=%d archives=%d → %d 条"
          % (len(home), len(sections[0][1]), merged, len(sections[1][1]), len(archives), len(kept)))
    print("content: diseases=%d drugs=%d astro_extra=%d → %d 条"
          % (len(diseases), len(drugs),
             len(content_entries) - len(disease_paths | set(p for p, _ in drugs)),
             len(content_entries)))
    print("m quality: home=%d services=%d categories=%d astro=%d papers=%d news=%d popular=%d target=%d"
          % (m_counts["home"], m_counts["services"], m_counts["categories"], m_counts["astro"],
             m_counts["papers"], m_counts["news"], m_counts["popular"], args.m_target))
    for name, xml in outputs:
        print("  %-22s %6d 条  %8.2f MB" % (name, xml.count("<loc>"), len(xml.encode("utf-8")) / 1048576.0))

    if errors:
        for err in errors:
            print("verify-fail: " + err, file=sys.stderr)
        if args.dry_run:
            os.makedirs(args.out_dir, exist_ok=True)
            for name, xml in outputs:
                write_atomic(os.path.join(args.out_dir, name), xml)
            print("dry-run 预览已写 %s，但校验未通过，线上文件未动" % args.out_dir)
        return 2

    if args.dry_run:
        os.makedirs(args.out_dir, exist_ok=True)
        for name, xml in outputs:
            write_atomic(os.path.join(args.out_dir, name), xml)
        print("verify: ok")
        print("dry-run 预览写到 %s，线上文件未动" % args.out_dir)
        return 0

    backup_and_commit(outputs)
    print("verify: ok")
    print("已备份并替换 %d 个文件到 %s" % (len(outputs), PUBLIC_DIR))
    return 0


if __name__ == "__main__":
    sys.exit(main())
