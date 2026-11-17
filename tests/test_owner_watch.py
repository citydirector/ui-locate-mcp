"""owner_watch 的单元测试：不真的杀进程，只喂注入的 is_alive。"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import unittest

from ui_locate import owner_watch


class ParseOwnerTest(unittest.TestCase):
    def test_absent_and_invalid_values_are_none(self) -> None:
        for value in (None, "", "   ", "abc", "0", "-3", "12.5"):
            with self.subTest(value=value):
                self.assertIsNone(owner_watch.parse_owner(value))

    def test_positive_pid(self) -> None:
        self.assertEqual(owner_watch.parse_owner("123"), 123)
        self.assertEqual(owner_watch.parse_owner(" 456 "), 456)
        self.assertEqual(owner_watch.parse_owner(789), 789)


class WatchTest(unittest.TestCase):
    def test_strikes_trigger_gone_exactly_once(self) -> None:
        calls: list[int] = []
        gone = threading.Event()

        def on_gone() -> None:
            calls.append(1)
            gone.set()

        owner_watch.start(42, on_gone, is_alive=lambda _pid: False, interval=0.005, strikes=3)
        self.assertTrue(gone.wait(2.0))
        time.sleep(0.05)
        self.assertEqual(len(calls), 1)

    def test_alive_resets_the_streak(self) -> None:
        # 两死 → 一活（清零）→ 三死才收工。活的那次不清零就会误杀。
        answers = iter([False, False, True, False, False, False])
        gone = threading.Event()

        owner_watch.start(
            42,
            gone.set,
            is_alive=lambda _pid: next(answers, True),
            interval=0.005,
            strikes=3,
        )
        self.assertTrue(gone.wait(2.0))

    def test_stays_when_owner_lives(self) -> None:
        gone = threading.Event()
        owner_watch.start(42, gone.set, is_alive=lambda _pid: True, interval=0.005, strikes=3)
        self.assertFalse(gone.wait(0.2))


class ProbeTest(unittest.TestCase):
    def test_current_process_is_alive(self) -> None:
        self.assertTrue(owner_watch.probe(os.getpid()))

    def test_exited_process_is_not_alive(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=10)
        self.assertFalse(owner_watch.probe(child.pid))
