import asyncio
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WORKDIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = WORKDIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import ranking_mcp_bridge
from mcp_manager import MCPManager


class _RecordingHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []
    status_code = 200
    response_body: object = {"ok": True}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length).decode("utf-8")
        self.__class__.requests.append(
            {
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": json.loads(raw_body),
            }
        )
        self.send_response(self.__class__.status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        payload = json.dumps(self.__class__.response_body, ensure_ascii=False).encode("utf-8")
        self.wfile.write(payload)

    def log_message(self, format, *args):
        return


class RankingBridgeTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
        _RecordingHandler.requests = []
        _RecordingHandler.status_code = 200
        _RecordingHandler.response_body = {"ok": True}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        self.original_base_url = ranking_mcp_bridge.os.environ.get("RANKING_MCP_BASE_URL")
        self.original_timeout = ranking_mcp_bridge.os.environ.get("RANKING_MCP_TIMEOUT_SECONDS")
        self.original_bearer = ranking_mcp_bridge.os.environ.get("RANKING_MCP_BEARER_TOKEN")
        self.original_api_key = ranking_mcp_bridge.os.environ.get("RANKING_MCP_API_KEY")
        self.original_api_key_header = ranking_mcp_bridge.os.environ.get("RANKING_MCP_API_KEY_HEADER")
        ranking_mcp_bridge.os.environ["RANKING_MCP_BASE_URL"] = f"http://127.0.0.1:{self.server.server_port}"
        ranking_mcp_bridge.os.environ["RANKING_MCP_TIMEOUT_SECONDS"] = "5"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self._restore_env("RANKING_MCP_BASE_URL", self.original_base_url)
        self._restore_env("RANKING_MCP_TIMEOUT_SECONDS", self.original_timeout)
        self._restore_env("RANKING_MCP_BEARER_TOKEN", self.original_bearer)
        self._restore_env("RANKING_MCP_API_KEY", self.original_api_key)
        self._restore_env("RANKING_MCP_API_KEY_HEADER", self.original_api_key_header)

    def _restore_env(self, key: str, value: str | None):
        if value is None:
            ranking_mcp_bridge.os.environ.pop(key, None)
        else:
            ranking_mcp_bridge.os.environ[key] = value

    def test_tool_specs_match_strict_skill_names(self):
        self.assertEqual(
            list(ranking_mcp_bridge.TOOL_SPECS.keys()),
            [
                "ranking_query_understanding",
                "ranking_ugc_search_aiapi",
                "ranking_ugc_search_uiapi",
                "ranking_hotsell_recall",
                "ranking_bmc_detail_enrich",
                "ranking_sh_detail_enrich",
                "ranking_brand_normalize",
            ],
        )
        self.assertEqual(
            ranking_mcp_bridge.LEGACY_TOOL_ALIASES["mcp__ranking__query_understanding"],
            "ranking_query_understanding",
        )
        self.assertEqual(
            ranking_mcp_bridge.TOOL_SPECS["ranking_brand_normalize"].input_schema["required"],
            ["brands"],
        )
        self.assertEqual(
            ranking_mcp_bridge.TOOL_SPECS["ranking_brand_normalize"].input_schema["properties"]["brands"]["items"]["type"],
            "string",
        )
        self.assertEqual(
            ranking_mcp_bridge.TOOL_SPECS["ranking_bmc_detail_enrich"].input_schema["required"],
            ["bmc_ids"],
        )
        self.assertEqual(
            ranking_mcp_bridge.TOOL_SPECS["ranking_sh_detail_enrich"].input_schema["required"],
            ["sh_spu_ids"],
        )

    def test_query_understanding_proxy_posts_json_to_expected_endpoint(self):
        result = ranking_mcp_bridge.invoke_tool(
            "ranking_query_understanding",
            {"query": "空气炸锅品牌榜", "mall": "京东"},
        )

        self.assertIn('"ok": true', result.lower())
        self.assertEqual(len(_RecordingHandler.requests), 1)
        self.assertEqual(_RecordingHandler.requests[0]["path"], "/api/v1/mcp/query-understanding")
        self.assertEqual(
            _RecordingHandler.requests[0]["body"],
            {"query": "空气炸锅品牌榜", "mall": "京东"},
        )

    def test_optional_auth_headers_are_forwarded(self):
        ranking_mcp_bridge.os.environ["RANKING_MCP_BEARER_TOKEN"] = "token-123"
        ranking_mcp_bridge.os.environ["RANKING_MCP_API_KEY"] = "key-456"
        ranking_mcp_bridge.os.environ["RANKING_MCP_API_KEY_HEADER"] = "X-Test-Key"

        ranking_mcp_bridge.invoke_tool("ranking_ugc_search_aiapi", {"query": "foo"})

        headers = _RecordingHandler.requests[0]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer token-123")
        self.assertEqual(headers["X-Test-Key"], "key-456")

    def test_http_errors_return_diagnostic_text(self):
        _RecordingHandler.status_code = 502
        _RecordingHandler.response_body = {"detail": "bad gateway"}

        result = ranking_mcp_bridge.invoke_tool("ranking_ugc_search_uiapi", {"query": "foo"})

        self.assertIn("上游服务错误", result)
        self.assertIn("status=502", result)
        self.assertIn("/api/v1/ugc-search/uiapi", result)

    def test_legacy_alias_hits_same_endpoint(self):
        canonical_result = ranking_mcp_bridge.invoke_tool("ranking_hotsell_recall", {"query": "foo"})
        legacy_result = ranking_mcp_bridge.invoke_tool("mcp__ranking__hotsell_recall", {"query": "foo"})

        self.assertIn('"ok": true', canonical_result.lower())
        self.assertIn('"ok": true', legacy_result.lower())
        self.assertEqual(_RecordingHandler.requests[0]["path"], "/api/v1/mcp/hotsell-recall")
        self.assertEqual(_RecordingHandler.requests[1]["path"], "/api/v1/mcp/hotsell-recall")

    def test_mcp_manager_accepts_brand_normalize_string_array(self):
        async def run_test():
            manager = MCPManager(
                [
                    {
                        "name": "ranking",
                        "command": sys.executable,
                        "args": ["backend/ranking_mcp_bridge.py"],
                    }
                ]
            )
            await manager.initialize()
            try:
                result = await manager.call_tool(
                    "ranking_brand_normalize",
                    {"brands": ["九阳", "Joyoung"]},
                )
            finally:
                await manager.close()
            return result

        result = asyncio.run(run_test())
        self.assertIn('"ok": true', result.lower())
        self.assertEqual(_RecordingHandler.requests[0]["path"], "/api/v1/mcp/brand-normalize")
        self.assertEqual(_RecordingHandler.requests[0]["body"], {"brands": ["九阳", "Joyoung"]})


if __name__ == "__main__":
    unittest.main()
