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
import socket
import ssl
import platform
import traceback
from datetime import datetime
from urllib.parse import urlparse

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

DEBUG = False
VERIFY_TLS = True
REQUIRED_PYTHON = (3, 10)
CAPTCHA_TIMEOUT = 20
CAPTCHA_RETRIES = 4
SAVE_CAPTCHA = False
CAPTCHA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captcha-debug")


class DebugSession(requests.Session):
    """带详细请求/响应日志的 Session"""

    def __init__(self, debug=False):
        super().__init__()
        self.debug = debug

    def request(self, method, url, **kwargs):
        start = time.time()
        debug = getattr(self, "debug", False)
        if debug:
            print(f"[HTTP] -> {method.upper()} {url}")
            if kwargs.get("params"):
                print(f"[HTTP]    params={json.dumps(kwargs['params'], ensure_ascii=False, default=str)}")
            if kwargs.get("data"):
                data = kwargs["data"]
                if isinstance(data, dict):
                    print(f"[HTTP]    data={json.dumps(data, ensure_ascii=False, default=str)}")
                else:
                    print(f"[HTTP]    data={str(data)[:500]}")
            headers = kwargs.get("headers") or {}
            if headers:
                masked = dict(headers)
                print(f"[HTTP]    headers={json.dumps(masked, ensure_ascii=False, default=str)}")
            print(f"[HTTP]    timeout={kwargs.get('timeout')}, allow_redirects={kwargs.get('allow_redirects', True)}")

        try:
            resp = super().request(method, url, **kwargs)
            cost_ms = int((time.time() - start) * 1000)
            if debug:
                print(f"[HTTP] <- {resp.status_code} {resp.reason} ({cost_ms} ms)")
                print(f"[HTTP]    final_url={resp.url}")
                print(f"[HTTP]    content-type={resp.headers.get('content-type', '')}")
                print(f"[HTTP]    content-length={resp.headers.get('content-length', '')}")
                print(f"[HTTP]    set-cookie={resp.headers.get('set-cookie', '')[:500]}")
                body_preview = ""
                try:
                    if "image" not in resp.headers.get("content-type", ""):
                        body_preview = resp.text[:500].replace("\n", " ")
                except Exception:
                    body_preview = "<unavailable>"
                if body_preview:
                    print(f"[HTTP]    body[:500]={body_preview}")
            return resp
        except Exception as e:
            cost_ms = int((time.time() - start) * 1000)
            if debug:
                print(f"[HTTP] !! {type(e).__name__}: {e} ({cost_ms} ms)")
                print(f"[HTTP]    traceback:\n{traceback.format_exc()}")
            raise


def dbg(msg):
    if DEBUG:
        print(msg)


def mask_proxy(url):
    if not url:
        return url
    return re.sub(r"://([^:@/]+):([^@/]+)@", r"://\1:***@", url)


def summarize_proxies(proxies):
    if not proxies:
        return {}
    return {k: mask_proxy(v) for k, v in proxies.items()}


def describe_environment():
    print("[诊断] 运行环境")
    print(f"  Python: {sys.version.split()[0]}")
    print(f"  requests: {getattr(requests, '__version__', 'unknown')}")
    print(f"  平台: {platform.platform()}")
    print(f"  OpenSSL: {ssl.OPENSSL_VERSION}")
    print(f"  SSL_CERT_FILE: {os.environ.get('SSL_CERT_FILE', '') or '-'}")
    print(f"  REQUESTS_CA_BUNDLE: {os.environ.get('REQUESTS_CA_BUNDLE', '') or '-'}")
    env_proxy = {
        "HTTP_PROXY": os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"),
        "HTTPS_PROXY": os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"),
        "ALL_PROXY": os.environ.get("ALL_PROXY") or os.environ.get("all_proxy"),
        "NO_PROXY": os.environ.get("NO_PROXY") or os.environ.get("no_proxy"),
    }
    print(f"  环境代理: {json.dumps(summarize_proxies(env_proxy), ensure_ascii=False)}")
    print("  提示: 浏览器常能自动使用系统代理/PAC，但 requests 通常只识别环境变量代理，不会自动读取 macOS 系统代理。")


def dns_lookup(hostname):
    try:
        infos = socket.getaddrinfo(hostname, 443, proto=socket.IPPROTO_TCP)
        ips = []
        for item in infos:
            ip = item[4][0]
            if ip not in ips:
                ips.append(ip)
        return ips
    except Exception as e:
        return [f"DNS解析失败: {e}"]


def tls_probe(hostname, port=443, timeout=10):
    start = time.time()
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((hostname, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()
                cipher = ssock.cipher()
                version = ssock.version()
                subject = cert.get("subject", [])
                issuer = cert.get("issuer", [])
                cost_ms = int((time.time() - start) * 1000)
                return {
                    "ok": True,
                    "ms": cost_ms,
                    "tls_version": version,
                    "cipher": cipher[0] if cipher else "",
                    "subject": str(subject)[:300],
                    "issuer": str(issuer)[:300],
                }
    except Exception as e:
        cost_ms = int((time.time() - start) * 1000)
        return {
            "ok": False,
            "ms": cost_ms,
            "error": f"{type(e).__name__}: {e}",
        }


def create_session(debug=False, proxies=None, verify=True):
    session = DebugSession(debug=debug)
    session.headers.update({"Accept-Encoding": "gzip, deflate, br"})
    session.verify = verify
    if proxies:
        session.proxies.update(proxies)
        session.trust_env = False
    else:
        session.trust_env = True
    return session


def print_session_info(session, label="Session"):
    print(f"[{label}] trust_env={session.trust_env}, verify_tls={getattr(session, 'verify', True)}")
    if session.proxies:
        print(f"[{label}] 显式代理: {json.dumps(summarize_proxies(session.proxies), ensure_ascii=False)}")
    else:
        print(f"[{label}] 显式代理: -")
    if session.cookies:
        cookie_names = sorted({c.name for c in session.cookies})
        print(f"[{label}] 当前Cookies: {cookie_names}")


def check_network(session=None):
    """检测出口IP及网络可达性，返回 (出口IP, 是否大陆IP, 法执网是否可达)"""
    out_ip = "未知"
    is_mainland = False
    site_ok = False
    sess = session or create_session(debug=DEBUG, verify=VERIFY_TLS)

    # 检测出口IP（用 ip-api.com，支持中文地区信息）
    try:
        r = sess.get(
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
    except Exception:
        # 备用：ipify
        try:
            out_ip = sess.get("https://api.ipify.org", timeout=5).text.strip()
        except Exception:
            pass
        print(f"  IP检测备用: {out_ip} (无法获取地区信息)")

    # 检测法执网是否可达
    try:
        r3 = sess.get(
            f"{BASE_URL}/",
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


def ensure_python_version():
    current = sys.version_info[:2]
    if current != REQUIRED_PYTHON:
        print(
            f"[环境] 当前 Python 为 {current[0]}.{current[1]}，"
            f"建议使用 Python {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]} 以兼容 ddddocr。"
        )
        print("[环境] 示例：python3.10 -m venv venv && ./venv/bin/pip install -r requirements.txt")
        return False
    return True


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


def save_captcha_image(image_bytes, captcha_id, attempt, recognized_text=None):
    if not SAVE_CAPTCHA:
        return None
    os.makedirs(CAPTCHA_DIR, exist_ok=True)
    suffix = recognized_text or "unknown"
    suffix = re.sub(r"[^0-9A-Za-z_-]", "_", suffix)
    filename = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{captcha_id[:8]}_try{attempt}_{suffix}.jpg"
    path = os.path.join(CAPTCHA_DIR, filename)
    try:
        with open(path, "wb") as f:
            f.write(image_bytes)
        print(f"[验证码] 已保存图片: {path}")
        return path
    except Exception as e:
        print(f"[验证码] 保存图片失败: {e}")
        return None


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


def init_session(max_try=3, proxies=None):
    """初始化 session，获取 WAF cookie 和 JSESSIONID"""
    for i in range(max_try):
        session = create_session(debug=DEBUG, proxies=proxies, verify=VERIFY_TLS)
        print_session_info(session, f"Session#{i+1}")
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
                if DEBUG:
                    print_session_info(session, f"Session#{i+1}-after-init")
                return session, resp.text, init_captcha_id
            else:
                print(f"[Session] 第{i+1}次失败: status={resp.status_code}, len={len(resp.text)}, 等待重试...")
                time.sleep(5)
        except Exception as e:
            print(f"[Session] 第{i+1}次异常: {type(e).__name__}: {e}，等待重试...")
            if DEBUG:
                print(traceback.format_exc())
            time.sleep(5)
    return None, None, None


def get_captcha(session, captcha_id=None, timeout=CAPTCHA_TIMEOUT, retries=CAPTCHA_RETRIES):
    """获取并识别验证码；返回详细信息字典"""
    last_reason = "unknown"
    last_path = None
    last_text = None
    last_id = captcha_id

    for attempt in range(1, retries + 1):
        if captcha_id is None:
            captcha_id = make_captcha_id()
        last_id = captcha_id
        rand = str(time.time())
        url = f"{BASE_URL}/captcha.do?captchaId={captcha_id}&random={rand}"
        try:
            resp = session.get(
                url,
                headers={**HEADERS_AJAX, "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"},
                timeout=timeout
            )
            content_type = resp.headers.get("content-type", "")
            if resp.status_code == 200 and "image" in content_type and len(resp.content) > 200:
                text = recognize_captcha(resp.content)
                path = save_captcha_image(resp.content, captcha_id, attempt, text)
                last_path = path
                last_text = text
                print(f"[验证码] 第{attempt}/{retries}次 ID={captcha_id[:8]}... 识别={text} | 文件={path or '-'}")
                if text:
                    return {
                        "captcha_id": captcha_id,
                        "text": text,
                        "ok": True,
                        "reason": "ok",
                        "attempt": attempt,
                        "image_path": path,
                    }
                last_reason = "ocr_failed"
                time.sleep(min(attempt, 2))
            else:
                last_reason = f"bad_response:{resp.status_code}:{content_type}:{len(resp.content)}"
                print(f"[验证码] 第{attempt}/{retries}次获取失败: status={resp.status_code}, type={content_type}, len={len(resp.content)}")
                time.sleep(min(attempt, 2))
        except Exception as e:
            last_reason = f"{type(e).__name__}: {e}"
            print(f"[验证码] 第{attempt}/{retries}次请求异常: {type(e).__name__}: {e}")
            if DEBUG:
                print(traceback.format_exc())
            time.sleep(min(attempt, 2))

        captcha_id = make_captcha_id()

    return {
        "captcha_id": last_id,
        "text": last_text,
        "ok": False,
        "reason": last_reason,
        "attempt": retries,
        "image_path": last_path,
    }


def check_captcha(session, captcha_id, pcode, image_path=None):
    """校验验证码是否正确，返回 (ok, raw_result)"""
    try:
        resp = session.get(
            f"{BASE_URL}/checkyzm",
            params={"captchaId": captcha_id, "pCode": pcode},
            headers=HEADERS_AJAX,
            timeout=8
        )
        result = resp.json()
        ok = result == "1" or result == 1
        print(f"[验证码校验] pcode={pcode} | ok={ok} | raw={result} | 文件={image_path or '-'}")
        dbg(f"[验证码校验] 返回: {result}")
        return ok, result
    except Exception as e:
        print(f"[验证码校验] 异常: {type(e).__name__}: {e} | pcode={pcode} | 文件={image_path or '-'}")
        if DEBUG:
            print(traceback.format_exc())
        return True, None  # 校验失败时继续尝试查询


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
    try:
        resp = session.post(
            f"{BASE_URL}/searchZhcx.do",
            data=data,
            headers=HEADERS_AJAX,
            timeout=20
        )
    except Exception as e:
        print(f"[查询] 请求异常: {type(e).__name__}: {e}")
        if DEBUG:
            print(traceback.format_exc())
        return None, 0, 0

    print(f"[查询] status={resp.status_code}, len={len(resp.text)}")
    if resp.status_code != 200:
        print(f"[查询] 非200响应: headers={dict(resp.headers)}")
        return None, 0, 0
    try:
        j = resp.json()
        if DEBUG:
            print(f"[查询] JSON前500字符: {json.dumps(j, ensure_ascii=False, default=str)[:500]}")
        if isinstance(j, list) and len(j) > 0:
            result = j[0].get("result", []) or []
            total = j[0].get("totalSize", 0)
            cur_page = j[0].get("currentPage", page)
            return result, total, cur_page
        return [], 0, page
    except Exception as e:
        print(f"[查询] JSON解析失败: {e}, 内容: {resp.text[:300]}")
        return None, 0, 0


def query_fzw(name=None, card_num=None, max_retry=3, fetch_all_pages=True, proxies=None):
    """
    查询失信被执行人主函数
    """
    queried_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}")
    print(f"[查询] 姓名={name or '-'} | 身份证={card_num or '-'} | 时间={queried_at}")
    print(f"{'='*60}")

    # 初始化 session
    session, html, init_cid = init_session(proxies=proxies)
    if session is None:
        print("[!] Session 初始化失败，无法查询")
        return None

    all_results = []
    query_succeeded = False
    last_error = None

    for attempt in range(1, max_retry + 1):
        print(f"\n[尝试 {attempt}/{max_retry}]")

        # 获取验证码（每次重新生成 ID）
        captcha_info = get_captcha(session, make_captcha_id())
        captcha_id = captcha_info["captcha_id"]
        pcode = captcha_info["text"]
        captcha_ok = captcha_info["ok"]
        captcha_reason = captcha_info["reason"]
        image_path = captcha_info.get("image_path")

        if not captcha_ok or not pcode:
            last_error = f"captcha_failed:{captcha_reason}"
            print(f"[!] 验证码获取/识别失败：{captcha_reason} | 文件={image_path or '-'}，重试...")
            session, html, init_cid = init_session(proxies=proxies)
            if session is None:
                break
            time.sleep(1)
            continue

        # 可选：先校验验证码
        ok, raw_check = check_captcha(session, captcha_id, pcode, image_path=image_path)
        if not ok:
            last_error = f"captcha_check_failed:{pcode}:{raw_check}"
            print(f"[!] 验证码 '{pcode}' 校验不通过 | 文件={image_path or '-'}，重新获取...")
            time.sleep(0.5)
            continue

        print(f"[✓] 验证码 '{pcode}' 校验通过 | 文件={image_path or '-'}")

        # 查询第一页
        results, total, cur_page = query_page(session, name, card_num, captcha_id, pcode, page=1)

        if results is None:
            last_error = "query_page_failed"
            print("[!] 查询请求失败，重新初始化 session...")
            session, html, init_cid = init_session(proxies=proxies)
            if session is None:
                break
            continue

        query_succeeded = True
        print(f"[结果] 第1页 {len(results)} 条，总量 {total}")
        all_results.extend(results)

        # 翻页获取全部结果
        if fetch_all_pages and total > 10:
            total_pages = (total + 9) // 10
            for page in range(2, min(total_pages + 1, 11)):  # 最多10页
                print(f"[翻页] 获取第 {page}/{total_pages} 页...")
                time.sleep(0.5)
                # 翻页需要新验证码
                page_captcha = get_captcha(session, make_captcha_id())
                cid2 = page_captcha["captcha_id"]
                pcode2 = page_captcha["text"]
                ok2 = page_captcha["ok"]
                reason2 = page_captcha["reason"]
                image2 = page_captcha.get("image_path")
                if not ok2 or not pcode2:
                    print(f"[!] 第{page}页验证码失败（{reason2}） | 文件={image2 or '-'}，停止翻页")
                    break
                more, _, _ = query_page(session, name, card_num, cid2, pcode2, page=page)
                if more is None:
                    print(f"[!] 第{page}页查询失败，停止翻页")
                    break
                if len(more) == 0:
                    break
                all_results.extend(more)
                print(f"[翻页] 第{page}页 {len(more)} 条，累计 {len(all_results)} 条")

        break

    if not query_succeeded:
        print(f"[失败] 本次查询未成功完成，最后错误: {last_error or 'unknown'}")
        print("[失败] 不写入“无结果”记录，避免把网络/验证码失败误判为查无结果。")
        return None

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


def parse_proxy_args(proxy_url=None):
    if not proxy_url:
        return None
    return {
        "http": proxy_url,
        "https": proxy_url,
    }


def run_diagnostics(session=None):
    print("\n[诊断] 开始网络诊断")
    describe_environment()
    hostname = urlparse(BASE_URL).hostname
    print(f"[诊断] DNS({hostname}): {dns_lookup(hostname)}")
    tls_result = tls_probe(hostname)
    if tls_result.get("ok"):
        print(
            f"[诊断] TLS握手成功: {tls_result.get('tls_version')} | {tls_result.get('cipher')} | "
            f"{tls_result.get('ms')}ms"
        )
        print(f"[诊断] 证书主题: {tls_result.get('subject')}")
        print(f"[诊断] 证书颁发者: {tls_result.get('issuer')}")
    else:
        print(f"[诊断] TLS握手失败: {tls_result.get('error')} ({tls_result.get('ms')}ms)")

    sess = session or create_session(debug=DEBUG, verify=VERIFY_TLS)
    try:
        resp = sess.get(f"{BASE_URL}/", headers=HEADERS_HTML, timeout=15, allow_redirects=True)
        print(f"[诊断] 首页探测: status={resp.status_code}, len={len(resp.text)}, final_url={resp.url}")
        print(f"[诊断] 首页响应头: content-type={resp.headers.get('content-type', '')}")
    except Exception as e:
        print(f"[诊断] 首页探测异常: {type(e).__name__}: {e}")
        if DEBUG:
            print(traceback.format_exc())


def main():
    global DEBUG, VERIFY_TLS, SAVE_CAPTCHA

    python_ok = ensure_python_version()

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
  python3 fzw.py --name 张三 --debug
  python3 fzw.py --name 张三 --debug --proxy http://127.0.0.1:7890
  python3 fzw.py --name 张三 --diag-only
  python3 fzw.py --name 张三 --debug --save-captcha
        """
    )
    parser.add_argument("--name", "-n", help="姓名或企业名称")
    parser.add_argument("--card", "-c", help="身份证号或统一社会信用代码")
    parser.add_argument("--show", "-s", action="store_true", help="查看数据库记录")
    parser.add_argument("--limit", "-l", type=int, default=20, help="显示记录条数（默认20）")
    parser.add_argument("--no-paging", action="store_true", help="只获取第一页结果")
    parser.add_argument("--force", action="store_true", help="即使网络检测失败也强制执行")
    parser.add_argument("--debug", action="store_true", help="开启详细网络/请求日志")
    parser.add_argument("--diag-only", action="store_true", help="只做网络诊断，不执行查询")
    parser.add_argument("--proxy", help="显式指定代理，例如 http://host:port")
    parser.add_argument("--insecure", action="store_true", help="跳过TLS证书校验（仅用于排障）")
    parser.add_argument("--save-captcha", action="store_true", help="将每次获取到的验证码图片保存到本地，便于排障")
    args = parser.parse_args()

    DEBUG = args.debug
    VERIFY_TLS = not args.insecure
    SAVE_CAPTCHA = args.save_captcha

    init_db()

    if not python_ok and not args.show:
        print("[环境] 建议切换到 Python 3.10 后再执行真实查询，否则 ddddocr 可能异常。")
        return

    if args.show:
        show_db_records(args.limit)
        return

    proxies = parse_proxy_args(args.proxy)
    diag_session = create_session(debug=DEBUG, proxies=proxies, verify=VERIFY_TLS)
    print_session_info(diag_session, "Startup")
    run_diagnostics(diag_session)

    if args.diag_only:
        return

    if not args.name and not args.card:
        parser.print_help()
        return

    # 网络自检
    print("[网络检测] 检测出口IP及法执网可达性...")
    out_ip, is_mainland, site_ok = check_network(session=diag_session)
    mainland_str = "✅ 大陆IP" if is_mainland else "⚠️  非大陆IP"
    site_str = "✅ 可达" if site_ok else "❌ 不可达（可能被墙、被企业策略拦截、证书/代理不匹配）"
    print(f"  出口IP: {out_ip} ({mainland_str})")
    print(f"  法执网: {site_str}")
    if not site_ok:
        print("\n[!] 法执网不可达，查询可能失败。")
        print("    建议排查：")
        print("    1) 浏览器是否走了企业代理/PAC，而 requests 没有")
        print("    2) 企业根证书是否已被 Python/requests 信任")
        print("    3) 是否被企业网关按 User-Agent/TLS 指纹/HTTP 协议策略拦截")
        print("    4) 可尝试 --proxy 或在环境变量中设置 HTTPS_PROXY")
        print("    5) 如怀疑证书拦截，可临时用 --insecure 验证是否为证书链问题")
        if not args.force:
            print("    使用 --force 强制继续查询")
            return

    query_fzw(
        name=args.name,
        card_num=args.card,
        fetch_all_pages=not args.no_paging,
        proxies=proxies
    )


if __name__ == "__main__":
    main()
