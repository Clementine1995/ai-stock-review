from __future__ import annotations

import ast
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "stock_review"
REVIEW_RUN_ROOT = PACKAGE_ROOT / "review_runs"
REVIEW_RUN_CLI = PACKAGE_ROOT / "review_run_cli.py"
LEGACY_CLI = PACKAGE_ROOT / "cli.py"
RENDERER = PACKAGE_ROOT / "reports" / "render_review_run.py"

DOMAIN_MODEL_FILES = (
    "canonical_hash.py",
    "domain_error.py",
    "run_manifest.py",
    "evidence_bundle.py",
    "review_run_event.py",
    "review_run.py",
    "review_revision.py",
    "action_set.py",
)
HTTP_CLIENT_MODULES = (
    "aiohttp",
    "anthropic",
    "http.client",
    "httpx",
    "openai",
    "requests",
    "urllib.request",
    "urllib3",
    "stock_review.llm",
)
DOMAIN_FORBIDDEN_MODULES = (
    "argparse",
    "click",
    "sqlite3",
    "typer",
    "stock_review.cli",
    "stock_review.review_run_cli",
    "stock_review.reports",
)
RENDERER_FORBIDDEN_MODULES = (
    "stock_review.cli",
    "stock_review.evidence",
    "stock_review.observations",
    "stock_review.planning",
    "stock_review.reports",
    "stock_review.review_documents",
    "stock_review.review_run_cli",
    "stock_review.storage",
)
RENDERER_FORBIDDEN_CALLS = {
    "assess_revision_quality",
    "build_plan_items",
    "build_review_context",
    "build_trade_plan",
    "summarize_review_facts",
    "validate_revision_bundle",
}
DAILY_COMMAND_FUNCTIONS = {
    "_register_daily_plan": "plan",
    "_register_daily_start": "start",
    "_register_daily_attach": "attach-evidence",
    "_register_daily_seal": "seal-evidence",
    "_register_daily_prepare_model": "prepare-model",
    "_register_daily_complete_model": "complete-model",
    "_register_daily_complete_local": "complete-local",
    "_register_daily_revise": "revise",
    "_register_daily_propose": "propose-actions",
    "_register_daily_finalize": "finalize",
    "_register_daily_show": "show",
    "_register_daily_verify": "verify",
}
DOCUMENTED_COMMANDS = {
    ("daily", command) for command in DAILY_COMMAND_FUNCTIONS.values()
} | {
    ("weekly", "create"),
    ("pool", "list"),
    ("observation", "list"),
    ("observation", "propose-result"),
    ("observation", "record-result"),
    ("database", "plan-migration"),
    ("database", "migrate"),
}


class ReviewRunArchitectureBoundaryTest(unittest.TestCase):
    def test_domain_models_do_not_depend_on_infrastructure_or_presentation(self):
        violations: list[str] = []
        for file_name in DOMAIN_MODEL_FILES:
            path = REVIEW_RUN_ROOT / file_name
            for module, line_number in _imported_modules(path):
                if _matches_any_module(module, DOMAIN_FORBIDDEN_MODULES) or _is_http_client(
                    module
                ):
                    violations.append(f"{_relative(path)}:{line_number} -> {module}")
        self.assertEqual(
            [],
            violations,
            "ReviewRun 领域模型出现基础设施或展示层反向依赖：\n"
            + "\n".join(violations),
        )

    def test_review_run_cli_uses_application_services_and_repository_boundary(self):
        imports = _imported_modules(REVIEW_RUN_CLI)
        violations = [
            f"{_relative(REVIEW_RUN_CLI)}:{line_number} -> {module}"
            for module, line_number in imports
            if module == "sqlite3"
            or _is_http_client(module)
            or _matches_any_module(
                module,
                (
                    "stock_review.evidence",
                    "stock_review.llm",
                    "stock_review.reports",
                    "stock_review.storage.sqlite_repository",
                ),
            )
        ]
        self.assertEqual(
            [],
            violations,
            "新版 CLI 绕过应用服务或 ReviewRunRepository：\n"
            + "\n".join(violations),
        )
        self.assertIn(
            "stock_review.storage.review_run_repository",
            {module for module, _line_number in imports},
            "新版 CLI 必须通过 ReviewRunRepository 访问持久化层。",
        )

    def test_renderer_has_no_business_or_storage_dependencies(self):
        import_violations = [
            f"{_relative(RENDERER)}:{line_number} -> {module}"
            for module, line_number in _imported_modules(RENDERER)
            if _matches_any_module(module, RENDERER_FORBIDDEN_MODULES)
            or module.rsplit(".", 1)[-1] in RENDERER_FORBIDDEN_CALLS
        ]
        call_violations: list[str] = []
        for node in ast.walk(_parse(RENDERER)):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            if name in RENDERER_FORBIDDEN_CALLS or name.startswith(
                ("assess_", "derive_", "infer_", "score_")
            ):
                call_violations.append(
                    f"{_relative(RENDERER)}:{node.lineno} -> {name}()"
                )
        violations = [*import_violations, *call_violations]
        self.assertEqual(
            [],
            violations,
            "Markdown renderer 承担了存储、事实推导或质量评估：\n"
            + "\n".join(violations),
        )

    def test_new_review_run_modules_use_absolute_internal_imports(self):
        violations: list[str] = []
        internal_names = _internal_module_names()
        for path in _new_review_run_files():
            tree = _parse(path)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.level:
                        violations.append(
                            f"{_relative(path)}:{node.lineno} -> 相对导入"
                        )
                        continue
                    module = node.module or ""
                    if _looks_like_unqualified_internal(module, internal_names):
                        violations.append(
                            f"{_relative(path)}:{node.lineno} -> {module}"
                        )
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if _looks_like_unqualified_internal(alias.name, internal_names):
                            violations.append(
                                f"{_relative(path)}:{node.lineno} -> {alias.name}"
                            )
        self.assertEqual(
            [],
            violations,
            "新版模块的 stock_review 内部导入必须从包根开始：\n"
            + "\n".join(violations),
        )

    def test_public_command_tree_contains_documented_review_run_commands(self):
        review_tree = _parse(REVIEW_RUN_CLI)
        functions = {
            node.name: node
            for node in review_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        register = functions["register_review_run_commands"]
        daily_is_wired = (
            _assigned_parser_binding(register, "daily") == ("subparsers", "daily")
            and _assigned_subparsers_receiver(register, "daily_commands") == "daily"
        )
        actual_commands = {
            ("daily", command)
            for function_name, command in DAILY_COMMAND_FUNCTIONS.items()
            if daily_is_wired
            and command in _add_parser_literals(functions[function_name])
            and _calls_function_with_arguments(
                register, function_name, {"daily_commands"}
            )
        }
        if (
            _assigned_parser_binding(register, "weekly") == ("subparsers", "weekly")
            and _assigned_subparsers_receiver(register, "weekly_commands") == "weekly"
            and _assigned_parser_binding(register, "weekly_create")
            == ("weekly_commands", "create")
        ):
            actual_commands.add(("weekly", "create"))
        for variable_name, command in (
            ("propose_result", "propose-result"),
            ("record_result", "record-result"),
        ):
            if _assigned_parser_binding(register, variable_name) == (
                "observation_subparsers",
                command,
            ):
                actual_commands.add(("observation", command))
        if (
            _assigned_parser_binding(register, "database")
            == ("subparsers", "database")
            and _assigned_subparsers_receiver(register, "database_commands")
            == "database"
        ):
            for variable_name, command in (
                ("plan_migration", "plan-migration"),
                ("migrate", "migrate"),
            ):
                if _assigned_parser_binding(register, variable_name) == (
                    "database_commands",
                    command,
                ):
                    actual_commands.add(("database", command))

        legacy_tree = _parse(LEGACY_CLI)
        build_parser = next(
            node
            for node in legacy_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "build_parser"
        )
        register_is_called = _calls_function_with_arguments(
            build_parser,
            "register_review_run_commands",
            {
                "subparsers",
                "observation_subparsers",
                "observation_list_parser",
                "pool_list_parser",
            },
        )
        if register_is_called and (
            _assigned_parser_binding(build_parser, "pool_parser")
            == ("subparsers", "pool")
            and _assigned_subparsers_receiver(build_parser, "pool_subparsers")
            == "pool_parser"
            and _assigned_parser_binding(build_parser, "pool_list_parser")
            == ("pool_subparsers", "list")
            and _handler_override(register, "pool_list_parser", "handle_review_run_pool_list")
        ):
            actual_commands.add(("pool", "list"))
        if register_is_called and (
            _assigned_parser_binding(build_parser, "observation_parser")
            == ("subparsers", "observation")
            and _assigned_subparsers_receiver(
                build_parser, "observation_subparsers"
            )
            == "observation_parser"
            and _assigned_parser_binding(build_parser, "observation_list_parser")
            == ("observation_subparsers", "list")
            and _handler_override(
                register,
                "observation_list_parser",
                "handle_review_run_observation_list",
            )
        ):
            actual_commands.add(("observation", "list"))

        self.assertEqual(
            set(),
            DOCUMENTED_COMMANDS - actual_commands,
            "新版公开命令树缺少文档规定命令。",
        )


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_modules(path: Path) -> list[tuple[str, int]]:
    modules: list[tuple[str, int]] = []
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            modules.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            module = prefix + (node.module or "")
            modules.append((module, node.lineno))
            modules.extend(
                (f"{module}.{alias.name}".lstrip("."), node.lineno)
                for alias in node.names
                if alias.name != "*"
            )
    return modules


def _matches_any_module(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == prefix or module.startswith(prefix + ".") for prefix in prefixes)


def _is_http_client(module: str) -> bool:
    return _matches_any_module(module, HTTP_CLIENT_MODULES) or module.rsplit(".", 1)[
        -1
    ].endswith("_client")


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _new_review_run_files() -> tuple[Path, ...]:
    explicit_files = (
        PACKAGE_ROOT / "review_run_cli.py",
        PACKAGE_ROOT / "evidence" / "build_evidence_bundle.py",
        PACKAGE_ROOT / "evidence" / "collect_manifest_capture.py",
        PACKAGE_ROOT / "learning" / "create_weekly_review_run.py",
        PACKAGE_ROOT / "observations" / "review_due_observations.py",
        PACKAGE_ROOT / "planning" / "build_plan_items.py",
        PACKAGE_ROOT / "reports" / "render_review_run.py",
        PACKAGE_ROOT / "storage" / "plan_review_run_migration.py",
        PACKAGE_ROOT / "storage" / "project_audit_log.py",
        PACKAGE_ROOT / "storage" / "review_run_repository.py",
    )
    return tuple(sorted(REVIEW_RUN_ROOT.glob("*.py"))) + tuple(
        path for path in explicit_files if path.exists()
    )


def _internal_module_names() -> set[str]:
    directory_names = {
        path.name for path in PACKAGE_ROOT.iterdir() if path.is_dir()
    }
    module_names = {path.stem for path in PACKAGE_ROOT.rglob("*.py")}
    return directory_names | module_names


def _looks_like_unqualified_internal(module: str, internal_names: set[str]) -> bool:
    root_name = module.split(".", 1)[0]
    return root_name != "stock_review" and root_name in internal_names


def _add_parser_literals(function: ast.AST) -> set[str]:
    values: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "add_parser" or not node.args:
            continue
        value = node.args[0]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            values.add(value.value)
    return values


def _assigned_parser_binding(
    function: ast.AST, variable_name: str
) -> tuple[str, str] | None:
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == variable_name
            for target in node.targets
        ):
            continue
        if not isinstance(node.value, ast.Call) or not isinstance(
            node.value.func, ast.Attribute
        ):
            continue
        if node.value.func.attr != "add_parser" or not node.value.args:
            continue
        receiver = node.value.func.value
        command = node.value.args[0]
        if (
            isinstance(receiver, ast.Name)
            and isinstance(command, ast.Constant)
            and isinstance(command.value, str)
        ):
            return receiver.id, command.value
    return None


def _assigned_subparsers_receiver(
    function: ast.AST, variable_name: str
) -> str | None:
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == variable_name
            for target in node.targets
        ):
            continue
        if not isinstance(node.value, ast.Call) or not isinstance(
            node.value.func, ast.Attribute
        ):
            continue
        receiver = node.value.func.value
        if node.value.func.attr == "add_subparsers" and isinstance(receiver, ast.Name):
            return receiver.id
    return None


def _calls_function_with_arguments(
    function: ast.AST,
    called_name: str,
    required_argument_names: set[str],
) -> bool:
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != called_name:
            continue
        argument_names = {
            argument.id for argument in node.args if isinstance(argument, ast.Name)
        }
        if required_argument_names.issubset(argument_names):
            return True
    return False


def _handler_override(
    function: ast.AST,
    parser_name: str,
    handler_name: str,
) -> bool:
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "set_defaults" or not isinstance(node.func.value, ast.Name):
            continue
        if node.func.value.id != parser_name:
            continue
        return any(
            keyword.arg == "handler"
            and isinstance(keyword.value, ast.Name)
            and keyword.value.id == handler_name
            for keyword in node.keywords
        )
    return False


def _relative(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


if __name__ == "__main__":
    unittest.main()
