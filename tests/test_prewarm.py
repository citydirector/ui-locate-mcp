"""MCP 层拉起服务：并发时只拉一个、预热开关、后台预热失败不外抛。不起真服务。"""

from __future__ import annotations

import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_locate import config  # noqa: E402
from ui_locate import mcp_server as M  # noqa: E402


class FakeChild:
    returncode = None

    def poll(self):
        return None


class EnsureReady(unittest.TestCase):
    def test_concurrent_callers_spawn_once(self):
        state = {"spawned": 0, "ready": False}

        def spawn():
            state["spawned"] += 1
            threading.Timer(0.3, lambda: state.update(ready=True)).start()
            return FakeChild()

        probe = lambda: {"ready": True} if state["ready"] else None  # noqa: E731
        with mock.patch.object(M, "_probe", probe), mock.patch.object(M, "_spawn_server", spawn), \
                mock.patch.object(M, "_POLL_S", 0.02):
            results: list = []
            threads = [threading.Thread(target=lambda: results.append(M.ensure_ready(5))) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(state["spawned"], 1)
        self.assertEqual(len(results), 4)

    def test_already_ready_does_not_spawn(self):
        with mock.patch.object(M, "_probe", lambda: {"ready": True}), \
                mock.patch.object(M, "_spawn_server", side_effect=AssertionError("spawned")):
            self.assertTrue(M.ensure_ready(1)["ready"])

    def test_spawn_failure_fails_fast(self):
        def spawn():
            M._SPAWN_ERROR = "NotADirectoryError: cwd"
            return None

        with mock.patch.object(M, "_probe", lambda: None), mock.patch.object(M, "_spawn_server", spawn):
            import time as _t
            t0 = _t.monotonic()
            with self.assertRaises(RuntimeError) as ctx:
                M.ensure_ready(30)
        self.assertLess(_t.monotonic() - t0, 1.0)
        self.assertIn("NotADirectoryError", str(ctx.exception))

    def test_prewarm_swallows_errors(self):
        with mock.patch.object(M, "ensure_ready", side_effect=RuntimeError("boom")):
            M._prewarm()  # 不抛


class Switch(unittest.TestCase):
    def test_env_values(self):
        for value, want in (("1", True), ("on", True), ("TRUE", True), ("0", False), ("", False), ("no", False)):
            with mock.patch.dict(os.environ, {"UI_LOCATE_PREWARM": value}):
                self.assertEqual(config.prewarm(), want, value)

    def test_default_off_and_runtime_json(self):
        env = {k: v for k, v in os.environ.items() if k != "UI_LOCATE_PREWARM"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(config, "runtime_settings", lambda: {}):
                self.assertFalse(config.prewarm())
            with mock.patch.object(config, "runtime_settings", lambda: {"prewarm": True}):
                self.assertTrue(config.prewarm())


if __name__ == "__main__":
    unittest.main()
