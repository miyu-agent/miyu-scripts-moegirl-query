# moegirl.py 的测试（审计 2026-11-18 #178 moegirl）。
#
# 跑法（在仓库根）：
#   python3 -I -B -m unittest discover -s .github/tests
# （不加 -t .：.github 不是合法的包名，加了 unittest 会报 Start directory is not importable。）
#
# 只用标准库，不联网：脚本自己的 get() 换成桩，喂构造的 API 响应。HOME、XDG_*、MIYU_* 指到临时目录。
# 放在 .github/tests/ 而不是 tests/：打包会跳过 .github，测试不会进包。

import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "moegirl.py"
API_ERROR = {"error": {"code": "maxlag", "info": "Waiting for a database server", "*": ""}}


def load():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("moegirl_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SearchApiError(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        env = {k: v for k, v in os.environ.items() if not k.startswith(("MIYU_", "XDG_"))}
        env.update(HOME=root, XDG_CONFIG_HOME=root, XDG_CACHE_HOME=root,
                   XDG_STATE_HOME=root, XDG_DATA_HOME=root)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mod = load()
        self.body = json.dumps(API_ERROR)
        self.mod.get = lambda url, accept: (url, self.body)

    # 审计 #178（moegirl）：opensearch 出错时 MediaWiki 回的是 {"error": {...}} 字典，
    # search() 取 data[1] 抛 KeyError: 1，只接了 JSONDecodeError/IndexError/TypeError；
    # 到 main 里被兜底成「错误：KeyError: 1」，看不出是萌娘百科那边出错。应报友好的 Failed。
    def test_search_api_error_is_friendly(self):
        with self.assertRaises(self.mod.Failed) as cm:
            self.mod.search("芙莉莲", 5)
        self.assertIn("萌娘百科", str(cm.exception))

    def test_main_reports_api_error_without_keyerror(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"query": "芙莉莲", "action": "search"}))), \
                redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            self.mod.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertNotIn("KeyError", out.getvalue())
        self.assertIn("萌娘百科", out.getvalue())

    # 防改坏（现在就绿）：正常的 opensearch 结果照常列出。
    def test_search_ok(self):
        self.body = json.dumps(["芙莉莲", ["芙莉莲", "芙莉莲(角色)"], ["", ""], ["", ""]])
        hits = self.mod.search("芙莉莲", 5)
        self.assertEqual([h["title"] for h in hits], ["芙莉莲", "芙莉莲(角色)"])


if __name__ == "__main__":
    unittest.main()
