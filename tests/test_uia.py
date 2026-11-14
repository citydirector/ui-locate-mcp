"""UIA 适配器的测试：不碰真实窗口，也不依赖当前桌面上有什么。

覆盖三类东西：
  * GUID 解析这次最容易写错的手工字节搬运；
  * 线程/队列的三种结局 —— 不可用、忙、超时放弃；
  * ``_with_element`` 取不到元素、取到元素但没 pattern。

真实 ``ElementFromHandle`` 的成功路径在冒烟脚本里验（要一个真窗口），这里只
用替身，保证这些分支在任何机器上都能跑到。
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_locate import uia  # noqa: E402


class GuidTests(unittest.TestCase):
    def test_guid_round_trips_through_uuid(self):
        text = "30cbe57d-d9d0-452a-ab13-7ac5ac4825ee"
        value = uuid.UUID(text)
        guid = uia._guid(text)
        self.assertEqual(guid.Data1, value.time_low)
        self.assertEqual(guid.Data2, value.time_mid)
        self.assertEqual(guid.Data3, value.time_hi_version)
        self.assertEqual(bytes(guid.Data4), value.bytes[8:])

    def test_known_ids_are_parseable(self):
        for guid in (uia.CLSID_CUIAutomation, uia.IID_IUIAutomation):
            self.assertIsInstance(guid, uia._GUID)


class FakeWorker:
    def __init__(self, *, alive=True, busy=False, run=True):
        self.alive = alive
        self.busy = busy
        self.calls = 0
        self.retired = False
        self.error = None
        self._run = run
        self._uia = 0x1234
        self._q = self  # _submit 用 w._q.put(...)，这里让它指向自己

    def put(self, job):
        fn, box = job
        if not self._run:
            return  # 永远不返回：模拟卡在目标进程里的调用

        def go():
            try:
                box["result"] = fn(self._uia)
            except BaseException as exc:  # 与真实 worker 一致：异常进 box，不杀线程
                box["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                self.busy = False
                box["done"].set()

        threading.Thread(target=go, daemon=True).start()


class SubmitTests(unittest.TestCase):
    def test_unavailable_channel_reports_why(self):
        worker = FakeWorker(alive=False)
        worker.error = "RuntimeError: no ole32"
        with mock.patch.object(uia, "worker", lambda: worker):
            out = uia._submit(lambda _u: {"ok": True}, 100)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "unavailable")
        self.assertIn("no ole32", out["detail"])

    def test_busy_channel_refuses_instead_of_queueing(self):
        worker = FakeWorker(busy=True)
        with mock.patch.object(uia, "worker", lambda: worker):
            out = uia._submit(lambda _u: {"ok": True}, 100)
        self.assertEqual(out["reason"], "busy")
        self.assertEqual(worker.calls, 0)

    def test_timeout_is_reported_and_the_worker_is_retired(self):
        worker = FakeWorker(run=False)
        with mock.patch.object(uia, "worker", lambda: worker):
            out = uia._submit(lambda _u: {"ok": True}, 60)
        self.assertEqual(out["reason"], "timeout")
        self.assertTrue(worker.busy, "超时后不应复位 busy：旧调用多半还卡着")
        self.assertEqual(worker.calls, 1)
        self.assertTrue(worker.retired, "卡住的线程要退休，否则整个通道永久不可用")

    def test_after_a_retirement_the_next_call_gets_a_fresh_worker(self):
        stuck, fresh = FakeWorker(run=False), FakeWorker()
        with mock.patch.object(uia, "worker", lambda: stuck):
            self.assertEqual(uia._submit(lambda _u: {"ok": True}, 60)["reason"], "timeout")
        with mock.patch.object(uia, "worker", lambda: fresh):
            out = uia._submit(lambda _u: {"ok": True}, 500)
        self.assertTrue(out["ok"])
        self.assertFalse(fresh.retired)

    def test_successful_call_returns_result_and_clears_busy(self):
        worker = FakeWorker()
        with mock.patch.object(uia, "worker", lambda: worker):
            out = uia._submit(lambda _u: {"ok": True, "seen": _u}, 500)
        self.assertTrue(out["ok"])
        self.assertEqual(out["seen"], 0x1234)
        self.assertFalse(worker.busy)

    def test_worker_exception_becomes_error_not_crash(self):
        worker = FakeWorker()

        def boom(_u):
            raise RuntimeError("driver said no")

        with mock.patch.object(uia, "worker", lambda: worker):
            out = uia._submit(boom, 500)
        self.assertEqual(out["reason"], "error")
        self.assertIn("driver said no", out["detail"])


class ElementTests(unittest.TestCase):
    def test_missing_element_reports_hr(self):
        job = uia._with_element(0x4242, lambda _el: {"ok": True})
        with mock.patch.object(uia, "_element", return_value=(0, 0x80004005)):
            out = job(0x1234)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "no_element")
        self.assertIn("0x4242", out["detail"])

    def test_invoke_without_pattern_is_no_pattern(self):
        def body(element):
            pattern, hr = uia._pattern(element, uia.UIA_InvokePatternId)
            return {"ok": False, "reason": "no_pattern", "hr": hr}

        with mock.patch.object(uia, "_element", return_value=(0x99, 0)), \
                mock.patch.object(uia, "_pattern", return_value=(0, 0x80004005)), \
                mock.patch.object(uia, "_release"):
            out = uia._with_element(0x4242, body)(0x1234)
        self.assertEqual(out["reason"], "no_pattern")

    def test_element_is_always_released(self):
        released: list[int] = []
        job = uia._with_element(0x4242, lambda _el: {"ok": True})
        with mock.patch.object(uia, "_element", return_value=(0x99, 0)), \
                mock.patch.object(uia, "_release", lambda ptr: released.append(ptr)):
            job(0x1234)
        self.assertEqual(released, [0x99])

    def test_element_is_released_even_when_body_raises(self):
        released: list[int] = []

        def boom(_el):
            raise RuntimeError("pattern blew up")

        job = uia._with_element(0x4242, boom)
        with mock.patch.object(uia, "_element", return_value=(0x99, 0)), \
                mock.patch.object(uia, "_release", lambda ptr: released.append(ptr)):
            with self.assertRaises(RuntimeError):
                job(0x1234)
        self.assertEqual(released, [0x99])


class RealWorkerSmokeTests(unittest.TestCase):
    """只断言"不炸"，不断言这台机器上一定有 UIA。"""

    def test_state_shape(self):
        st = uia.state()
        self.assertIn("available", st)
        self.assertIn("busy", st)
        self.assertIn("calls", st)
        self.assertIsInstance(st["available"], bool)

    def test_invoke_on_bogus_handle_fails_cleanly(self):
        out = uia.invoke(0, timeout_ms=400)
        self.assertFalse(out["ok"])
        self.assertIn(out["reason"],
                      {"no_element", "unavailable", "timeout", "busy", "error"})


if __name__ == "__main__":
    unittest.main()
