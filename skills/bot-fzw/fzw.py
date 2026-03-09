#!/usr/bin/env python3
"""
bot-fzw: 全国法院失信被执行人查询工具
查询网站: https://zxgk.court.gov.cn/zhzxgk/

接口分析:
  主页:     GET  /zhzxgk/
  验证码:   GET  /zhzxgk/captcha.do?captchaId={uuid}&random={rand}
  验证码校验: GET /zhzxgk/checkyzm?captchaId={uuid}&pCode={code}
  查询:     POST /zhzxgk/searchZhcx.do  (form data: pName,pCardNum,pCode,captchaId,currentPage,selectCourtArrange)
  返回:     JSON -> json[0].result (list), json[0].totalSize, json[0].currentPage

结果存入 fzw.db (SQLite)，每次查询均为新记录（含时间戳）
"""

import sqlite3
import json
import time
import re
import uuid
import argparse
import sys
import os
from datetime import datetime

try:
    import requests
    import ddddocr
except ImportError:
    print("请先安装依赖: cd skills/bot-fzw && venv/bin/pip install requests ddddocr")
    sys.exit(1)

# 数据库路径（与脚本同目录）
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fzw.db")
BASE_URL = "https://zxgk.court.gov.cn/zhzxgk"

HEADERS_HTML = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
}

HEADERS_AJAX = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://zxgk.court.gov.cn",
    "Referer": "https://zxgk.court.gov.cn/zhzxgk/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


def check_network():
    """检测出口IP及网络可达性，返回 (出口IP, 是否大陆IP, 法执网是否可达)"""
    out_ip = "未知"
    is_mainland = False
    site_ok = False

    # 检测出口IP（用 ip-api.com，支持中文地区信息）
    try:
        r = requests.get(
            "http://ip-api.com/json?fields=query,country,countryCode,regionName,city,isp",
            timeout=8, headers={"User-Agent": "curl/7.64.1"}
        )
        d = r.json()
        out_ip = d.get("query", "未知")
        country_code = d.get("countryCode", "")
        country = d.get("country", "")
        city = d.get("city", "")
        isp = d.get("isp", "")
        is_mainland = country_code == "CN"
        if out_ip != "未知":
            print(f"  出口IP详情: {out_ip} | {country} {city} | {isp}")
    except Exception as e:
        # 备用：ipify
        try:
            out_ip = requests.get("https://api.ipify.org", timeout=5).text.strip()
        except Exception:
            pass
        print(f"  IP检测备用: {out_ip} (无法获取地区信息)")

    # 检测法执网是否可达
    try:
        r3 = requests.get(
            "https://zxgk.court.gov.cn/zhzxgk/",
            timeout=12,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
            allow_redirects=True
        )
        site_ok = r3.status_code == 200 and len(r3.text) > 1000
    except Exception:
        site_ok = False

    return out_ip, is_mainland, site_ok


def init_db():
    """初始化数据库"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS fzw_records (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            query_name      TEXT,
            query_card      TEXT,
            queried_at      TEXT NOT NULL,
            result_count    INTEGER DEFAULT 0,
            total_size      INTEGER DEFAULT 0,
            -- 结果字段（每条结果一行）
            rec_name        TEXT,
            sex             TEXT,
            age             TEXT,
            card_num        TEXT,
            exec_court      TEXT,
            case_code       TEXT,
            case_create_time TEXT,
            duty            TEXT,
            performance     TEXT,
            area_name       TEXT,
            rec_id          TEXT,
            raw_json        TEXT
        )
    """)
    conn.commit()
    conn.close()
    print(f"[DB] 数据库: {DB_PATH}")


def save_records(query_name, query_card, results, queried_at, total_size=0):
    """将查询结果存入数据库，每次查询都是新记录"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    count = len(results)

    if count == 0:
        c.execute("""
            INSERT INTO fzw_records
            (query_name, query_card, queried_at, result_count, total_size, raw_json)
            VALUES (?, ?, ?, 0, 0, '[]')
        """, (query_name, query_card, queried_at))
        print(f"[DB] 写入：无结果记录")
    else:
        for item in results:
            # 处理日期字段
            cct = item.get("caseCreateTime")
            case_date = ""
            if isinstance(cct, dict):
                case_date = f"{cct.get('year', 0) + 1900}-{cct.get('month', 0) + 1:02d}-{cct.get('date', 0):02d}"
            c.execute("""
                INSERT INTO fzw_records
                (query_name, query_card, queried_at, result_count, total_size,
                 rec_name, sex, age, card_num, exec_court, case_code,
                 case_create_time, duty, performance, area_name, rec_id, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                query_name, query_card, queried_at, count, total_size,
                item.get("pname", "") or item.get("iname", ""),
                item.get("sexy", "") or item.get("sex", ""),
                item.get("age", ""),
                item.get("dePartyCardNum", "") or item.get("cardNum", ""),
                item.get("courtName", ""),
                item.get("caseCode", ""),
                case_date,
                item.get("duty", ""),
                item.get("performance", ""),
                item.get("areaName", ""),
                str(item.get("id", "")),
                json.dumps(item, ensure_ascii=False)
            ))
        print(f"[DB] 写入 {count} 条结果（总量 {total_size}，时间: {queried_at}）")

    conn.commit()
    conn.close()


def make_captcha_id():
    """生成 UUID 格式的验证码 ID"""
    return str(uuid.uuid4()).replace("-", "")


def recognize_captcha(image_bytes):
    """使用 ddddocr 识别验证码"""
    try:
        ocr = ddddocr.DdddOcr()
        if hasattr(ocr, 'predict'):
            result = ocr.predict(image_bytes)
            text = result.text if hasattr(result, 'text') else str(result)
        else:
            text = ocr.classification(image_bytes)
        return text.strip()
    except Exception as e:
        print(f"[验证码] OCR 失败: {e}")
        return None


def init_session(max_try=3):
    """初始化 session，获取 WAF cookie 和 JSESSIONID"""
    for i in range(max_try):
        session = requests.Session()
        try:
            resp = session.get(
                f"{BASE_URL}/",
                headers=HEADERS_HTML,
                timeout=25,
                allow_redirects=True
            )
            if resp.status_code == 200 and len(resp.text) > 1000:
                print(f"[Session] 初始化成功（{len(resp.text)} bytes）")
                # 从 HTML 中提取初始 captchaId
                m = re.search(r'captchaId.*?value=["\']([a-f0-9\-]+)["\']', resp.text)
                init_captcha_id = m.group(1).replace("-", "") if m else None
                return session, resp.text, init_captcha_id
            else:
                print(f"[Session] 第{i+1}次失败: status={resp.status_code}, len={len(resp.text)}, 等待重试...")
                time.sleep(5)
        except Exception as e:
            print(f"[Session] 第{i+1}次异常: {e}，等待重试...")
            time.sleep(5)
    return None, None, None


def get_captcha(session, captcha_id=None):
    """获取并识别验证码"""
    if captcha_id is None:
        captcha_id = make_captcha_id()
    rand = str(time.time())
    url = f"{BASE_URL}/captcha.do?captchaId={captcha_id}&random={rand}"
    try:
        resp = session.get(
            url,
            headers={**HEADERS_AJAX, "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"},
            timeout=10
        )
        content_type = resp.headers.get("content-type", "")
        if resp.status_code == 200 and "image" in content_type and len(resp.content) > 200:
            text = recognize_captcha(resp.content)
            print(f"[验证码] ID={captcha_id[:8]}... 识别={text}")
            return captcha_id, text
        else:
            print(f"[验证码] 获取失败: status={resp.status_code}, type={content_type}, len={len(resp.content)}")
            return captcha_id, None
    except Exception as e:
        print(f"[验证码] 请求异常: {e}")
        return captcha_id, None


def check_captcha(session, captcha_id, pcode):
    """校验验证码是否正确，返回 True/False"""
    try:
        resp = session.get(
            f"{BASE_URL}/checkyzm",
            params={"captchaId": captcha_id, "pCode": pcode},
            headers=HEADERS_AJAX,
            timeout=8
        )
        result = resp.json()
        return result == "1" or result == 1
    except Exception as e:
        print(f"[验证码校验] 异常: {e}")
        return True  # 校验失败时继续尝试查询


def query_page(session, name, card_num, captcha_id, pcode, page=1):
    """查询一页结果"""
    data = {
        "pName": name or "",
        "pCardNum": card_num or "",
        "pCode": pcode,
        "captchaId": captcha_id,
        "selectCourtId": "0",
        "searchCourtName": "全国法院（包含地方各级法院）",
        "currentPage": str(page),
        "selectCourtArrange": "1",
        "pnameNewDel": "",
        "cardNumNewDel": "",
        "caseCodeNewDel": "",
    }
    resp = session.post(
        f"{BASE_URL}/searchZhcx.do",
        data=data,
        headers=HEADERS_AJAX,
        timeout=20
    )
    print(f"[查询] status={resp.status_code}, len={len(resp.text)}")
    if resp.status_code != 200:
        return None, 0, 0
    try:
        j = resp.json()
        if isinstance(j, list) and len(j) > 0:
            result = j[0].get("result", []) or []
            total = j[0].get("totalSize", 0)
            cur_page = j[0].get("currentPage", page)
            return result, total, cur_page
        return [], 0, page
    except Exception as e:
        print(f"[查询] JSON解析失败: {e}, 内容: {resp.text[:300]}")
        return None, 0, 0


def query_fzw(name=None, card_num=None, max_retry=3, fetch_all_pages=True):
    """
    查询失信被执行人主函数
    """
    queried_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}")
    print(f"[查询] 姓名={name or '-'} | 身份证={card_num or '-'} | 时间={queried_at}")
    print(f"{'='*60}")

    # 初始化 session
    session, html, init_cid = init_session()
    if session is None:
        print("[!] Session 初始化失败，无法查询")
        save_records(name, card_num, [], queried_at)
        return []

    all_results = []
    success = False

    for attempt in range(1, max_retry + 1):
        print(f"\n[尝试 {attempt}/{max_retry}]")

        # 获取验证码（每次重新生成 ID）
        captcha_id = make_captcha_id()
        captcha_id, pcode = get_captcha(session, captcha_id)

        if not pcode:
            print("[!] 验证码识别失败，重试...")
            time.sleep(1)
            continue

        # 可选：先校验验证码
        ok = check_captcha(session, captcha_id, pcode)
        if not ok:
            print(f"[!] 验证码 '{pcode}' 校验不通过，重新获取...")
            time.sleep(0.5)
            continue

        print(f"[✓] 验证码 '{pcode}' 校验通过")

        # 查询第一页
        results, total, cur_page = query_page(session, name, card_num, captcha_id, pcode, page=1)

        if results is None:
            print("[!] 查询请求失败，重新初始化 session...")
            session, html, init_cid = init_session()
            if session is None:
                break
            continue

        print(f"[结果] 第1页 {len(results)} 条，总量 {total}")
        all_results.extend(results)
        success = True

        # 翻页获取全部结果
        if fetch_all_pages and total > 10:
            total_pages = (total + 9) // 10
            for page in range(2, min(total_pages + 1, 11)):  # 最多10页
                print(f"[翻页] 获取第 {page}/{total_pages} 页...")
                time.sleep(0.5)
                # 翻页需要新验证码
                cid2, pcode2 = get_captcha(session, make_captcha_id())
                if not pcode2:
                    print(f"[!] 第{page}页验证码失败，停止翻页")
                    break
                more, _, _ = query_page(session, name, card_num, cid2, pcode2, page=page)
                if more is None or len(more) == 0:
                    break
                all_results.extend(more)
                print(f"[翻页] 第{page}页 {len(more)} 条，累计 {len(all_results)} 条")

        break

    # 打印结果摘要
    print(f"\n{'='*60}")
    print(f"[汇总] 共 {len(all_results)} 条结果")
    for i, r in enumerate(all_results, 1):
        name_val = r.get("pname") or r.get("iname") or ""
        card_val = r.get("dePartyCardNum") or r.get("cardNum") or ""
        court_val = r.get("courtName") or ""
        code_val = r.get("caseCode") or ""
        print(f"  [{i}] {name_val} | {card_val} | {court_val} | {code_val}")

    save_records(name, card_num, all_results, queried_at, total_size=len(all_results))
    return all_results


def show_db_records(limit=20):
    """查看数据库中的记录"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT id, queried_at, query_name, query_card, result_count,
               rec_name, card_num, exec_court, case_code
        FROM fzw_records
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()

    print(f"\n{'='*100}")
    print(f"{'ID':<4} {'查询时间':<20} {'查询姓名':<10} {'查询身份证':<20} {'结果数':<6} {'被执行人':<12} {'法院':<20} {'案号'}")
    print(f"{'-'*100}")
    for row in rows:
        print(f"{row[0]:<4} {str(row[1]):<20} {str(row[2] or ''):<10} {str(row[3] or ''):<20} "
              f"{str(row[4]):<6} {str(row[5] or '无'):<12} {str(row[6] or ''):<20} {str(row[8] or '')}")
    print(f"{'='*100}")
    print(f"共 {len(rows)} 条记录（最新 {limit} 条）")


def main():
    parser = argparse.ArgumentParser(
        description="全国法院失信被执行人查询工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 fzw.py --card 110101199001011234
  python3 fzw.py --name 张三 --card 110101199001011234
  python3 fzw.py --name 某某科技有限公司
  python3 fzw.py --show
  python3 fzw.py --show --limit 50
        """
    )
    parser.add_argument("--name", "-n", help="姓名或企业名称")
    parser.add_argument("--card", "-c", help="身份证号或统一社会信用代码")
    parser.add_argument("--show", "-s", action="store_true", help="查看数据库记录")
    parser.add_argument("--limit", "-l", type=int, default=20, help="显示记录条数（默认20）")
    parser.add_argument("--no-paging", action="store_true", help="只获取第一页结果")
    parser.add_argument("--force", action="store_true", help="即使网络检测失败也强制执行")
    args = parser.parse_args()

    init_db()

    if args.show:
        show_db_records(args.limit)
        return

    if not args.name and not args.card:
        parser.print_help()
        return

    # 网络自检
    print("[网络检测] 检测出口IP及法执网可达性...")
    out_ip, is_mainland, site_ok = check_network()
    mainland_str = "✅ 大陆IP" if is_mainland else "⚠️  非大陆IP"
    site_str = "✅ 可达" if site_ok else "❌ 不可达（可能被墙或IP封禁）"
    print(f"  出口IP: {out_ip} ({mainland_str})")
    print(f"  法执网: {site_str}")
    if not site_ok:
        print("\n[!] 法执网不可达，查询可能失败。")
        print("    建议：确认 zxgk.court.gov.cn 已走大陆VPN通道")
        if not args.force:
            print("    使用 --force 强制继续查询")
            return

    query_fzw(
        name=args.name,
        card_num=args.card,
        fetch_all_pages=not args.no_paging
    )


if __name__ == "__main__":
    main()
