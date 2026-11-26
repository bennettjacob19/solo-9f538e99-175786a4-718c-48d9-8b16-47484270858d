"""端到端单元测试：升档核算、补报/更正、64 组上限、回退与持久化。

运行：python -m unittest discover -s tests -v
"""
import os
import random
import tempfile
import unittest
from datetime import datetime, timezone

_TMP = tempfile.mkdtemp(prefix="gray-test-")
os.environ["GRAY_DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["GRAY_STABLE_PORT"] = "18181"
os.environ["GRAY_CANDIDATE_PORT"] = "18182"

from app import create_app  # noqa: E402
from app import services  # noqa: E402
from app.db import connect, transaction  # noqa: E402
from app.gateway import (  # noqa: E402
    CANDIDATE_VERSION,
    STABLE_VERSION,
    choose_target,
    start_answer_services,
    trial_request,
)
from app.timeutil import ValidationError, minute_key, span_minutes  # noqa: E402

UTC = timezone.utc


def dt(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


class _FixedRng:
    def __init__(self, value):
        self.value = value

    def randint(self, a, b):
        assert a <= self.value <= b
        return self.value


class GrayReleaseTestBase(unittest.TestCase):
    def setUp(self):
        # 每个用例一个全新库文件
        self.db_path = os.path.join(_TMP, f"{self._testMethodName}.db")
        os.environ["GRAY_DB_PATH"] = self.db_path
        self.conn = connect(self.db_path)
        from app.db import init_db
        init_db(self.db_path)
        start_answer_services()

    def tearDown(self):
        self.conn.close()

    def insert_minute(self, when, requests, failures):
        self.conn.execute(
            "INSERT INTO minute_metrics (minute, day, requests, failures) "
            "VALUES (?,?,?,?)",
            (minute_key(when), when.strftime("%Y-%m-%d"), requests, failures))
        self.conn.execute(
            "INSERT INTO daily_summary (day, requests, failures) VALUES (?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET requests = requests + excluded.requests, "
            "failures = failures + excluded.failures",
            (when.strftime("%Y-%m-%d"), requests, failures))
        self.conn.commit()

    def state(self):
        return services.get_state(self.conn)


class RampTests(GrayReleaseTestBase):
    def test_successful_ramp_activates_tier(self):
        for i in range(5):
            self.insert_minute(dt(2026, 1, 1, 0, i), 100, 1)  # 500 请求 / 5 失败 = 1%
        result = services.submit_ramp(self.conn, dt(2026, 1, 1),
                                      dt(2026, 1, 1, 0, 4), 2000, 500)
        self.assertTrue(result["accepted"])
        st = self.state()
        self.assertEqual(st["active"], 1)
        self.assertEqual(st["target_ratio_bp"], 2000)
        self.assertEqual(st["error_limit_bp"], 500)

    def test_ramp_rejected_when_minutes_missing(self):
        for i in (0, 2, 4):  # 缺 01、03 两分钟
            self.insert_minute(dt(2026, 1, 1, 0, i), 100, 1)
        result = services.submit_ramp(self.conn, dt(2026, 1, 1),
                                      dt(2026, 1, 1, 0, 4), 2000, 500)
        self.assertFalse(result["accepted"])
        self.assertIn("漏报", result["message"])
        self.assertEqual(self.state()["active"], 0)

    def test_ramp_rejected_when_zero_requests(self):
        for i in range(3):
            self.insert_minute(dt(2026, 2, 1, 0, i), 0, 0)
        result = services.submit_ramp(self.conn, dt(2026, 2, 1),
                                      dt(2026, 2, 1, 0, 2), 2000, 500)
        self.assertFalse(result["accepted"])
        self.assertIn("总请求数为零", result["message"])

    def test_ramp_rejected_when_over_limit(self):
        self.insert_minute(dt(2026, 3, 1), 100, 6)  # 6% > 5%
        result = services.submit_ramp(self.conn, dt(2026, 3, 1),
                                      dt(2026, 3, 1), 2000, 500)
        self.assertFalse(result["accepted"])
        self.assertIn("超限", result["message"])
        self.assertEqual(self.state()["active"], 0)

    def test_limit_boundary_is_inclusive(self):
        # 恰好 5% 不超限（> 才超限）
        self.insert_minute(dt(2026, 3, 2), 100, 5)
        result = services.submit_ramp(self.conn, dt(2026, 3, 2),
                                      dt(2026, 3, 2), 2000, 500)
        self.assertTrue(result["accepted"])

    def test_cross_year_and_bad_window(self):
        with self.assertRaises(ValidationError):
            services.submit_ramp(self.conn, dt(2026, 12, 31, 23, 59),
                                 dt(2027, 1, 1), 2000, 500)
        with self.assertRaises(ValidationError):
            services.submit_ramp(self.conn, dt(2026, 5, 2), dt(2026, 5, 1),
                                 2000, 500)

    def test_full_year_minute_span_supported(self):
        # 闰年全年分钟数；未补报时整段按漏报拒绝而不是报错
        start, end = dt(2024, 1, 1), dt(2024, 12, 31, 23, 59)
        self.assertEqual(span_minutes(start, end), 366 * 24 * 60)
        totals = services.window_totals(self.conn, start, end)
        self.assertEqual(totals["expected"], 527040)
        self.assertEqual(totals["missing"], 527040)
        result = services.submit_ramp(self.conn, start, end, 2000, 500)
        self.assertFalse(result["accepted"])
        self.assertIn("漏报", result["message"])


class CorrectionTests(GrayReleaseTestBase):
    def _active_tier(self):
        for i in range(5):
            self.insert_minute(dt(2026, 1, 1, 0, i), 100, 1)
        services.submit_ramp(self.conn, dt(2026, 1, 1),
                             dt(2026, 1, 1, 0, 4), 2000, 500)
        self.assertEqual(self.state()["active"], 1)

    def test_backfill_creates_minutes_and_summary_groups(self):
        rows = [(minute_key(dt(2026, 4, 1, 0, i)), "10", "1") for i in range(3)]
        result = services.submit_correction(self.conn, rows)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["checked_groups"], 1)
        self.assertEqual(result["changed_groups"], 1)
        self.assertEqual(result["missed_groups"], 1)  # 新天，汇总未命中也计
        summary = self.conn.execute(
            "SELECT * FROM daily_summary WHERE day='2026-04-01'").fetchone()
        self.assertEqual((summary["requests"], summary["failures"]), (30, 3))

    def test_correction_over_limit_rolls_back_tier_and_routes_stable_only(self):
        self._active_tier()
        # 把 00:00 的失败从 1 改成 50：合计 500/54 = 10.8% > 5%
        result = services.submit_correction(
            self.conn, [(minute_key(dt(2026, 1, 1, 0, 0)), "100", "50")])
        self.assertTrue(result["accepted"])
        self.assertIsNotNone(result["rollback"])
        self.assertIn("超限", result["rollback"])
        self.assertEqual(self.state()["active"], 0)
        # 日汇总按差值维护：500 请求 / 54 失败
        summary = self.conn.execute(
            "SELECT * FROM daily_summary WHERE day='2026-01-01'").fetchone()
        self.assertEqual((summary["requests"], summary["failures"]), (500, 54))
        # 回退后任何随机数都只能走稳定版
        for value in (1, 2000, 9999, 10000):
            self.assertEqual(choose_target(False, 2000, _FixedRng(value)),
                             "stable")

    def test_correction_to_zero_requests_rolls_back(self):
        self._active_tier()
        rows = [(minute_key(dt(2026, 1, 1, 0, i)), "0", "0") for i in range(5)]
        result = services.submit_correction(self.conn, rows)
        self.assertIn("零请求", result["rollback"])
        self.assertEqual(self.state()["active"], 0)

    def test_re_ramp_required_after_rollback(self):
        self._active_tier()
        services.submit_correction(
            self.conn, [(minute_key(dt(2026, 1, 1, 0, 0)), "100", "50")])
        self.assertEqual(self.state()["active"], 0)
        # 数据修复后不会自动恢复档位，必须重新升档
        fixed = services.submit_correction(
            self.conn, [(minute_key(dt(2026, 1, 1, 0, 0)), "100", "1")])
        self.assertTrue(fixed["accepted"])
        self.assertEqual(self.state()["active"], 0)
        again = services.submit_ramp(self.conn, dt(2026, 1, 1),
                                     dt(2026, 1, 1, 0, 4), 2000, 500)
        self.assertTrue(again["accepted"])
        self.assertEqual(self.state()["active"], 1)

    def test_correction_outside_tier_window_keeps_tier(self):
        self._active_tier()
        # 更正窗口之外的分钟，不触发重估，档位保持
        result = services.submit_correction(
            self.conn, [(minute_key(dt(2026, 1, 2, 0, 0)), "100", "99")])
        self.assertIsNone(result["rollback"])
        self.assertEqual(self.state()["active"], 1)

    def test_64_group_cap_blocks_and_counts_misses(self):
        # 65 个不同天 → 拒绝且不写入
        rows = [(minute_key(dt(2026, 1, 1).fromordinal(
            dt(2026, 1, 1).toordinal() + i)), "10", "0") for i in range(65)]
        result = services.submit_correction(self.conn, rows)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["checked_groups"], 65)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM minute_metrics").fetchone()["c"], 0)

        # 恰好 64 组 → 接受，64 组全部未命中、全部改动
        ok_rows = rows[:64]
        ok = services.submit_correction(self.conn, ok_rows)
        self.assertTrue(ok["accepted"])
        self.assertEqual(ok["checked_groups"], 64)
        self.assertEqual(ok["changed_groups"], 64)
        self.assertEqual(ok["missed_groups"], 64)

        # 第二次同样 64 天：全部命中，改动仍 64（差值可能为 0 也计为改过的组）
        again = services.submit_correction(
            self.conn, [(m, "10", "0") for m, _, _ in ok_rows])
        self.assertEqual(again["checked_groups"], 64)
        self.assertEqual(again["missed_groups"], 0)
        self.assertEqual(again["changed_groups"], 64)

    def test_duplicate_minute_in_one_submission_rejected(self):
        key = minute_key(dt(2026, 5, 1))
        with self.assertRaises(ValidationError):
            services.submit_correction(
                self.conn, [(key, "10", "1"), (key, "20", "2")])

    def test_failures_gt_requests_rejected_without_write(self):
        with self.assertRaises(ValidationError):
            services.submit_correction(
                self.conn, [(minute_key(dt(2026, 5, 2)), "1", "2")])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM minute_metrics").fetchone()["c"], 0)

    def test_transaction_rollback_on_write_failure(self):
        with self.assertRaises(RuntimeError):
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO daily_summary (day, requests, failures) "
                    "VALUES ('2026-06-01', 1, 0)")
                raise RuntimeError("simulated write failure")
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM daily_summary WHERE day='2026-06-01'").fetchone())


class GatewayAndPersistenceTests(GrayReleaseTestBase):
    def test_gateway_ratio_boundary(self):
        self.assertEqual(choose_target(True, 5000, _FixedRng(5000)),
                         "candidate")  # 含边界
        self.assertEqual(choose_target(True, 5000, _FixedRng(5001)), "stable")
        self.assertEqual(choose_target(True, 10000, _FixedRng(10000)),
                         "candidate")

    def test_real_trial_request_hits_local_answer_services(self):
        # 100% 候选版 → 应答版本必须是 candidate
        self.insert_minute(dt(2026, 7, 1), 100, 0)
        services.submit_ramp(self.conn, dt(2026, 7, 1), dt(2026, 7, 1),
                             10000, 500)
        payload = trial_request(True, 10000, random.Random(0))
        self.assertEqual(payload["version"], CANDIDATE_VERSION)
        # 回退后 → 稳定版
        payload = trial_request(False, 10000, random.Random(0))
        self.assertEqual(payload["version"], STABLE_VERSION)

    def test_state_survives_app_restart(self):
        self.insert_minute(dt(2026, 8, 1), 100, 0)
        services.submit_ramp(self.conn, dt(2026, 8, 1), dt(2026, 8, 1),
                             3000, 500)
        self.conn.close()
        # 模拟重启：新连接、新 app 实例读同一个 SQLite 文件
        app = create_app(start_services=False)
        client = app.test_client()
        page = client.get("/").get_data(as_text=True)
        self.assertIn("已升档", page)
        self.assertIn("30.00%", page)
        self.conn = connect(self.db_path)
        self.assertEqual(self.state()["target_ratio_bp"], 3000)


class HttpFlowTests(GrayReleaseTestBase):
    def setUp(self):
        super().setUp()
        self.app = create_app(start_services=True)
        self.client = self.app.test_client()

    def test_page_renders(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("灰度升档演练", resp.get_data(as_text=True))

    def test_ramp_then_correction_rollback_via_http(self):
        # 先通过页面补报 3 个分钟
        rows = [(f"2026-09-01T00:0{i}", "100", "1") for i in range(3)]
        resp = self.client.post("/correct", data={
            "minute[]": [r[0] for r in rows],
            "requests[]": [r[1] for r in rows],
            "failures[]": [r[2] for r in rows],
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("核对汇总 1 组", resp.get_data(as_text=True))

        # 升档成功
        resp = self.client.post("/ramp", data={
            "window_start": "2026-09-01T00:00",
            "window_end": "2026-09-01T00:02",
            "target_ratio": "20.00",
            "error_limit": "5.00",
        }, follow_redirects=True)
        self.assertIn("升档成功", resp.get_data(as_text=True))

        # 更正到超限 → 页面提示回退
        resp = self.client.post("/correct", data={
            "minute[]": ["2026-09-01T00:00"],
            "requests[]": ["100"],
            "failures[]": ["40"],  # 合计 300/42 = 14%
        }, follow_redirects=True)
        text = resp.get_data(as_text=True)
        self.assertIn("触发回退", text)
        self.assertIn("原档 / 已回退", text)
        self.assertIn("核对查过", text)

        # 试请求走真实本地应答服务，回退后必为稳定版
        resp = self.client.post("/trial", follow_redirects=True)
        self.assertIn(STABLE_VERSION, resp.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
