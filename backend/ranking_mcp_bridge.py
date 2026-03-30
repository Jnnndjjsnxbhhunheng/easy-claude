#!/usr/bin/env python3
"""
本地 ranking MCP bridge。

将 stdio MCP 工具调用透传到用户本机运行的 ranking_mcp_service HTTP 服务。
`ranking_*` 是当前网关可用的 canonical 工具名；
`ranking.*` 和旧的 `mcp__ranking__*` 仍保留为兼容别名。
"""
import json
import os
import socket
from dataclasses import dataclass
from typing import Any
from urllib import error, request

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class RankingToolSpec:
    name: str
    description: str
    path: str
    input_schema: dict[str, Any]


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _schema(
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
    additional_properties: bool = True,
) -> dict[str, Any]:
    schema = {
        "type": "object",
        "description": description,
        "properties": properties,
        "additionalProperties": additional_properties,
    }
    if required:
        schema["required"] = required
    return schema


TOOL_SPECS: dict[str, RankingToolSpec] = {
    "ranking_query_understanding": RankingToolSpec(
        name="ranking_query_understanding",
        description="理解品牌榜查询意图，识别品类、排序维度、平台和 rank_type。",
        path="/api/v1/mcp/query-understanding",
        input_schema=_schema(
            "根据真实 ranking_mcp_service 的 OpenAPI，传入 query 和可选 source。",
            {
                "query": {"type": "string", "description": "用户原始查询，如“空气炸锅品牌榜”"},
                "source": {
                    "type": "string",
                    "enum": ["jd", "dxd", "sh"],
                    "description": "类目映射使用的数据源",
                    "default": "jd",
                },
            },
            required=["query"],
        ),
    ),
    "ranking_ugc_search_aiapi": RankingToolSpec(
        name="ranking_ugc_search_aiapi",
        description="调用 aiapi 做品牌 UGC 主召回。",
        path="/api/v1/ugc-search/aiapi",
        input_schema=_schema(
            "根据真实 ranking_mcp_service 的 OpenAPI，传入 query 及可选站点、时间和分页参数。",
            {
                "query": {"type": "string", "description": "召回查询词"},
                "sites": _nullable(
                    {
                        "type": "array",
                        "description": "站点过滤列表，例如 xiaohongshu.com / bilibili.com",
                        "items": {"type": "string"},
                    }
                ),
                "page_time": _nullable(
                    {
                        "type": "object",
                        "description": "时间过滤，例如 {'ge':'2024-01-01'}",
                        "additionalProperties": True,
                    }
                ),
                "main_size": {
                    "type": "integer",
                    "description": "主结果数量",
                    "minimum": 0,
                    "maximum": 100,
                    "default": 50,
                },
                "video_size": {
                    "type": "integer",
                    "description": "视频结果数量",
                    "minimum": 0,
                    "maximum": 100,
                    "default": 20,
                },
                "aladdin_size": {
                    "type": "integer",
                    "description": "阿拉丁结果数量",
                    "minimum": 0,
                    "maximum": 20,
                    "default": 5,
                },
                "main_from": {
                    "type": "integer",
                    "description": "主结果起始偏移",
                    "minimum": 0,
                    "default": 0,
                },
            },
            required=["query"],
        ),
    ),
    "ranking_ugc_search_uiapi": RankingToolSpec(
        name="ranking_ugc_search_uiapi",
        description="调用 uiapi 做品牌口碑调查与交叉验证。",
        path="/api/v1/ugc-search/uiapi",
        input_schema=_schema(
            "根据真实 ranking_mcp_service 的 OpenAPI，传入 query 和可选内容类型/页数。",
            {
                "query": {"type": "string", "description": "口碑调查查询词"},
                "pd": _nullable(
                    {
                        "type": "string",
                        "enum": ["video", "note"],
                        "description": "内容类型，None 表示综合结果",
                    }
                ),
                "pn_cnt": {
                    "type": "integer",
                    "description": "拉取页数",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 1,
                },
            },
            required=["query"],
        ),
    ),
    "ranking_hotsell_recall": RankingToolSpec(
        name="ranking_hotsell_recall",
        description="按品牌执行 hotsell 召回，补全结构化商品候选。",
        path="/api/v1/mcp/hotsell-recall",
        input_schema=_schema(
            "根据真实 ranking_mcp_service 的 OpenAPI，传入 query 和可选平台/排序/价格范围/分页参数。",
            {
                "query": {"type": "string", "description": "召回查询词"},
                "mall": _nullable(
                    {
                        "type": "string",
                        "enum": ["京东", "天猫"],
                        "description": "平台过滤",
                    }
                ),
                "sort_key": _nullable(
                    {
                        "type": "string",
                        "enum": ["sales", "price", "comments"],
                        "description": "排序字段",
                    }
                ),
                "sort_type": {
                    "type": "string",
                    "enum": ["desc", "asc"],
                    "description": "排序方向",
                    "default": "desc",
                },
                "price_start": _nullable({"type": "number", "description": "最低价，单位元"}),
                "price_end": _nullable({"type": "number", "description": "最高价，单位元"}),
                "pn": {"type": "integer", "description": "偏移量", "default": 0},
                "rn": {"type": "integer", "description": "单次召回条数", "default": 20},
            },
            required=["query"],
        ),
    ),
    "ranking_bmc_detail_enrich": RankingToolSpec(
        name="ranking_bmc_detail_enrich",
        description="补全 BMC 结构化商品详情。",
        path="/api/v1/mcp/bmc-detail-enrich",
        input_schema=_schema(
            "根据真实 ranking_mcp_service 的 OpenAPI，传入 bmc_ids。",
            {
                "bmc_ids": {
                    "type": "array",
                    "description": "待补全的 bmc_id 列表",
                    "items": {"type": "integer"},
                }
            },
            required=["bmc_ids"],
        ),
    ),
    "ranking_sh_detail_enrich": RankingToolSpec(
        name="ranking_sh_detail_enrich",
        description="补全 SH 结构化商品详情。",
        path="/api/v1/mcp/sh-detail-enrich",
        input_schema=_schema(
            "根据真实 ranking_mcp_service 的 OpenAPI，传入 sh_spu_ids。",
            {
                "sh_spu_ids": {
                    "type": "array",
                    "description": "待补全的识货 SPU ID 列表",
                    "items": {"type": "string"},
                }
            },
            required=["sh_spu_ids"],
        ),
    ),
    "ranking_brand_normalize": RankingToolSpec(
        name="ranking_brand_normalize",
        description="做品牌实体归一化与别名合并。",
        path="/api/v1/mcp/brand-normalize",
        input_schema=_schema(
            "根据真实 ranking_mcp_service 的 OpenAPI，传入品牌词字符串列表 brands。",
            {
                "brands": {
                    "type": "array",
                    "description": "品牌名列表",
                    "items": {"type": "string"},
                }
            },
            required=["brands"],
        ),
    ),
}

LEGACY_TOOL_ALIASES = {
    "ranking.query_understanding": "ranking_query_understanding",
    "ranking.ugc_search_aiapi": "ranking_ugc_search_aiapi",
    "ranking.ugc_search_uiapi": "ranking_ugc_search_uiapi",
    "ranking.hotsell_recall": "ranking_hotsell_recall",
    "ranking.bmc_detail_enrich": "ranking_bmc_detail_enrich",
    "ranking.sh_detail_enrich": "ranking_sh_detail_enrich",
    "ranking.brand_normalize": "ranking_brand_normalize",
    "mcp__ranking__query_understanding": "ranking_query_understanding",
    "mcp__ranking__ugc_search_aiapi": "ranking_ugc_search_aiapi",
    "mcp__ranking__ugc_search_uiapi": "ranking_ugc_search_uiapi",
    "mcp__ranking__hotsell_recall": "ranking_hotsell_recall",
    "mcp__ranking__bmc_detail_enrich": "ranking_bmc_detail_enrich",
    "mcp__ranking__sh_detail_enrich": "ranking_sh_detail_enrich",
    "mcp__ranking__brand_normalize": "ranking_brand_normalize",
}

app = Server("ranking-mcp-bridge")


def get_base_url() -> str:
    return os.environ.get("RANKING_MCP_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def get_timeout_seconds() -> float:
    raw = os.environ.get("RANKING_MCP_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
    try:
        timeout = float(raw)
    except ValueError:
        timeout = DEFAULT_TIMEOUT_SECONDS
    return timeout if timeout > 0 else DEFAULT_TIMEOUT_SECONDS


def build_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    bearer_token = os.environ.get("RANKING_MCP_BEARER_TOKEN", "").strip()
    if bearer_token:
        auth_header = os.environ.get("RANKING_MCP_AUTH_HEADER", "Authorization").strip()
        headers[auth_header or "Authorization"] = f"Bearer {bearer_token}"

    api_key = os.environ.get("RANKING_MCP_API_KEY", "").strip()
    if api_key:
        api_key_header = os.environ.get("RANKING_MCP_API_KEY_HEADER", "X-API-Key").strip()
        headers[api_key_header or "X-API-Key"] = api_key

    return headers


def invoke_tool(tool_name: str, arguments: dict[str, Any] | None) -> str:
    canonical_name = LEGACY_TOOL_ALIASES.get(tool_name, tool_name)
    spec = TOOL_SPECS.get(canonical_name)
    if spec is None:
        return f"未知工具: {tool_name}"

    payload = arguments if isinstance(arguments, dict) else {}
    url = f"{get_base_url()}{spec.path}"
    timeout = get_timeout_seconds()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url=url,
        data=body,
        headers=build_headers(),
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace").strip()
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        return (
            f"上游服务错误: tool={tool_name} status={exc.code} "
            f"url={url} detail={detail or exc.reason}"
        )
    except error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        return f"上游服务不可达: tool={tool_name} url={url} reason={reason}"
    except socket.timeout:
        return f"上游服务超时: tool={tool_name} timeout={timeout}s url={url}"
    except TimeoutError:
        return f"上游服务超时: tool={tool_name} timeout={timeout}s url={url}"
    except Exception as exc:
        return f"上游服务调用失败: tool={tool_name} url={url} error={exc}"

    if not raw:
        return "(无输出)"

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return json.dumps(parsed, ensure_ascii=False, indent=2)


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name=spec.name,
            description=spec.description,
            inputSchema=spec.input_schema,
        )
        for spec in TOOL_SPECS.values()
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    return [TextContent(type="text", text=invoke_tool(name, arguments))]


if __name__ == "__main__":
    import anyio

    async def main():
        async with stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options(),
            )

    anyio.run(main)
