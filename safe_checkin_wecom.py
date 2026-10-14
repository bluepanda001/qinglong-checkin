#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Safe Check-in for QingLong
Sites: NodeSeek / V2EX / 恩山无线论坛

Security goals:
1. Cookie is read only from environment variables and is never printed.
2. No username/password login.
3. No dynamic pip install, no eval/exec/subprocess/os.system.
4. Credentials are only sent to the exact official host for each site.
5. WeCom (企业微信) robot notification is optional; only sanitized results are sent.
6. Multi-account supported with newline or ||| separator.
7. Enshan refreshed cookies are persisted locally with mode 600 so short-lived WAF cookies can rotate across days.

Recommended cron: 20 9 * * *

Required Python dependencies (install manually in QingLong -> Dependencies):
  requests
  curl_cffi   # NodeSeek only

Environment variables:
  NS_COOKIE / NODESEEK_COOKIE
  V2EX_COOKIES / V2EX_COOKIE
  ENSHAN_COOKIE / enshan_cookie

Optional:
  CHECKIN_DELAY_MAX=300        # random startup delay, seconds; 0 disables
  WECOM_WEBHOOK_KEY=...        # 企业微信群机器人 webhook URL 中的 key；留空则不通知
  HTTP_TIMEOUT=25

Multi-account separator:
  - one cookie per line; or
  - cookie1|||cookie2

This script intentionally does NOT import QingLong's notify.py, so its network behavior
remains auditable from this single file.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests

try:
    from curl_cffi import requests as cffi_requests
except Exception:
    cffi_requests = None


VERSION = "1.2.0"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
TIMEOUT = max(5, min(int(os.getenv("HTTP_TIMEOUT", "25")), 60))

OFFICIAL_HOSTS = {
    "nodeseek": "www.nodeseek.com",
    "v2ex": "www.v2ex.com",
    "enshan": "www.right.com.cn",
    "wecom": "qyapi.weixin.qq.com",
}


@dataclass
class Result:
    site: str
    account: int
    ok: bool
    status: str
    message: str

    def line(self) -> str:
        icon = "✅" if self.ok else "❌"
        return f"{icon} {self.site} 账号{self.account}: {self.message}"


def env_first(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def split_accounts(raw: str) -> list[str]:
    """Safely split account cookies. We intentionally do not split on '&'."""
    if not raw:
        return []
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    chunks: list[str] = []
    for line in normalized.split("\n"):
        for part in line.split("|||"):
            value = part.strip().strip('"').strip("'")
            if value:
                chunks.append(value)
    # Keep order and remove exact duplicates.
    seen = set()
    out = []
    for item in chunks:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def assert_official_url(url: str, site: str) -> None:
    parsed = urlparse(url)
    expected = OFFICIAL_HOSTS[site]
    if parsed.scheme != "https" or parsed.hostname != expected:
        raise ValueError(f"Blocked non-official URL for {site}: {parsed.scheme}://{parsed.hostname}")


def compact_text(value: object, limit: int = 220) -> str:
    text = re.sub(r"[\r\n\t]+", " ", str(value or "")).strip()
    return text[:limit] if len(text) > limit else text


def random_start_delay() -> None:
    try:
        max_delay = int(os.getenv("CHECKIN_DELAY_MAX", "300"))
    except ValueError:
        max_delay = 300
    max_delay = max(0, min(max_delay, 1800))
    if max_delay:
        delay = random.randint(0, max_delay)
        if delay:
            print(f"⏳ 随机延迟 {delay} 秒后开始（可用 CHECKIN_DELAY_MAX=0 关闭）")
            time.sleep(delay)


def requests_call(session: requests.Session, method: str, url: str, site: str, **kwargs):
    assert_official_url(url, site)
    kwargs.setdefault("timeout", TIMEOUT)
    kwargs.setdefault("allow_redirects", True)
    response = session.request(method, url, **kwargs)
    # Refuse cross-host redirects to prevent cookie leakage via redirects.
    final_host = urlparse(response.url).hostname
    if final_host != OFFICIAL_HOSTS[site]:
        raise RuntimeError(f"Blocked cross-host redirect for {site}: {final_host}")
    return response


# ======================= Local secure state (Enshan) =======================

STATE_FILE = Path(__file__).with_name(".safe_checkin_state.json")

def _cookie_fingerprint(cookie: str) -> str:
    return hashlib.sha256(cookie.encode("utf-8")).hexdigest()

def _load_state() -> dict:
    try:
        if not STATE_FILE.exists():
            return {}
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_state(state: dict) -> None:
    try:
        tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            raise
        os.replace(tmp, STATE_FILE)
        os.chmod(STATE_FILE, 0o600)
    except Exception as exc:
        print(f"⚠️ 恩山 Cookie 状态保存失败：{type(exc).__name__}")

def _enshan_cookie_for_run(env_cookie: str, account: int, state: dict) -> str:
    """Use persisted refreshed cookie only while the source env cookie is unchanged."""
    key = str(account)
    enshan = state.get("enshan") if isinstance(state.get("enshan"), dict) else {}
    item = enshan.get(key, {}) if isinstance(enshan, dict) else {}
    if isinstance(item, dict) and item.get("source_sha256") == _cookie_fingerprint(env_cookie):
        saved = item.get("cookie")
        if isinstance(saved, str) and saved.strip():
            return saved.strip()
    return env_cookie

def _persist_enshan_cookie(env_cookie: str, refreshed_cookie: str, account: int, state: dict) -> None:
    if not refreshed_cookie or refreshed_cookie == env_cookie:
        return
    enshan_state = state.setdefault("enshan", {})
    enshan_state[str(account)] = {
        "source_sha256": _cookie_fingerprint(env_cookie),
        "cookie": refreshed_cookie,
        "updated_at": int(time.time()),
    }
    _save_state(state)


# ============================= NodeSeek =============================

def nodeseek_checkin(cookie: str, account: int) -> Result:
    if cffi_requests is None:
        return Result(
            "NodeSeek",
            account,
            False,
            "dependency_missing",
            "缺少 curl_cffi；请在青龙依赖管理中手动安装 curl_cffi",
        )

    url = "https://www.nodeseek.com/api/attendance?random=true"
    assert_official_url(url, "nodeseek")
    headers = {
        "User-Agent": USER_AGENT,
        "Origin": "https://www.nodeseek.com",
        "Referer": "https://www.nodeseek.com/board",
        "Content-Type": "application/json",
        "Cookie": cookie,
    }

    try:
        r = cffi_requests.post(
            url,
            headers=headers,
            json={},
            timeout=TIMEOUT,
            impersonate="chrome110",
            allow_redirects=False,
        )
        # Do not follow redirects because Cookie must never leave nodeseek.com.
        if 300 <= r.status_code < 400:
            location = r.headers.get("location", "")
            if location:
                target = urljoin(url, location)
                assert_official_url(target, "nodeseek")
            return Result("NodeSeek", account, False, "redirect", "签到接口返回重定向，请更新 Cookie")

        data = {}
        try:
            data = r.json()
        except Exception:
            pass

        msg = compact_text(data.get("message") if isinstance(data, dict) else "")
        success = bool(data.get("success")) if isinstance(data, dict) else False
        status = data.get("status") if isinstance(data, dict) else None

        if success or "鸡腿" in msg or "签到成功" in msg:
            return Result("NodeSeek", account, True, "success", msg or "签到成功")
        if "已完成签到" in msg or "已签到" in msg:
            return Result("NodeSeek", account, True, "already", msg or "今日已签到")
        if status == 404 or r.status_code in (401, 403):
            return Result("NodeSeek", account, False, "cookie_invalid", "Cookie 无效或已过期")
        if r.status_code != 200:
            return Result("NodeSeek", account, False, "http_error", f"HTTP {r.status_code}")
        return Result("NodeSeek", account, False, "unknown", msg or "签到结果无法确认")
    except Exception as exc:
        return Result("NodeSeek", account, False, "error", f"请求失败: {type(exc).__name__}")


# =============================== V2EX ===============================

class BalanceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.values: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "div":
            return
        attr = dict(attrs)
        if self.depth:
            self.depth += 1
            return
        classes = set((attr.get("class") or "").split())
        if "balance_area" in classes and "bigger" in classes:
            self.depth = 1

    def handle_endtag(self, tag):
        if tag == "div" and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            text = " ".join(data.split())
            if text:
                self.values.append(text)


def parse_v2ex_balance(html: str) -> str:
    parser = BalanceParser()
    try:
        parser.feed(html)
    except Exception:
        return ""
    vals = [x for x in parser.values if x]
    if len(vals) == 2:
        vals = ["0", *vals]
    if len(vals) >= 3:
        return f"{vals[0]} 金 / {vals[1]} 银 / {vals[2]} 铜"
    return ""


def v2ex_checkin(cookie: str, account: int) -> Result:
    base = "https://www.v2ex.com"
    daily = base + "/mission/daily"
    balance_url = base + "/balance"
    for u in (daily, balance_url):
        assert_official_url(u, "v2ex")

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            "Cookie": cookie,
        }
    )

    login_markers = ("需要先登录", "Sign In", "/signin")
    already_markers = ("每日登录奖励已领取", "已领取", "已完成今日")

    try:
        r = requests_call(session, "GET", daily, "v2ex")
        if r.status_code != 200:
            return Result("V2EX", account, False, "http_error", f"每日任务页 HTTP {r.status_code}")
        body = r.text
        if any(m in body for m in login_markers):
            return Result("V2EX", account, False, "cookie_invalid", "Cookie 无效或已过期")

        claimed = any(m in body for m in already_markers)
        action_msg = "今日已签到" if claimed else ""

        if not claimed:
            match = re.search(r'(/mission/daily/redeem\?once=[A-Za-z0-9_-]+)', body)
            if not match:
                return Result("V2EX", account, False, "parse_error", "未找到领取链接，页面结构可能已变化")
            redeem = urljoin(base, match.group(1))
            assert_official_url(redeem, "v2ex")
            rr = requests_call(session, "GET", redeem, "v2ex", headers={"Referer": daily, "User-Agent": USER_AGENT, "Cookie": cookie})
            if rr.status_code != 200:
                return Result("V2EX", account, False, "http_error", f"领取请求 HTTP {rr.status_code}")

            # Confirm from the official daily page instead of trusting one response.
            vr = requests_call(session, "GET", daily, "v2ex")
            verify_text = vr.text
            if any(m in verify_text for m in login_markers):
                return Result("V2EX", account, False, "cookie_invalid", "签到后登录状态失效")
            if any(m in verify_text for m in already_markers) or "每日登录奖励" in rr.text:
                action_msg = "签到成功"
            else:
                return Result("V2EX", account, False, "verify_failed", "领取后无法确认签到状态")

        balance = ""
        try:
            br = requests_call(session, "GET", balance_url, "v2ex")
            if br.status_code == 200:
                balance = parse_v2ex_balance(br.text)
        except Exception:
            balance = ""

        msg = action_msg
        if balance:
            msg += f"；余额 {balance}"
        return Result("V2EX", account, True, "success", msg)
    except Exception as exc:
        return Result("V2EX", account, False, "error", f"请求失败: {type(exc).__name__}")
    finally:
        session.close()


# =============================== 恩山 ===============================

ENSHAN_BASE = "https://www.right.com.cn/forum"
ENSHAN_SIGN_PAGE = ENSHAN_BASE + "/erling_qd-sign_in.html"
ENSHAN_SIGN_API = ENSHAN_BASE + "/plugin.php?id=erling_qd:action&action=sign"
ENSHAN_FORUM = ENSHAN_BASE + "/forum.php"

for _u in (ENSHAN_SIGN_PAGE, ENSHAN_SIGN_API, ENSHAN_FORUM):
    assert_official_url(_u, "enshan")


def _cookie_upsert(cookie_string: str, kv: str) -> str:
    if "=" not in kv:
        return cookie_string
    key, value = kv.split("=", 1)
    key, value = key.strip(), value.strip()
    parts: list[tuple[str, str]] = []
    replaced = False
    for raw in cookie_string.split(";"):
        raw = raw.strip()
        if "=" not in raw:
            continue
        k, v = raw.split("=", 1)
        k, v = k.strip(), v.strip()
        if k == key:
            parts.append((key, value))
            replaced = True
        else:
            parts.append((k, v))
    if not replaced:
        parts.append((key, value))
    return "; ".join(f"{k}={v}" for k, v in parts)


def _extract_formhash(html: str) -> str:
    patterns = [
        r'name=["\']formhash["\']\s+value=["\']([0-9a-fA-F]+)["\']',
        r'formhash=([0-9a-fA-F]+)',
    ]
    for p in patterns:
        m = re.search(p, html, re.I | re.S)
        if m:
            return m.group(1)
    return ""


def _rotl8(x: int, n: int) -> int:
    x &= 0xFF
    n &= 7
    return ((x << n) & 0xFF) | (x >> (8 - n))


def _rotr8(x: int, n: int) -> int:
    x &= 0xFF
    n &= 7
    return (x >> n) | ((x << (8 - n)) & 0xFF)


def _decode_enshan_challenge(html: str) -> str:
    """Decode the current local JS cookie challenge without executing JavaScript."""
    arr_m = re.search(r"oo\s*=\s*\[([^\]]+)\]", html)
    if not arr_m:
        return ""
    tokens = re.findall(r"0x[0-9a-fA-F]+|\d+", arr_m.group(1))
    arr = [int(x, 16) if x.lower().startswith("0x") else int(x) for x in tokens]
    if len(arr) < 6:
        return ""

    wi_m = re.search(r'setTimeout\(["\']\w+\((\d+)\)["\']', html)
    if not wi_m:
        wi_m = re.search(r"\b\w+\((\d+)\)", html)
    if not wi_m:
        return ""
    wi = int(wi_m.group(1))

    p1 = re.search(
        r"qo\s*=\s*(\d+);\s*do\{.*?oo\[qo\]=\(-oo\[qo\]\)&0xff;.*?"
        r"oo\[qo\]=\(\(\(oo\[qo\]>>(\d+)\)\|\(\(oo\[qo\]<<(\d+)\)&0xff\)\)\-(\d+)\)&0xff;.*?"
        r"\}\s*while\(--qo>=2\);",
        html,
        re.S,
    )
    p2 = re.search(
        r"qo\s*=\s*(\d+);\s*do\s*\{[^}]*oo\[qo\]\s*=\s*\(oo\[qo\]\s*-\s*oo\[qo\s*-\s*1\]\)\s*&\s*0xff;[^}]*\}\s*while\s*\(\s*--\s*qo\s*>=\s*3\s*\)",
        html,
        re.S,
    )
    block = re.search(r"qo\s*=\s*1;\s*for\s*\(.*?\)\s*\{(.*?)\}\s*po\s*=", html, re.S)
    if not (p1 and p2 and block):
        return ""

    upper_m = re.search(r"qo\s*>\s*(\d+)\)\s*break", block.group(1))
    assign_m = re.search(r"oo\[qo\]\s*=\s*(.+?);", block.group(1), re.S)
    if not (upper_m and assign_m):
        return ""

    adds = [int(x) for x in re.findall(r"\+\s*(\d+)", assign_m.group(1))]
    shifts_raw = re.findall(r"<<\s*(\d+)|>>\s*(\d+)", assign_m.group(1))
    shifts = [int(a or b) for a, b in shifts_raw if (a or b)]
    if len(adds) < 2 or len(shifts) < 1:
        return ""

    mod_m = re.search(r"qo\s*%\s*(\d+)", html)
    mod_skip = int(mod_m.group(1)) if mod_m else 7

    loop1_start, sr, sl, sub = map(int, p1.groups())
    loop2_start = int(p2.group(1))
    loop3_upper = int(upper_m.group(1))
    add1, add2 = adds[0], adds[1]
    rot_l = shifts[0]

    last = len(arr) - 1
    q = min(loop1_start, last - 1)
    while q >= 2:
        arr[q] = (-arr[q]) & 0xFF
        if sr + sl == 8:
            arr[q] = (_rotr8(arr[q], sr) - sub) & 0xFF
        else:
            arr[q] = (((arr[q] >> sr) | ((arr[q] << sl) & 0xFF)) - sub) & 0xFF
        q -= 1

    q = min(loop2_start, last - 2)
    while q >= 3:
        arr[q] = (arr[q] - arr[q - 1]) & 0xFF
        q -= 1

    for q in range(1, min(loop3_upper, last - 1) + 1):
        arr[q] = _rotl8((arr[q] + add1 + add2) & 0xFF, rot_l)

    decoded = "".join(
        chr((arr[q] ^ (wi & 0xFF)) & 0xFF)
        for q in range(1, last)
        if q % mod_skip != 0
    )
    cookie_m = re.search(r"document\.cookie\s*=\s*[\"']([^\"']+)[\"']", decoded)
    if not cookie_m:
        return ""
    return cookie_m.group(1).split(";", 1)[0].strip()


class EnshanClient:
    def __init__(self, cookie: str):
        self.cookie = cookie
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Cookie": cookie,
            }
        )
        self.formhash = ""

    def _sync(self):
        self.session.headers["Cookie"] = self.cookie

    def _merge_response_cookies(self, response):
        for k, v in response.cookies.items():
            self.cookie = _cookie_upsert(self.cookie, f"{k}={v}")
        self._sync()

    def _get(self, url: str, **kwargs):
        r = requests_call(self.session, "GET", url, "enshan", **kwargs)
        self._merge_response_cookies(r)
        return r

    def _post(self, url: str, **kwargs):
        r = requests_call(self.session, "POST", url, "enshan", **kwargs)
        self._merge_response_cookies(r)
        return r

    def prepare(self) -> tuple[bool, str]:
        # 1) Sign page first: it has the current formhash and may expose a local JS cookie challenge.
        try:
            r = self._get(
                ENSHAN_SIGN_PAGE,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Cookie": self.cookie,
                    "Referer": ENSHAN_FORUM,
                },
            )
        except Exception:
            r = None

        if r is not None:
            self.formhash = _extract_formhash(r.text)
            if self.formhash:
                return True, "ok"

            # Current Enshan WAF challenge can be solved locally by decoding its JS-generated cookie.
            challenge_cookie = _decode_enshan_challenge(r.text)
            if challenge_cookie:
                self.cookie = _cookie_upsert(self.cookie, challenge_cookie)
                self._sync()
                try:
                    r2 = self._get(
                        ENSHAN_SIGN_PAGE,
                        headers={
                            "User-Agent": USER_AGENT,
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                            "Accept-Language": "zh-CN,zh;q=0.9",
                            "Cookie": self.cookie,
                            "Referer": ENSHAN_FORUM,
                        },
                    )
                    self.formhash = _extract_formhash(r2.text)
                    if self.formhash:
                        return True, "ok"
                except Exception:
                    pass

        # 2) Fallback to forum page.
        try:
            rf = self._get(ENSHAN_FORUM)
            if rf.status_code == 200:
                self.formhash = _extract_formhash(rf.text)
                if self.formhash:
                    return True, "ok"
            if rf.status_code in (401, 403, 521):
                return False, f"恩山返回 HTTP {rf.status_code}，Cookie/WAF 状态需要更新"
        except Exception as exc:
            return False, f"准备签到失败: {type(exc).__name__}"
        return False, "未获取到 formhash，Cookie 可能失效或页面结构已变化"

    def sign(self) -> tuple[bool, str]:
        ok, msg = self.prepare()
        if not ok:
            return False, msg

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://www.right.com.cn",
            "Referer": ENSHAN_SIGN_PAGE,
            "Cookie": self.cookie,
        }
        try:
            r = self._post(ENSHAN_SIGN_API, headers=headers, data={"formhash": self.formhash})
            if r.status_code in (401, 403, 521):
                return False, f"HTTP {r.status_code}，Cookie/WAF 状态需要更新"
            if r.status_code != 200:
                return False, f"签到接口 HTTP {r.status_code}"

            try:
                data = r.json()
            except Exception:
                data = {}

            message = ""
            success = False
            status = None
            if isinstance(data, dict):
                message = compact_text(data.get("message"))
                success = data.get("success") is True
                status = data.get("status")
            if success or status in (1, "1", "success", True):
                return True, message or "签到成功"
            if "已签到" in message or "已经签到" in message:
                return True, message
            if "成功" in message:
                return True, message

            text = compact_text(r.text)
            if "已签到" in text or "已经签到" in text or "签到成功" in text:
                return True, "今日已签到" if "已签到" in text else "签到成功"
            return False, message or "签到结果无法确认"
        except Exception as exc:
            return False, f"签到请求失败: {type(exc).__name__}"

    def close(self):
        self.session.close()


def enshan_checkin(cookie: str, account: int, state: Optional[dict] = None) -> Result:
    state = state if state is not None else {}
    runtime_cookie = _enshan_cookie_for_run(cookie, account, state)
    client = EnshanClient(runtime_cookie)
    try:
        ok, msg = client.sign()
        # Persist any Set-Cookie / locally refreshed WAF cookie for the next day.
        # The file is local-only (0600) and is never included in logs or notifications.
        _persist_enshan_cookie(cookie, client.cookie, account, state)
        return Result("恩山", account, ok, "success" if ok else "fail", msg)
    finally:
        client.close()


# ============================ Notification ============================

def _wecom_markdown(results: Iterable[Result]) -> str:
    items = list(results)
    ok_count = sum(1 for r in items if r.ok)
    fail_count = sum(1 for r in items if not r.ok)
    lines = [
        "## 🔐 Safe Check-in",
        f"> 成功：<font color=\"info\">{ok_count}</font>　失败：<font color=\"warning\">{fail_count}</font>",
        "",
    ]
    for r in items:
        color = "info" if r.ok else "warning"
        status = "✅" if r.ok else "❌"
        # r.message is sanitized and never contains Cookie/Token values.
        lines.append(f"{status} **{r.site} 账号{r.account}**：<font color=\"{color}\">{compact_text(r.message, 180)}</font>")
    content = "\n".join(lines)
    # 企业微信机器人 markdown 内容有长度限制；按字符保守截断。
    if len(content) > 3800:
        content = content[:3750] + "\n\n> 内容过长，已安全截断"
    return content


def wecom_notify(results: Iterable[Result]) -> None:
    key = env_first("WECOM_WEBHOOK_KEY", "QYWX_WEBHOOK_KEY")
    if not key:
        return
    # Key should be only the value after ?key=, never a full URL.
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", key):
        print("⚠️ WECOM_WEBHOOK_KEY 格式异常；只填写企业微信机器人 Webhook 中 ?key= 后面的值")
        return

    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
    assert_official_url(url, "wecom")
    payload = {
        "msgtype": "markdown",
        "markdown": {"content": _wecom_markdown(results)},
    }
    try:
        r = requests.post(
            url,
            json=payload,
            timeout=TIMEOUT,
            allow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
        )
        if 300 <= r.status_code < 400:
            print("⚠️ 企业微信通知接口返回重定向，已拒绝跟随")
            return
        if r.status_code != 200:
            print(f"⚠️ 企业微信通知发送失败：HTTP {r.status_code}")
            return
        try:
            data = r.json()
        except Exception:
            data = {}
        if isinstance(data, dict) and data.get("errcode") == 0:
            print("✅ 企业微信通知发送成功")
        else:
            errcode = data.get("errcode") if isinstance(data, dict) else "unknown"
            print(f"⚠️ 企业微信通知发送失败：errcode={errcode}")
    except Exception as exc:
        print(f"⚠️ 企业微信通知发送失败：{type(exc).__name__}")


# ================================ Main ================================

def run_site(site: str, cookies: list[str], state: Optional[dict] = None) -> list[Result]:
    if not cookies:
        return []
    out: list[Result] = []
    for idx, cookie in enumerate(cookies, 1):
        if site == "nodeseek":
            result = nodeseek_checkin(cookie, idx)
        elif site == "v2ex":
            result = v2ex_checkin(cookie, idx)
        elif site == "enshan":
            result = enshan_checkin(cookie, idx, state)
        else:
            raise ValueError(site)
        out.append(result)
        print(result.line())
        if idx < len(cookies):
            time.sleep(random.uniform(0.8, 2.0))
    return out


def main() -> int:
    print(f"🔐 Safe Check-in v{VERSION}")
    print("安全模式：不打印 Cookie；不自动安装依赖；不加载第三方 notify.py")

    ns = split_accounts(env_first("NS_COOKIE", "NODESEEK_COOKIE"))
    v2 = split_accounts(env_first("V2EX_COOKIES", "V2EX_COOKIE"))
    es = split_accounts(env_first("ENSHAN_COOKIE", "enshan_cookie"))

    if not any((ns, v2, es)):
        print("❌ 未配置任何 Cookie。请至少设置 NS_COOKIE / V2EX_COOKIES / ENSHAN_COOKIE 之一。")
        return 2

    random_start_delay()

    state = _load_state()

    results: list[Result] = []
    results += run_site("nodeseek", ns, state)
    results += run_site("v2ex", v2, state)
    results += run_site("enshan", es, state)

    print("\n======== 汇总 ========")
    for result in results:
        print(result.line())

    wecom_notify(results)

    failed = [r for r in results if not r.ok]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())