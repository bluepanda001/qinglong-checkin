import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime as RealDatetime
from unittest.mock import patch
from urllib.parse import urlsplit

from requests import Response
from requests.exceptions import Timeout

import safe_checkin_wecom as checkin


BASE = "https://www.v2ex.com"
DAILY = "/mission/daily"
BALANCE = "/balance"
REDEEM = "/mission/daily/redeem?once=current123"
MISSION = """
<h1>每日登录奖励 20261119</h1>
<a href="/signout?once=99999">退出</a>
<input type="button" value="领取 3 铜币"
       onclick="location.href = '/mission/daily/redeem?once=current123';">
"""
CLAIMED = "<h1>每日登录奖励 20261119</h1><div>每日登录奖励已领取</div>"


def ledger(*rows, copper=74):
    return (
        f'<div class="balance_area bigger">55 <img src="/s.png">'
        f'{copper} <img src="/b.png"></div>'
        '<table><tr><td>时间</td><td>类型</td><td>金额</td>'
        '<td>余额</td><td>描述</td></tr>' + "".join(rows) + "</table>"
    )


def transaction(timestamp, kind, amount, total, description):
    return (
        f"<tr><td><small>{timestamp}</small></td><td>{kind}</td>"
        f"<td><strong>{amount}</strong></td><td>{total}</td>"
        f"<td><span>{description}</span></td></tr>"
    )


YESTERDAY = transaction(
    "2026-11-18 19:37:00 +08:00", "每日登录奖励", "3.0", "5574.0",
    "20261118 的每日登录奖励 3 铜币",
)
TODAY = transaction(
    "2026-11-19 01:20:04 +00:00", "每日登录奖励", "+3.0", "5577.0",
    "20261119 的每日登录奖励 3 铜币",
)
ORIGINAL = ledger(YESTERDAY)
UPDATED = ledger(TODAY, YESTERDAY, copper=77)


def response(body="", status=200, location=None):
    result = Response()
    result.status_code = status
    result.encoding = "utf-8"
    result._content = body.encode("utf-8")
    if location is not None:
        result.headers["Location"] = location
    return result


class FixedDatetime(RealDatetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromisoformat("2026-11-19T09:20:00+08:00").astimezone(tz)


class V2EXTests(unittest.TestCase):
    def setUp(self):
        self.routes = {}
        self.sent = []
        self.patch_attribute(checkin.requests.adapters.HTTPAdapter, "send",
                             side_effect=self.send)
        self.patch_attribute(checkin.time, "sleep")
        self.patch_attribute(checkin, "datetime", new=FixedDatetime, create=True)

    def patch_attribute(self, target, name, **kwargs):
        fixture = patch.object(target, name, **kwargs)
        self.addCleanup(fixture.stop)
        return fixture.start()

    def send(self, request, **kwargs):
        info = urlsplit(request.url)
        self.assertEqual((info.scheme, info.netloc), ("https", "www.v2ex.com"))
        self.assertEqual(request.method, "GET")
        path = info.path + (f"?{info.query}" if info.query else "")
        cookies = request.headers.get("Cookie", "")
        self.sent.append((cookies, path, request.headers.get("Referer")))
        account = dict(part.strip().split("=", 1) for part in cookies.split(";") if "=" in part)
        login = account.get("A2", "first")
        try:
            result = self.routes[login, path].pop(0)
        except (KeyError, IndexError):
            self.fail(f"Unexpected request for {info.path}")
        if isinstance(result, Exception):
            raise result
        result.url = request.url
        result.request = request
        return result

    def queue(self, path, *results, account="first"):
        self.routes[account, path] = list(results)

    def checkin(self):
        return checkin.v2ex_checkin("PB3_SESSION=example; A2=first", 1)

    def claim(self, after, claim_response=None, mission=MISSION):
        self.queue(BALANCE, response(ORIGINAL), *after)
        self.queue(DAILY, response(mission))
        self.queue(REDEEM, claim_response if claim_response is not None
                   else response(status=302, location=DAILY))

    def assert_claim_count(self, count):
        claims = [(cookie, referrer) for cookie, path, referrer in self.sent
                  if path.startswith("/mission/daily/redeem")]
        self.assertEqual(len(claims), count)
        for cookie, referrer in claims:
            self.assertIn("PB3_SESSION=example", cookie)
            self.assertEqual(referrer, BASE + DAILY)

    def test_unclaimed_with_redeem_redirect_and_new_beijing_ledger_row(self):
        self.claim([response(UPDATED)])
        result = self.checkin()
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "success")
        self.assertIn("今日奖励：3 铜币", result.line())
        self.assertIn("当前余额：55 银 / 77 铜", result.line())
        self.assert_claim_count(1)

    def test_existing_row_skips_redeem(self):
        self.queue(BALANCE, response(UPDATED))
        result = self.checkin()
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "already")
        self.assertIn("ℹ️ V2EX 账号1", result.line())
        self.assertIn("今日奖励：3 铜币", result.line())
        self.assert_claim_count(0)

    def test_login_redirect_is_reported_without_following_it(self):
        self.queue(BALANCE, response(status=302, location="/signin?next=%2Fbalance"))
        self.queue(DAILY, response(status=302, location="/signin?next=%2Fmission%2Fdaily"))
        result = self.checkin()
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "cookie_invalid")
        self.assertEqual(len(self.sent), 1)

    def test_200_login_form_is_reported(self):
        self.queue(BALANCE, response('<form method="post" action="/signin"></form>'))
        self.queue(DAILY, response('<form method="post" action="/signin"></form>'))
        result = self.checkin()
        self.assertEqual(result.status, "cookie_invalid")
        self.assert_claim_count(0)

    def test_redeem_http_failure_is_not_success(self):
        self.claim([], response(status=503))
        result = self.checkin()
        self.assertFalse(result.ok)
        self.assertIn("HTTP 503", result.line())
        self.assert_claim_count(1)

    def test_title_from_200_response_does_not_prove_credit(self):
        self.claim([response(ORIGINAL) for _ in range(3)], response(MISSION))
        self.queue(DAILY, response(MISSION), response(MISSION))
        result = self.checkin()
        self.assertFalse(result.ok)
        self.assertIn("未检测到今日奖励流水", result.line())
        self.assert_claim_count(1)

    def test_delayed_ledger_only_retries_read(self):
        self.claim([response(ORIGINAL), response(UPDATED)])
        result = self.checkin()
        self.assertEqual(result.status, "success")
        self.assert_claim_count(1)
        self.assertEqual(sum(path == BALANCE for _, path, _ in self.sent), 3)

    def test_timeout_does_not_retry_claim(self):
        self.claim([response(ORIGINAL) for _ in range(3)], Timeout())
        result = self.checkin()
        self.assertFalse(result.ok)
        self.assertIn("未检测到今日奖励流水", result.line())
        self.assert_claim_count(1)

    def test_already_banner_without_ledger_is_unconfirmed(self):
        self.queue(BALANCE, *(response(ORIGINAL) for _ in range(4)))
        self.queue(DAILY, response(CLAIMED))
        result = self.checkin()
        self.assertFalse(result.ok)
        self.assertIn("未检测到今日奖励流水", result.line())
        self.assert_claim_count(0)

    def test_only_exact_type_positive_amount_and_beijing_date_count(self):
        wrong = ledger(
            transaction("2026-11-19 09:20:04 +08:00", "连续登录奖励", "200.0",
                        "5774.0", "20261119 的每日登录奖励 200 铜币"),
            transaction("2026-11-19 09:20:05 +08:00", "每日登录奖励", "-3.0",
                        "5771.0", "20261119 的每日登录奖励 3 铜币"),
            transaction("2026-11-18 23:59:00 +08:00", "每日登录奖励", "4.0",
                        "5578.0", "20261119 的每日登录奖励 4 铜币"),
            copper=71,
        )
        self.claim([response(wrong) for _ in range(3)], response(CLAIMED))
        self.queue(DAILY, response(MISSION), response(CLAIMED))
        result = self.checkin()
        self.assertFalse(result.ok)
        self.assert_claim_count(1)

    def test_external_claim_link_is_never_rebased_to_official_host(self):
        self.queue(BALANCE, *(response(ORIGINAL) for _ in range(4)))
        self.queue(DAILY, response('<a href="https://example.invalid' + REDEEM + '">领取</a>'))
        result = self.checkin()
        self.assertFalse(result.ok)
        self.assert_claim_count(0)

    def test_foreign_redirect_from_redeem_is_never_followed(self):
        self.claim([], response(status=302, location="https://example.invalid/"))
        result = self.checkin()
        self.assertFalse(result.ok)
        self.assert_claim_count(1)
        self.assertEqual(len(self.sent), 3)

    def test_reward_before_v2ex_morning_reset_does_not_block_new_reward(self):
        previous_period = transaction(
            "2026-11-18 22:12:00 +00:00", "每日登录奖励", "7.0", "5574.0",
            "20261118 的每日登录奖励 7 铜币",
        )
        self.queue(BALANCE, response(ledger(previous_period)),
                   response(ledger(TODAY, previous_period, copper=77)))
        self.queue(DAILY, response(MISSION))
        self.queue(REDEEM, response(status=302, location=DAILY))
        result = self.checkin()
        self.assertEqual(result.status, "success")
        self.assertIn("今日奖励：3 铜币", result.line())
        self.assert_claim_count(1)

    def test_existing_reward_on_next_ledger_page_skips_redeem(self):
        other = transaction(
            "2026-11-19 09:21:00 +08:00", "创建回复", "-8.0", "5569.0", "回复主题",
        )
        self.queue(BALANCE, response(ledger(other) + '<a href="/balance?p=2">2</a>'))
        self.queue(BALANCE + "?p=2", response(UPDATED))
        result = self.checkin()
        self.assertEqual(result.status, "already")
        self.assert_claim_count(0)

    def test_link_may_use_html_entities_and_once_in_a_different_position(self):
        link = "/mission/daily/redeem?source=mission&once=current123"
        self.queue(BALANCE, response(ORIGINAL), response(UPDATED))
        self.queue(DAILY, response(
            '<a href="https://[bad/mission/daily/redeem?once=bad">无效链接</a>'
            f'<a href="{BASE}{link.replace("&", "&amp;")}">领取</a>'
        ))
        self.queue(link, response(status=302, location=DAILY))
        result = self.checkin()
        self.assertEqual(result.status, "success")
        self.assert_claim_count(1)

    def test_reward_from_before_attempt_is_reported_as_already_claimed(self):
        previous = transaction(
            "2026-11-19 08:20:00 +08:00", "每日登录奖励", "3.0", "5577.0",
            "20261119 的每日登录奖励 3 铜币",
        )
        self.claim([response(ledger(previous, copper=77))])
        result = self.checkin()
        self.assertEqual(result.status, "already")
        self.assert_claim_count(1)

    def test_multi_account_wecom_payload_uses_each_accounts_result(self):
        self.claim([response(UPDATED)])
        self.queue(BALANCE, response(ledger(
            transaction("2026-11-19 08:20:00 +08:00", "每日登录奖励", "8.0",
                        "5562.0", "20261119 的每日登录奖励 8 铜币"), copper=62,
        )), account="second")
        self.queue(BALANCE, response(status=401), account="expired")
        with redirect_stdout(io.StringIO()):
            results = checkin.run_site("v2ex", [
                "PB3_SESSION=example; A2=first", "PB3_SESSION=example; A2=second",
                "PB3_SESSION=example; A2=expired",
            ])
        self.assertEqual([result.status for result in results], ["success", "already", "cookie_invalid"])
        self.assert_claim_count(1)
        post = self.patch_attribute(checkin.requests, "post")
        post.return_value = response('{"errcode": 0}')
        with patch.dict(checkin.os.environ, {"WECOM_WEBHOOK_KEY": "example_webhook"}):
            with redirect_stdout(io.StringIO()):
                checkin.wecom_notify(results + [
                    checkin.Result("NodeSeek", 1, True, "success", "5 个鸡腿")
                ])
        payload = post.call_args.kwargs["json"]["markdown"]["content"]
        self.assertIn("✅ **V2EX 账号1**", payload)
        self.assertIn("今日奖励：3 铜币", payload)
        self.assertIn("ℹ️ **V2EX 账号2**", payload)
        self.assertIn("今日奖励：8 铜币", payload)
        self.assertIn("❌ **V2EX 账号3**", payload)
        self.assertIn("状态：领取失败", payload)
        self.assertIn("原因：Cookie 无效或已过期", payload)
        self.assertIn("✅ **NodeSeek 账号1**", payload)
        self.assertNotIn("PB3_SESSION", payload)


if __name__ == "__main__":
    unittest.main()
