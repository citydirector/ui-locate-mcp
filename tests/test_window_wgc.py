"""WGC 抓窗口：纯逻辑（裁客户区、黄框参数）和 window.grab 的回落顺序。"""

import unittest
from unittest import mock

import numpy as np

from ui_locate import window, window_wgc


class CropTests(unittest.TestCase):
    def test_crop_to_client(self):
        # 外框 (100,50)-(900,650) = 800x600；客户区从 (108, 81) 起 784x561
        box = window_wgc.client_crop_box((800, 600), (100, 50, 900, 650), (108, 81, 784, 561))
        self.assertEqual(box, (8, 31, 792, 592))

    def test_size_mismatch_means_whole_frame(self):
        self.assertIsNone(window_wgc.client_crop_box((1200, 900), (100, 50, 900, 650),
                                                     (108, 81, 784, 561)))

    def test_small_tolerance(self):
        box = window_wgc.client_crop_box((801, 600), (100, 50, 900, 650), (100, 50, 800, 600))
        self.assertEqual(box, (0, 0, 800, 600))

    def test_border_param(self):
        self.assertIs(window_wgc.border_param(22631), False)
        self.assertIsNone(window_wgc.border_param(19045))


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.win = window.WinInfo(hwnd=42, title="game", cls="x", pid=1, rect=(108, 81, 64, 48))
        p = mock.patch.object(window, "resolve", return_value=self.win)
        p.start()
        self.addCleanup(p.stop)

    def _img(self, v):
        return np.full((48, 64, 3), v, np.uint8)

    def test_auto_prefers_wgc(self):
        with mock.patch.object(window_wgc, "grab_client",
                               return_value=(self._img(120), (108, 81), {"cropped": True})), \
                mock.patch.object(window, "print_window") as pw:
            img, org = window.grab("hwnd:42", "auto")
        self.assertEqual(window.LAST["backend"], "window/wgc")
        self.assertEqual(org, (108, 81))
        pw.assert_not_called()

    def test_auto_black_wgc_falls_back_to_printwindow(self):
        with mock.patch.object(window_wgc, "grab_client",
                               return_value=(self._img(0), (108, 81), {"cropped": True})), \
                mock.patch.object(window, "print_window", return_value=self._img(90)):
            window.grab("hwnd:42", "auto")
        self.assertEqual(window.LAST["backend"], "window/printwindow")
        self.assertTrue(any(n.startswith("wgc:") for n in window.LAST["notes"]))

    def test_auto_wgc_error_falls_back(self):
        with mock.patch.object(window_wgc, "grab_client",
                               side_effect=window_wgc.WgcUnavailable("没装")), \
                mock.patch.object(window, "print_window", return_value=self._img(90)):
            window.grab("hwnd:42", "auto")
        self.assertEqual(window.LAST["backend"], "window/printwindow")

    def test_explicit_wgc_raises(self):
        with mock.patch.object(window_wgc, "grab_client",
                               side_effect=window_wgc.WgcUnavailable("没装")):
            with self.assertRaises(window.WindowError):
                window.grab("hwnd:42", "wgc")

    def test_explicit_printwindow_skips_wgc(self):
        with mock.patch.object(window_wgc, "grab_client") as g, \
                mock.patch.object(window, "print_window", return_value=self._img(90)):
            window.grab("hwnd:42", "printwindow")
        g.assert_not_called()

    def test_uncropped_wgc_uses_bounds_origin(self):
        with mock.patch.object(window_wgc, "grab_client",
                               return_value=(self._img(120), (100, 50), {"cropped": False})):
            _img, org = window.grab("hwnd:42", "auto")
        self.assertEqual(org, (100, 50))
        self.assertTrue(window.LAST["notes"])

    def test_pool_unavailable_off_windows(self):
        if window_wgc.available():
            self.skipTest("装了 windows-capture")
        with self.assertRaises(window_wgc.WgcUnavailable):
            window_wgc.POOL.frame(1)


if __name__ == "__main__":
    unittest.main()
