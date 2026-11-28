"""MCP 层 locate_text 的请求体：默认不带 text_mode，服务端才会在 mosaic 没命中时整页回退。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_locate import mcp_server as M  # noqa: E402


class LocatePayload(unittest.TestCase):
    def _sent(self, **kw) -> dict:
        seen: list[dict] = []
        with mock.patch.object(M, "_post", lambda path, p: seen.append(p) or {"ok": False}):
            M.locate_text("规程", **kw)
        return seen[0]

    def test_default_omits_text_mode(self):
        self.assertNotIn("text_mode", self._sent())

    def test_explicit_text_mode_passed(self):
        self.assertEqual(self._sent(text_mode="mosaic")["text_mode"], "mosaic")
        self.assertEqual(self._sent(text_mode="full")["text_mode"], "full")


if __name__ == "__main__":
    unittest.main()
