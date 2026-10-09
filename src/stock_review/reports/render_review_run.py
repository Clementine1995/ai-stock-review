# 本文件只把上游已校验的 ReviewRun 投影渲染为单一 Markdown 日报，不补写业务判断。

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any


MISSING_INPUT = "待确认（上游未提供）"
EMPTY_INPUT = "无"
_MISSING = object()


# 正式日报入口兼容普通 mapping 和具有同名属性的 domain dataclass。
def render_review_run(
    review_run: Any,
    framework: Any | None = None,
    *,
    quality: Any | None = None,
    observations: Any = _MISSING,
    appendix: Any = _MISSING,
) -> str:
    revision = get(review_run, "review_revision", "current_revision", "revision", default=None)
    if revision is None and get(review_run, "review_revision_id") is not _MISSING:
        revision = review_run
    decision = section(review_run, revision, "decision", "decision_summary", "top_decision")
    quality = quality or section(review_run, revision, "quality", "quality_assessment")
    provenance = section(review_run, revision, "provenance", "traceability", "versions")
    trade_date = get_from((review_run, revision), "trade_date", "review_date")
    participation = get_from(
        (decision, revision, review_run),
        "participation",
        "participation_decision",
        "next_day_participation",
    )
    compact = participation == "non_participation"

    lines = [
        f"# {show(trade_date)} ReviewRun 盘后复盘",
        "",
        "- 渲染边界：仅展示上游已验证输入；缺失内容标记为待确认，不补充市场判断、交易对象或行动。",
        "",
    ]
    lines += render_decision(review_run, revision, decision)
    lines += render_quality(review_run, quality, compact=compact)
    lines += render_steps(review_run, revision, framework, compact=compact)
    lines += render_plans(review_run, revision, compact=compact)
    lines += render_observations(
        review_run,
        revision,
        observations,
        compact=compact,
    )
    lines += render_appendix(review_run, revision, appendix, compact=compact)
    # 哈希与版本只服务复核，统一放在业务正文之后，避免遮挡次日决策。
    lines += render_provenance(review_run, revision, provenance)
    return "\n".join(lines).rstrip() + "\n"


# 顶部决策只回显上游输入，不从计划项反向推导参与或仓位。
def render_decision(review_run: Any, revision: Any, decision: Any) -> list[str]:
    participation = get_from(
        (decision, revision, review_run),
        "participation",
        "participation_decision",
        "next_day_participation",
    )
    non_participation = participation == "non_participation"
    total_risk = get_from(
        (decision,),
        "position_guidance",
        "total_position",
        "total_risk",
        "risk_limit",
    )
    fields = (
        ("ReviewRun ID", (review_run, revision), ("run_id",), None),
        ("交易日期", (review_run, revision), ("trade_date", "review_date"), None),
        ("发布状态", (decision, revision, review_run), ("publication_status", "review_status", "revision_kind"), None),
        ("次日是否参与", (decision, revision, review_run), ("participation", "participation_decision", "next_day_participation"), None),
        ("次日总纲", (decision, revision, review_run), ("summary", "next_day_summary", "decision_note", "thesis"), None),
        (
            "重点对象",
            (decision,),
            ("focus_targets", "targets", "action_targets"),
            "无（空仓）" if non_participation else "未生成重点对象。",
        ),
        (
            "放弃与失效边界",
            (decision,),
            ("risk_boundary", "abandon_boundary", "invalidation_condition"),
            "不参与，仓位保持 0%。" if non_participation else "未生成放弃与失效边界。",
        ),
    )
    lines = [
        "## 顶部决策区",
        "",
        *[
            f"- {label}：{show_with_empty(get_from(containers, *names), empty_text)}"
            for label, containers, names, empty_text in fields[:4]
        ],
        f"- 总仓位与风险边界：{format_total_risk(total_risk, participation)}",
    ]
    lines += [
        f"- {label}：{show_with_empty(get_from(containers, *names), empty_text)}"
        for label, containers, names, empty_text in fields[4:]
    ]
    return [*lines, ""]


# 空阻断项只有在上游显式传入空集合时才渲染为无，字段缺失仍保持待确认。
def render_quality(review_run: Any, quality: Any, *, compact: bool = False) -> list[str]:
    level = get_from((quality, review_run), "publication_level", "quality_level", "level")
    publishable = get_from((quality,), "publishable")
    disposition = get_from((quality,), "disposition", "handling", "publication_disposition")
    gates = get_from((quality,), "gates", "gate_results")
    blockers = get_from(
        (quality, review_run),
        "blocking_items",
        "blockers",
        "blocking_reasons",
        "issues",
    )
    if blockers is _MISSING and gates is not _MISSING and gates is not None:
        blockers = tuple(
            issue
            for gate in collection(gates) or ()
            for issue in collection(get_from((gate,), "issues")) or ()
        )
    notes = get_from((quality,), "notes", "quality_notes", "warnings")
    if compact:
        gate_items = collection(gates, ("gate_id", "status"))
        gate_text = (
            MISSING_INPUT
            if gate_items is None
            else (
                "；".join(
                    f"{show(get_from((gate,), 'gate_id'))}={show(get_from((gate,), 'status'))}"
                    for gate in gate_items
                )
                if gate_items
                else "未生成"
            )
        )
        return [
            "## 质量处置及阻断项",
            "",
            f"- 发布等级：{show(level)}",
            f"- 是否允许发布：{show(publishable)}",
            f"- 处置：{show(disposition)}",
            f"- G1-G6 门禁：{gate_text}",
            *render_compact_items(blockers, "阻断项", empty_text="无"),
            *render_compact_items(notes, "其它质量提示", empty_text="无"),
            "",
        ]
    lines = [
        "## 质量处置及阻断项",
        "",
        f"- 发布等级：{show(level)}",
        f"- 是否允许发布：{show(publishable)}",
        f"- 处置：{show(disposition)}",
        "",
        "### G1-G6 门禁",
        "",
        *render_gates(gates),
        "",
        "### 阻断项",
        "",
        *render_blockers(blockers),
        "",
        "### 其它质量提示",
        "",
        *render_items(notes),
        "",
    ]
    return lines


def render_gates(value: Any) -> list[str]:
    gates = collection(value, ("gate_id", "status"))
    if gates is None:
        return [f"- {MISSING_INPUT}：G1-G6 门禁。"]
    if not gates:
        return ["- 未生成 G1-G6 门禁结果。"]
    return [
        f"- {show(get_from((gate,), 'gate_id'))}：{show(get_from((gate,), 'status'))}"
        for gate in gates
    ]


# 版本字段优先读取扁平投影，缺失时再读取嵌套 domain 对象的原字段。
def render_provenance(review_run: Any, revision: Any, provenance: Any) -> list[str]:
    manifest = get_from((review_run, provenance), "manifest", "run_manifest")
    evidence = get_from((review_run, provenance), "evidence_bundle")
    framework = get_from((review_run, revision, provenance), "framework")
    flat = (provenance, revision, review_run)
    supersedes_publication_id = trace(flat, None, ("supersedes_publication_id",))
    if supersedes_publication_id is None:
        # 首次 publication 显式保存 null，业务含义是没有被取代的前序版本。
        supersedes_publication_id = EMPTY_INPUT
    model_response_hash = trace(
        flat,
        revision,
        ("model_response_hashes", "model_response_hash", "response_hash"),
    )
    model_id = trace(flat, None, ("model_id", "model_identifier"))
    if is_missing_value(model_id) and is_explicit_empty(model_response_hash):
        model_id = "未使用正式模型通道"
    rows = (
        ("Publication ID", trace(flat, None, ("publication_id",))),
        (
            "Publication 内容哈希",
            trace(
                flat,
                None,
                (
                    "publication_hash",
                    "publication_content_hash",
                    "publication_hash_locator",
                ),
            ),
        ),
        (
            "正式报告 SHA-256",
            trace(flat, None, ("report_hash", "report_hash_locator")),
        ),
        (
            "取代的 Publication ID",
            supersedes_publication_id,
        ),
        ("ActionSet ID", trace(flat, None, ("action_set_id",))),
        (
            "ActionSet 内容哈希",
            trace(
                flat,
                None,
                ("action_set_hash", "action_set_content_hash", "action_set_hash_locator"),
            ),
        ),
        ("RunManifest ID", trace(flat, manifest, ("manifest_id",))),
        ("RunManifest 哈希", trace(flat, manifest, ("manifest_hash", "run_manifest_hash"), ("content_hash",))),
        ("EvidenceBundle ID", trace(flat, evidence, ("evidence_bundle_id",))),
        ("EvidenceBundle 哈希", trace(flat, evidence, ("evidence_bundle_hash", "evidence_hash"), ("content_hash",))),
        ("证据截点", trace(flat, evidence, ("evidence_cutoff", "evidence_cutoff_at"), ("sealed_at",))),
        ("ReviewRevision ID", trace(flat, revision, ("review_revision_id", "revision_id"))),
        ("ReviewRevision 哈希", trace(flat, revision, ("review_revision_hash", "revision_hash"), ("content_hash",))),
        ("修订类型", trace(flat, revision, ("revision_kind",))),
        ("修订创建时间", trace(flat, revision, ("created_at",))),
        ("替代的修订 ID", trace(flat, revision, ("replaces_review_revision_id",))),
        ("框架版本", trace(flat, framework, ("framework_version",), ("version",))),
        ("框架哈希", trace(flat, framework, ("framework_hash",), ("content_hash",))),
        ("程序版本", trace(flat, None, ("program_version",))),
        ("渲染器版本", trace(flat, None, ("renderer_version",))),
        ("模型标识", model_id),
        ("模型响应哈希", model_response_hash),
    )
    return ["## 证据与版本追溯", "", *[f"- {label}：{show(value)}" for label, value in rows], ""]


# STEP 标题取框架元数据，STEP 内容只取对应的上游 assessment。
def render_steps(
    review_run: Any,
    revision: Any,
    framework: Any | None,
    *,
    compact: bool = False,
) -> list[str]:
    framework = framework or get_from((review_run, revision), "framework")
    framework_entries = step_entries(get(framework, "steps"))
    assessment_entries = step_entries(get_from((revision, review_run), "step_assessments", "steps"))
    framework_by_number = index_steps(framework_entries)
    assessment_by_number = index_steps(assessment_entries)
    order = framework_order(framework_entries)
    lines: list[str] = []

    for number in order:
        framework_step = framework_by_number.get(number)
        assessment = assessment_by_number.get(number)
        if assessment is None and framework_step is not None and not assessment_entries:
            assessment = framework_step
        title = get_from((framework_step, assessment), "title", "name")
        status = get_from((assessment,), "status", "assessment_status")
        conclusion = get_from((assessment,), "conclusion", "judgment", "summary")
        evidence = get_from((assessment,), "key_evidence", "evidence", "facts", "evidence_items")
        evidence_prefix = None
        if evidence is _MISSING:
            # RevisionBundle 只保存可核验引用时，关键证据区明确回显引用，不伪造事实摘要。
            evidence = get_from(
                (assessment,),
                "evidence_refs",
                "evidence_references",
                "references",
            )
            if evidence is not _MISSING:
                evidence_prefix = "关键证据引用"
        counter = get_from((assessment,), "counter_evidence", "contrary_evidence", "reverse_evidence")
        open_questions = get_from((assessment,), "open_questions", "questions")
        integrity_issues = get_from((assessment,), "integrity_issues")
        gaps = get_from((assessment,), "evidence_gaps", "hard_gaps", "blocking_gaps", "gaps")
        validation = get_from(
            (assessment,),
            "next_day_checks",
            "next_day_validation",
            "next_validation",
            "validation",
        )
        references = get_from((assessment,), "evidence_refs", "evidence_references", "references")
        patterns = get_from((assessment,), "pattern_assessments")
        if compact:
            lines += render_compact_step(
                number,
                title,
                status,
                conclusion,
                evidence if evidence_prefix is None else _MISSING,
                counter,
                open_questions,
                integrity_issues,
                gaps,
                validation,
                patterns,
            )
            continue
        lines += [
            f"## STEP {number}: {show(title)}",
            "",
            "### 本步结论",
            "",
            f"- 状态：{show(status)}",
            f"- 结论：{show(conclusion)}",
            "",
            "### 关键证据",
            "",
            *render_items(evidence, evidence_prefix),
            "",
        ]
        if number == 9:
            lines += render_step_nine_patterns(patterns)
        lines += [
            "### 反向证据与硬缺口",
            "",
            *render_items(counter, "反向证据", empty_text="无反向证据"),
            *render_items(open_questions, "待确认问题"),
            *render_items(integrity_issues, "完整性问题"),
            *render_items(gaps, "证据缺口"),
            "",
            "### 次日验证点",
            "",
            *render_items(validation),
            "",
            "### 证据引用",
            "",
            *render_references(references),
            "",
        ]
    return lines


# 空仓报告只保留每步结论和真正存在的限制，不重复渲染空分区与技术引用。
def render_compact_step(
    number: int,
    title: Any,
    status: Any,
    conclusion: Any,
    evidence: Any,
    counter: Any,
    open_questions: Any,
    integrity_issues: Any,
    gaps: Any,
    validation: Any,
    patterns: Any,
) -> list[str]:
    limitations = compact_values(counter, open_questions, integrity_issues, gaps)
    lines = [
        f"## STEP {number}: {show(title)}",
        "",
        f"- 状态：{show(status)}；结论：{show(conclusion)}",
        *render_compact_items(evidence, "关键依据", omit_empty=True),
        *(
            [f"- 十二模式状态：{compact_pattern_statuses(patterns)}"]
            if number == 9 and collection(patterns, ("pattern_number", "pattern_name"))
            else []
        ),
        *(
            [f"- 限制与缺口：{'；'.join(limitations)}"]
            if limitations
            else []
        ),
        *render_compact_items(validation, "次日验证", omit_empty=True),
        "",
    ]
    return lines


def compact_pattern_statuses(value: Any) -> str:
    counts: dict[str, int] = {}
    for pattern in collection(value, ("pattern_number", "pattern_name")) or ():
        status = show(get_from((pattern,), "status"))
        counts[status] = counts.get(status, 0) + 1
    return "；".join(f"{status} × {counts[status]}" for status in sorted(counts))


# STEP 9 逐项回显上游结构化状态、对象、缺口和引用，不从结论文本推导模式结果。
def render_step_nine_patterns(value: Any) -> list[str]:
    patterns = collection(value, ("pattern_number", "pattern_name"))
    lines = ["### 十二模式逐项核验", ""]
    if patterns is None:
        return [*lines, f"- {MISSING_INPUT}：STEP 9 模式逐项核验。", ""]
    if not patterns:
        return [*lines, "- 未生成 STEP 9 模式逐项核验。", ""]
    for pattern in sorted(
        patterns,
        key=lambda item: sort_key(get_from((item,), "pattern_number")),
    ):
        number = get_from((pattern,), "pattern_number")
        name = get_from((pattern,), "pattern_name")
        lines += [
            f"#### 模式 {show(number)}：{show(name)}",
            "",
            f"- 状态：{show(get_from((pattern,), 'status'))}",
            f"- 对象 ID：{show(get_from((pattern,), 'object_ids')) if get_from((pattern,), 'object_ids') else '未绑定对象'}",
            "- 证据缺口："
            + show(get_from((pattern,), "missing_evidence")),
            "- 模式证据引用：",
            *render_references(
                get_from((pattern,), "evidence_references", "evidence_refs")
            ),
            "",
        ]
        if get_from((pattern,), "status") in {"base_evidence_available_trigger_pending", "assessment_pending"}:
            lines += ["- 核验边界：能力材料不等于背景匹配；具体背景与对象尚待联合修订核验。", ""]
    return lines


# 四态条件与动作分别读取，避免把计划级 action 错当成每个情景的动作。
def render_plans(review_run: Any, revision: Any, *, compact: bool = False) -> list[str]:
    value = get_from(
        (revision, review_run),
        "plan_items",
        "plan_item_revisions",
        "plans",
        "trade_plan",
    )
    if value is _MISSING:
        revision_kind = get_from((revision, review_run), "revision_kind")
        value = get_from(
            (revision, review_run),
            "approved_plan_items" if revision_kind == "final" else "candidate_plan_items",
        )
    plans = collection(value, ("plan_item_revision_id", "plan_item_id", "code", "target"))
    lines = ["## 正式交易计划", ""]
    if plans is None:
        return [*lines, f"- {MISSING_INPUT}：正式交易计划。", ""]
    if not plans:
        text = "空仓，不生成交易计划。" if compact else "未生成正式交易计划。"
        return [*lines, f"- {text}", ""]

    for position, plan in enumerate(plans, start=1):
        plan_id = get_from((plan,), "plan_item_revision_id", "plan_item_id", "id")
        heading = show(plan_id) if plan_id is not _MISSING else f"计划项 {position}"
        lines += [
            f"### {heading}",
            "",
            f"- 对象：{show(plan_target(plan))}",
            f"- 角色：{show(get_from((plan,), 'role', 'target_role'))}",
            f"- 角色来源：{show(get_from((plan,), 'role_source'))}",
            f"- 角色已确认：{show(get_from((plan,), 'role_confirmed'))}",
            f"- 状态：{show(get_from((plan,), 'status', 'approval_status'))}",
            f"- 行动类型：{show(get_from((plan,), 'action', 'action_type'))}",
            f"- 可验证假设：{show(get_from((plan,), 'hypothesis'))}",
            f"- 成立条件：{show(get_from((plan,), 'confirmation_condition'))}",
            f"- 失效条件：{show(get_from((plan,), 'invalidation_condition'))}",
            f"- 最大仓位或风险：{show(plan_risk(plan))}",
            f"- 可观察触发时点：{show(get_from((plan,), 'trigger_time', 'observable_at'))}",
            f"- 最晚回填日期：{show(get_from((plan,), 'review_deadline', 'deadline'))}",
            "",
            "#### 角色证据引用",
            "",
            *render_references(get_from((plan,), "role_evidence_references", "role_evidence_refs")),
            "",
        ]
        scenarios = index_scenarios(get(plan, "scenario_bands", "scenarios", "conditions"))
        for title, keys, condition_keys, action_keys in scenario_specs():
            scenario = get_from((scenarios,), *keys)
            if scenario is not _MISSING and not is_record(scenario):
                condition = scenario
                action = _MISSING
            else:
                condition = get_from((scenario,), "condition_text", "condition")
                action = get_from((scenario,), "action")
            if condition is _MISSING:
                condition = get_from((plan,), *condition_keys)
            if action is _MISSING:
                action = get_from((plan,), *action_keys)
            lines += [
                f"#### {title}",
                "",
                f"- 条件：{show(condition)}",
                f"- 动作：{show(action)}",
                f"- 来源与主指标：{show(scenario_metric(scenario))}",
                f"- 可观察时点：{show(get_from((scenario,), 'observable_at', 'trigger_time'))}",
                "",
            ]
        references = get_from((plan,), "evidence_refs", "evidence_references", "references")
        lines += [
            "#### 计划证据引用",
            "",
            *render_references(references),
            "",
            f"- 关联 Observation：{show(get_from((plan,), 'observation_ids', 'observation_id'))}",
            "",
        ]
    return lines


# Observation 只展示显式映射，不从计划自动创建或补齐字段。
def render_observations(
    review_run: Any,
    revision: Any,
    supplied: Any,
    *,
    compact: bool = False,
) -> list[str]:
    value = supplied
    if value is _MISSING:
        value = get_from((revision, review_run), "observations", "observation_mappings")
    observations = collection(value, ("observation_id", "plan_item_revision_id", "hypothesis"))
    lines = ["## Observation 映射", ""]
    if observations is None:
        return [*lines, f"- {MISSING_INPUT}：Observation 映射。", ""]
    if not observations:
        text = "空仓，不生成 Observation。" if compact else "未生成 Observation 映射。"
        return [*lines, f"- {text}", ""]

    fields = (
        ("PlanItemRevision ID", ("plan_item_revision_id", "plan_item_id")),
        ("状态", ("status",)),
        ("假设", ("hypothesis",)),
        ("成立条件", ("confirmation_condition",)),
        ("失效条件", ("invalidation_condition",)),
        ("最晚回填日期", ("review_deadline", "deadline")),
        ("实际结果", ("actual_result",)),
        ("回填原因", ("reason", "review_reason")),
    )
    for position, observation in enumerate(observations, start=1):
        observation_id = get_from((observation,), "observation_id", "id")
        heading = show(observation_id) if observation_id is not _MISSING else f"Observation {position}"
        lines += [f"### {heading}", ""]
        lines += [f"- {label}：{show(get_from((observation,), *names))}" for label, names in fields]
        lines += [
            "",
            "#### Observation 证据引用",
            "",
            *render_references(
                get_from((observation,), "evidence_refs", "evidence_references", "references")
            ),
            "",
        ]
    return lines


# 附录 mapping 按标题排序，显式 section 列表则保留上游顺序。
def render_appendix(
    review_run: Any,
    revision: Any,
    supplied: Any,
    *,
    compact: bool = False,
) -> list[str]:
    appendix = supplied
    if appendix is _MISSING:
        appendix = get_from((revision, review_run), "appendix", "appendices")
    lines = ["## 附录", ""]
    if appendix is _MISSING or appendix is None:
        return [*lines, f"- {MISSING_INPUT}：附录。", ""]
    if isinstance(appendix, Mapping):
        if not appendix:
            return [*lines, "- 未生成附录。", ""]
        for title in sorted(appendix, key=str):
            if compact and not compact_values(appendix[title]):
                continue
            items = (
                render_compact_items(appendix[title], show(title), omit_empty=True)
                if compact
                else [f"### {show(title)}", "", *render_items(appendix[title]), ""]
            )
            lines += items
        if len(lines) == 2:
            return [*lines, "- 无需附加业务内容。", ""]
        if compact:
            lines.append("")
        return lines

    sections = collection(appendix, ("title", "name", "items")) or []
    if not sections:
        return [*lines, "- 未生成附录。", ""]
    for position, item in enumerate(sections, start=1):
        if is_record(item):
            title = get_from((item,), "title", "name")
            content = get_from((item,), "items", "content", "values")
        else:
            title = f"附录 {position}"
            content = item
        lines += [f"### {show(title)}", "", *render_items(content), ""]
    return lines


def render_blockers(value: Any) -> list[str]:
    blockers = collection(value, ("code", "message", "description"))
    if blockers is None:
        return [f"- {MISSING_INPUT}：阻断项。"]
    if not blockers:
        return ["- 无阻断项。"]
    lines: list[str] = []
    for blocker in blockers:
        if not is_record(blocker):
            lines.append(f"- {show(blocker)}")
            continue
        severity = get_from((blocker,), "severity", "level")
        code = get_from((blocker,), "code", "blocking_code")
        message = get_from((blocker,), "message", "description", "reason")
        tags = [show(item) for item in (severity, code) if item is not _MISSING and item not in (None, "")]
        tag_text = f"[{' / '.join(tags)}] " if tags else ""
        lines.append(f"- {tag_text}{show(message)}")
        references = collection(get_from((blocker,), "evidence_refs", "evidence_references", "references"))
        if references:
            lines += [f"  - 证据引用：{show(item)}" for item in references]
    return lines


def render_items(value: Any, prefix: str | None = None, *, empty_text: str = EMPTY_INPUT) -> list[str]:
    if value is _MISSING or value is None:
        text = MISSING_INPUT
        return [f"- {prefix}：{text}" if prefix else f"- {text}"]
    if isinstance(value, Mapping):
        if not value:
            text = empty_text
            return [f"- {prefix}：{text}" if prefix else f"- {text}"]
        items = [f"{show(key)}：{show(value[key])}" for key in sorted(value, key=str)]
    else:
        items = collection(value) or []
        items = [show_item(item) for item in items]
    if not items:
        items = [empty_text]
    return [f"- {prefix}：{item}" if prefix else f"- {item}" for item in items]


# 紧凑报告把同类项目合并到一行；缺失字段不再制造重复的“待确认”段落。
def render_compact_items(
    value: Any,
    prefix: str,
    *,
    empty_text: str = EMPTY_INPUT,
    omit_empty: bool = False,
) -> list[str]:
    items = compact_values(value)
    if not items:
        return [] if omit_empty else [f"- {prefix}：{empty_text}"]
    return [f"- {prefix}：{'；'.join(items)}"]


def compact_values(*values: Any) -> list[str]:
    items: list[str] = []
    for value in values:
        if value is _MISSING or value is None:
            continue
        if isinstance(value, Mapping):
            items.extend(
                f"{show(key)}：{show(value[key])}"
                for key in sorted(value, key=str)
            )
            continue
        items.extend(show_item(item) for item in (collection(value) or ()))
    return [item for item in items if item not in ("", EMPTY_INPUT, MISSING_INPUT)]


def render_references(value: Any) -> list[str]:
    references = collection(value)
    if references is None:
        return [f"- 证据引用：{MISSING_INPUT}"]
    if not references:
        return [f"- 证据引用：{EMPTY_INPUT}"]
    return [f"- 证据引用：{show_item(item)}" for item in references]


def show_item(value: Any) -> str:
    if not is_record(value):
        return show(value)
    label = get_from((value,), "label", "title", "name", "code")
    content = get_from((value,), "value", "text", "message", "description")
    if label is not _MISSING and content is not _MISSING:
        return f"{show(label)}：{show(content)}"
    return show(content if content is not _MISSING else value)


def scenario_specs() -> tuple[tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...]:
    return (
        ("符合预期", ("expectation", "expected", "as_expected", "meets_expectations"), ("expected_condition", "as_expected_condition"), ("expected_action", "as_expected_action")),
        ("超预期", ("over_expectation", "above_expected", "super_expected", "exceeds_expectations"), ("above_expected_condition", "super_expected_condition"), ("above_expected_action", "super_expected_action")),
        ("不及预期", ("under_expectation", "below_expected", "under_expected", "misses_expectations"), ("below_expected_condition", "under_expected_condition"), ("below_expected_action", "under_expected_action")),
        ("放弃条件", ("abandon", "give_up", "cancel"), ("abandon_condition", "invalidation_condition"), ("abandon_action", "give_up_action", "cancel_action")),
    )


def plan_target(plan: Any) -> Any:
    target = get_from((plan,), "target", "object", "target_name")
    if target is not _MISSING:
        return target
    values = [
        show(value)
        for value in (
            get_from((plan,), "object_code", "code", "target_code"),
            get_from((plan,), "object_name", "name", "stock_name", "object_id"),
        )
        if value is not _MISSING and value not in (None, "")
    ]
    return " ".join(values) if values else _MISSING


def plan_risk(plan: Any) -> Any:
    value = get_from((plan,), "position_limit", "max_risk", "position_guidance")
    unit = get_from((plan,), "risk_unit")
    if value is _MISSING:
        return _MISSING
    return f"{show(value)} {show(unit)}" if unit is not _MISSING and unit not in (None, "") else value


def index_scenarios(value: Any) -> Any:
    if value is _MISSING or value is None:
        return _MISSING
    if isinstance(value, Mapping):
        if get(value, "scenario") is _MISSING:
            return value
        return {show(get(value, "scenario")): value}
    indexed: dict[str, Any] = {}
    for band in collection(value) or ():
        scenario = get_from((band,), "scenario", "name")
        if scenario is not _MISSING:
            indexed[show(scenario)] = band
    return indexed


def scenario_metric(scenario: Any) -> Any:
    source = get_from((scenario,), "source")
    metric = get_from((scenario,), "metric")
    unit = get_from((scenario,), "unit")
    values = [show(item) for item in (source, metric, unit) if item is not _MISSING and item not in (None, "")]
    return " / ".join(values) if values else _MISSING


def framework_order(entries: list[tuple[Any, Any]]) -> list[int]:
    ordered: list[int] = []
    for fallback, step in entries:
        number = step_number(step, fallback)
        if number is not None and 1 <= number <= 10 and number not in ordered:
            ordered.append(number)
    return [*ordered, *(number for number in range(1, 11) if number not in ordered)]


def index_steps(entries: list[tuple[Any, Any]]) -> dict[int, Any]:
    indexed: dict[int, Any] = {}
    for fallback, step in entries:
        number = step_number(step, fallback)
        if number is not None and 1 <= number <= 10 and number not in indexed:
            indexed[number] = step
    return indexed


def step_entries(value: Any) -> list[tuple[Any, Any]]:
    if value is _MISSING or value is None:
        return []
    if isinstance(value, Mapping):
        return [(key, value[key]) for key in sorted(value, key=sort_key)]
    if is_sequence(value):
        return list(enumerate(value, start=1))
    return [(1, value)]


def step_number(step: Any, fallback: Any) -> int | None:
    try:
        return int(get(step, "number", "step_number", default=fallback))
    except (TypeError, ValueError):
        return None


def trace(
    flat: Sequence[Any],
    nested: Any,
    flat_names: tuple[str, ...],
    nested_names: tuple[str, ...] | None = None,
) -> Any:
    value = get_from(flat, *flat_names)
    return get_from((nested,), *(nested_names or flat_names)) if value is _MISSING else value


def section(review_run: Any, revision: Any, *names: str) -> Any:
    value = get_from((revision, review_run), *names)
    return None if value is _MISSING else value


def get_from(containers: Sequence[Any], *names: str) -> Any:
    for container in containers:
        value = get(container, *names)
        if value is not _MISSING:
            return value
    return _MISSING


def get(container: Any, *names: str, default: Any = _MISSING) -> Any:
    if container is _MISSING or container is None:
        return default
    for name in names:
        if isinstance(container, Mapping) and name in container:
            return container[name]
        if hasattr(container, name):
            return getattr(container, name)
    return default


def collection(value: Any, identity_fields: tuple[str, ...] = ()) -> list[Any] | None:
    if value is _MISSING or value is None:
        return None
    if isinstance(value, Mapping):
        if identity_fields and any(field in value for field in identity_fields):
            return [value]
        return [value[key] for key in sorted(value, key=str)]
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if is_sequence(value):
        return list(value)
    return [value]


def is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def is_record(value: Any) -> bool:
    return isinstance(value, Mapping) or (
        value is not None and value is not _MISSING and hasattr(value, "__dataclass_fields__")
    )


def sort_key(value: Any) -> tuple[int, Any]:
    try:
        return (0, int(value))
    except (TypeError, ValueError):
        return (1, str(value))


def show(value: Any) -> str:
    if value is _MISSING or value is None or value == "":
        return MISSING_INPUT
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, Mapping):
        if not value:
            return EMPTY_INPUT
        return "；".join(f"{show(key)}={show(value[key])}" for key in sorted(value, key=str))
    if isinstance(value, (set, frozenset)):
        value = sorted(value, key=str)
    if is_sequence(value):
        return "、".join(show(item) for item in value) if value else EMPTY_INPUT
    return str(value).replace("\r\n", "<br>").replace("\n", "<br>").strip()


def format_total_risk(value: Any, participation: Any) -> str:
    if is_numeric_zero(value):
        return "0%（空仓，不参与）" if participation == "non_participation" else "0%"
    return show(value)


def is_numeric_zero(value: Any) -> bool:
    if isinstance(value, bool) or value is _MISSING or value is None:
        return False
    try:
        return Decimal(str(value).strip()) == 0
    except (InvalidOperation, ValueError):
        return False


def is_missing_value(value: Any) -> bool:
    return value is _MISSING or value is None or value == ""


def is_explicit_empty(value: Any) -> bool:
    return (
        value is not _MISSING
        and value is not None
        and (
            (isinstance(value, Mapping) and not value)
            or (isinstance(value, (set, frozenset)) and not value)
            or (is_sequence(value) and not value)
        )
    )


# 只有显式空集合使用字段自己的空态文案；缺失、null 和空字符串仍按待确认处理。
def show_with_empty(value: Any, empty_text: str | None) -> str:
    if empty_text is not None and (
        (isinstance(value, Mapping) and not value)
        or (isinstance(value, (set, frozenset)) and not value)
        or (is_sequence(value) and not value)
    ):
        return empty_text
    return show(value)
