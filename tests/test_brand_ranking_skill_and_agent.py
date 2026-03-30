import asyncio
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace

WORKDIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = WORKDIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from agent import (
    AgentRunner,
    DEFAULT_MODEL_TIMEOUT_SECONDS,
    LONG_WORKFLOW_MODEL_TIMEOUT_SECONDS,
    extract_output_text,
    format_api_error,
    is_missing_response_completed_error,
    is_retryable_api_error,
    truncate_tool_output_for_context,
)
from skill_loader import SkillLoader

SKILLS_DIR = WORKDIR / "skills"
BRAND_RANKING_DIR = SKILLS_DIR / "brand-ranking"


def _tool(name: str) -> dict:
    return {"type": "function", "name": name, "parameters": {"type": "object"}}


def _write_skill(
    root: Path,
    *,
    name: str,
    description: str,
    body: str,
    files: dict[str, str] | None = None,
) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent(
            f"""\
            ---
            name: {name}
            description: {description}
            ---

            {body}
            """
        ),
        encoding="utf-8",
    )
    for relative_path, content in (files or {}).items():
        path = skill_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return skill_dir


class SkillLoaderTests(unittest.TestCase):
    def test_brand_ranking_skill_is_discoverable(self):
        loader = SkillLoader(SKILLS_DIR)
        self.assertIn("brand-ranking", loader.list_names())
        self.assertIn("品牌维度电商排行任务", loader.skills["brand-ranking"]["meta"]["description"])

    def test_brand_ranking_skill_contains_full_remote_workflow_and_local_scripts(self):
        text = (BRAND_RANKING_DIR / "SKILL.md").read_text(encoding="utf-8")

        for step in range(1, 9):
            self.assertIn(f"### Step {step}", text)
        self.assertIn("## Tool Usage Quick Reference", text)
        self.assertIn("## Configuration", text)
        self.assertIn("不要调用任何 query-understanding MCP 工具", text)
        self.assertIn("ranking_bmc_detail_enrich", text)
        self.assertNotIn("ranking_ugc_search_uiapi", text)
        self.assertNotIn("mcp__ranking__", text)
        self.assertIn("scripts/build_hotsell_plan.py", text)
        self.assertIn("scripts/merge_hotsell_results.py", text)
        self.assertIn("scripts/check_hotsell_coverage.py", text)

    def test_loader_extracts_brand_ranking_profile_from_skill_md(self):
        loader = SkillLoader(SKILLS_DIR)
        profile = loader.get_profile("brand-ranking")

        self.assertCountEqual(
            profile["referenced_files"],
            [
                "references/semantic-annotation.md",
                "references/scoring-formula.md",
                "references/output-template.md",
            ],
        )
        self.assertCountEqual(
            profile["executable_files"],
            [
                "scripts/build_hotsell_plan.py",
                "scripts/merge_hotsell_results.py",
                "scripts/check_hotsell_coverage.py",
            ],
        )
        self.assertNotIn("ranking_query_understanding", profile["mentioned_tools"])
        self.assertNotIn("ranking_ugc_search_uiapi", profile["mentioned_tools"])
        self.assertIn("ranking_bmc_detail_enrich", profile["mentioned_tools"])
        self.assertIn("ranking_brand_normalize", profile["mentioned_tools"])

    def test_loader_parses_nonstandard_directories_based_on_skill_md_references(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_skill(
                root,
                name="sample-skill",
                description="test progressive loading",
                body="""
                Read `docs/guide.md` first, then apply `prompts/template.md`.
                Run `tools/run_flow.py` when execution is needed.
                You may call `external_lookup`.
                Keep `assets/example.csv` for later inspection.
                Ignore `missing/not-found.md` because it does not exist.
                """,
                files={
                    "docs/guide.md": "# Guide\n",
                    "prompts/template.md": "template\n",
                    "tools/run_flow.py": "print('ok')\n",
                    "assets/example.csv": "col\nvalue\n",
                },
            )

            loader = SkillLoader(root)
            profile = loader.get_profile("sample-skill")

            self.assertEqual(
                profile["referenced_files"],
                ["docs/guide.md", "prompts/template.md"],
            )
            self.assertEqual(profile["executable_files"], ["tools/run_flow.py"])
            self.assertIn("external_lookup", profile["mentioned_tools"])
            self.assertEqual(profile["resource_hints"]["other_files"], ["assets/example.csv"])


class AgentSkillToolExpansionTests(unittest.TestCase):
    def test_brand_ranking_query_no_longer_forces_ranking_tools(self):
        runner = AgentRunner()
        runner.mcp = SimpleNamespace(
            tools=[
                _tool("ranking_query_understanding"),
                _tool("ranking_ugc_search_aiapi"),
                _tool("ranking_bmc_detail_enrich"),
            ],
            is_mcp_tool=lambda name: name.startswith("ranking_"),
        )

        selected = runner._select_tools("请做一个空气炸锅品牌榜，并分析什么牌子好")
        names = [tool["name"] for tool in selected]

        self.assertIn("load_skill", names)
        self.assertNotIn("ranking_query_understanding", names)
        self.assertNotIn("ranking_ugc_search_aiapi", names)

    def test_loaded_skill_opens_read_and_exec_tools_from_skill_md_profile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_skill(
                root,
                name="sample-skill",
                description="test dynamic tools",
                body="""
                Read `docs/guide.md`.
                Run `tools/run_flow.py`.
                Call `external_lookup` if needed.
                """,
                files={
                    "docs/guide.md": "# Guide\n",
                    "tools/run_flow.py": "print('ok')\n",
                },
            )
            loader = SkillLoader(root)

            runner = AgentRunner()
            runner.skills = loader
            runner.mcp = SimpleNamespace(
                tools=[_tool("external_lookup")],
                is_mcp_tool=lambda name: name == "external_lookup",
            )

            before = [tool["name"] for tool in runner._select_tools("请处理这个任务")]
            after = [tool["name"] for tool in runner._select_tools("请处理这个任务", {"sample-skill"})]

            self.assertIn("load_skill", before)
            self.assertNotIn("read_file", before)
            self.assertNotIn("bash", before)
            self.assertNotIn("external_lookup", before)

            self.assertIn("read_file", after)
            self.assertIn("bash", after)
            self.assertIn("external_lookup", after)

    def test_loaded_skill_without_attachment_files_does_not_add_read_or_bash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_skill(
                root,
                name="light-skill",
                description="tool-only skill",
                body="""
                Call `external_lookup` when you need remote data.
                """,
            )
            loader = SkillLoader(root)

            runner = AgentRunner()
            runner.skills = loader
            runner.mcp = SimpleNamespace(
                tools=[_tool("external_lookup")],
                is_mcp_tool=lambda name: name == "external_lookup",
            )

            names = [tool["name"] for tool in runner._select_tools("请处理这个任务", {"light-skill"})]

            self.assertIn("load_skill", names)
            self.assertIn("external_lookup", names)
            self.assertNotIn("read_file", names)
            self.assertNotIn("bash", names)

    def test_system_prompt_describes_generic_progressive_skill_loading(self):
        runner = AgentRunner()
        prompt = runner._build_system_prompt()

        self.assertIn("先根据用户请求和下方 skill 描述，自主判断是否需要调用 load_skill", prompt)
        self.assertIn("不会自动展开整个 skill 目录", prompt)
        self.assertIn("只读取当前步骤真正需要的那些文件", prompt)
        self.assertIn("只在当前步骤需要时再运行", prompt)
        self.assertNotIn('优先调用 load_skill(name="brand-ranking")', prompt)
        self.assertIn("必读 / 必跑", prompt)

    def test_loaded_skill_read_file_falls_back_to_real_skill_directory(self):
        runner = AgentRunner()

        output = asyncio.run(
            runner._dispatch_tool(
                "read_file",
                {
                    "path": str(WORKDIR / "brand-ranking" / "references" / "semantic-annotation.md"),
                    "limit": 20,
                },
                {"brand-ranking"},
            )
        )

        self.assertIn("语义标注规则", output)

    def test_loaded_skill_bash_rewrites_skill_relative_script_path(self):
        runner = AgentRunner()

        rewritten = runner._rewrite_bash_command_for_loaded_skills(
            "python scripts/build_hotsell_plan.py",
            {"brand-ranking"},
        )

        self.assertIn("/skills/brand-ranking/scripts/build_hotsell_plan.py", rewritten)
        self.assertEqual(rewritten.count("/skills/brand-ranking/scripts/build_hotsell_plan.py"), 1)

    def test_loaded_skill_followup_lists_skill_root_and_attachments(self):
        runner = AgentRunner()

        followup = runner._build_loaded_skill_followup("brand-ranking")

        self.assertIn("skill_root=", followup)
        self.assertIn("referenced_files=references/semantic-annotation.md", followup)
        self.assertIn("executable_files=scripts/build_hotsell_plan.py", followup)

    def test_truncate_tool_output_for_context_limits_large_results(self):
        output = truncate_tool_output_for_context("ranking_ugc_search_aiapi", "x" * 20000)

        self.assertLess(len(output), 13000)
        self.assertIn("已截断", output)

    def test_extract_output_text_accepts_string_response_shape(self):
        self.assertEqual(extract_output_text("summary text"), "summary text")

    def test_format_api_error_makes_timeout_diagnostic(self):
        message = format_api_error(asyncio.TimeoutError(), 20)

        self.assertEqual(message, "模型调用超时（20s）")

    def test_missing_response_completed_event_is_retryable(self):
        exc = RuntimeError("Didn't receive a `response.completed` event.")

        self.assertTrue(is_retryable_api_error(exc))
        self.assertTrue(is_missing_response_completed_error(exc))

    def test_loaded_skill_uses_longer_model_timeout(self):
        runner = AgentRunner()

        self.assertEqual(runner._model_timeout_seconds(set()), DEFAULT_MODEL_TIMEOUT_SECONDS)
        self.assertEqual(
            runner._model_timeout_seconds({"brand-ranking"}),
            LONG_WORKFLOW_MODEL_TIMEOUT_SECONDS,
        )


class BrandRankingScriptTests(unittest.TestCase):
    def test_build_hotsell_plan_script_matches_expected_shape(self):
        payload = {
            "category": "空气炸锅",
            "rn": 20,
            "pn_sequence": [1, 2],
            "candidate_pool": [
                {
                    "brand_name": "九阳",
                    "candidate_tier": "stable",
                    "semantic_score": 3.2,
                    "mention_count": 4,
                    "source_rounds": [1, 2],
                },
                {
                    "brand_name": "苏泊尔",
                    "candidate_tier": "provisional",
                    "semantic_score": 1.1,
                    "mention_count": 2,
                    "source_rounds": [2],
                },
                {
                    "brand_name": "杂牌",
                    "candidate_tier": "noise",
                    "semantic_score": -2,
                    "mention_count": 5,
                },
            ],
        }
        script = BRAND_RANKING_DIR / "scripts" / "build_hotsell_plan.py"

        completed = subprocess.run(
            [sys.executable, str(script)],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            check=True,
            cwd=WORKDIR,
        )
        output = json.loads(completed.stdout)

        self.assertEqual(output["candidate_count"], 2)
        self.assertEqual(output["plan"][0]["brand_name"], "九阳")
        self.assertEqual(output["plan"][0]["pn_sequence"], [1, 2])

    def test_build_hotsell_plan_accepts_candidate_list_payload(self):
        payload = [
            {
                "brand_name": "华为",
                "candidate_tier": "stable",
                "semantic_score": 4.5,
                "mention_count": 6,
                "source_rounds": [1, 2, 3],
            },
            {
                "brand_name": "苹果/Apple",
                "candidate_tier": "provisional",
                "semantic_score": 4.0,
                "mention_count": 5,
                "source_rounds": [1, 2],
            },
        ]
        script = BRAND_RANKING_DIR / "scripts" / "build_hotsell_plan.py"

        completed = subprocess.run(
            [sys.executable, str(script)],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            check=True,
            cwd=WORKDIR,
        )
        output = json.loads(completed.stdout)

        self.assertEqual(output["candidate_count"], 2)
        self.assertEqual(output["plan"][0]["brand_name"], "华为")
        self.assertEqual(output["plan"][0]["query"], "华为")

    def test_merge_and_coverage_scripts_work_together(self):
        merge_script = BRAND_RANKING_DIR / "scripts" / "merge_hotsell_results.py"
        coverage_script = BRAND_RANKING_DIR / "scripts" / "check_hotsell_coverage.py"
        merge_payload = {
            "results": [
                {
                    "brand_name": "九阳",
                    "pages": [
                        {"items": [{"product_id": 1, "title": "A"}, {"product_id": 1, "title": "A"}]},
                        {"items": [{"product_id": 2, "title": "B"}]},
                    ],
                },
                {
                    "brand_name": "苏泊尔",
                    "pages": [{"items": [{"product_id": 3, "title": "C"}]}],
                },
            ]
        }
        merged = subprocess.run(
            [sys.executable, str(merge_script)],
            input=json.dumps(merge_payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            check=True,
            cwd=WORKDIR,
        )
        merged_output = json.loads(merged.stdout)
        self.assertEqual(merged_output["brand_count"], 2)
        self.assertEqual(merged_output["summary"][0]["unique_product_count"], 2)

        coverage_payload = {
            "plan": [
                {"brand_name": "九阳", "must_execute": True},
                {"brand_name": "苏泊尔", "must_execute": True},
            ],
            "merged_results": merged_output["merged_results"],
        }
        coverage = subprocess.run(
            [sys.executable, str(coverage_script)],
            input=json.dumps(coverage_payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            check=True,
            cwd=WORKDIR,
        )
        coverage_output = json.loads(coverage.stdout)
        self.assertTrue(coverage_output["is_complete"])
        self.assertEqual(coverage_output["missing_brands"], [])


if __name__ == "__main__":
    unittest.main()
