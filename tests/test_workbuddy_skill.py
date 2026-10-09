# 本文件验证 WorkBuddy 文档只描述新版 ReviewRun 正式合同和真实验收边界。

from pathlib import Path
import re
import unittest


class WorkBuddySkillTest(unittest.TestCase):
    def test_standard_capture_profile_and_business_revision_are_required(self):
        for text in ("推荐 capture profile", "STEP_EVIDENCE_REQUIREMENTS", "全部通用模板",
                     "两条修订路径同时跳过", "背景与对象仍未核验", "完整人工联合修订",
                     "--capture-profile-base64-stdin", "akshare:market", "hhxg:snapshot",
                     "tongdaxin:index-history", "workbuddy:sector-history", "workbuddy:stocks",
                     '"allowed_objects"', '"max_records"'):
            self.assertIn(text, self.content)
        self.assertNotIn("### 5.7 可选人工联合修订", self.content)
        self.assertNotIn("### 5.6 可选模型修订", self.content)

    @classmethod
    def setUpClass(cls):
        cls.skill_path = (
            Path(__file__).resolve().parents[1]
            / "integrations"
            / "workbuddy"
            / "stock-review"
            / "SKILL.md"
        )
        cls.content = cls.skill_path.read_text(encoding="utf-8")

    def test_skill_metadata_declares_v2_contract_and_pending_acceptance(self):
        self.assertTrue(self.content.startswith("---\n"))
        frontmatter = self.content.split("---", maxsplit=2)[1]
        name_match = re.search(r"^name:\s*(.+)$", frontmatter, re.MULTILINE)
        description_match = re.search(
            r"^description:\s*(.+)$", frontmatter, re.MULTILINE
        )

        self.assertIsNotNone(name_match)
        self.assertIsNotNone(description_match)
        name = name_match.group(1).strip()
        description = description_match.group(1).strip()
        self.assertEqual("stock-review", name)
        self.assertRegex(name, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
        self.assertLessEqual(len(name), 64)
        self.assertLessEqual(len(description), 1024)
        self.assertIn('version: "2.2.0"', frontmatter)
        self.assertIn("real contract acceptance is pending", frontmatter)

    def test_skill_uses_isolated_new_database_and_states_trial_status_honestly(self):
        for statement in (
            "新版默认使用独立的 `data/review_run.sqlite`",
            "旧 `data/stock_review.sqlite` 保持历史原貌",
            "当前也没有跨库合并能力",
            "`daily start` 会初始化完整 schema",
            "不要先运行迁移",
            "本 Skill 尚未完成真实 WorkBuddy 合同验收",
            "没有连续五个交易日的新版试跑证据",
            "真实合同验收通过前不删除",
            "不得把本地单元测试、假证据、命令帮助、旧报告或本文描述说成已经联网、已经迁移或已经真实验收",
        ):
            self.assertIn(statement, self.content)

        self.assertIn("用户只需说“复盘 YYYY-MM-DD”", self.content)
        self.assertIn("不要让用户手工编码 Base64", self.content)
        self.assertIn("不得自行更换用户已经指定的模型或推理强度", self.content)
        self.assertIn("`.workbuddy/memory/` 是旧链过程记录", self.content)

    def test_skill_defines_one_official_object_chain(self):
        chain = (
            "RunManifest → ReviewRun → EvidenceBundle → RevisionBundle → "
            "FinalizationProposal/ActionSet → 唯一当前 publication"
        )
        self.assertIn(chain, self.content)
        self.assertIn(
            "没有 `daily finalize` 成功结果的 Markdown 不是新版正式 publication",
            self.content,
        )
        self.assertIn(
            "旧 CLI 和 `scripts/run_daily_review.ps1` 只作为阶段 4 前的过渡入口",
            self.content,
        )
        self.assertNotRegex(
            self.content,
            r"(?m)^review (?:create-brief|prepare-detailed|create-detailed)\b",
        )

    def test_skill_requires_full_object_hash_preview_before_each_commit(self):
        for item in (
            "RunManifest 与 `manifest_hash`",
            "FinalizationProposal、ActionSet、报告预览与 `proposal_hash`",
            "Observation 结果 action 与 `action_hash`",
            "MigrationPlan 与 `plan_hash`",
            "正文、目标库、当前版本或文件状态变化后，旧确认失效",
        ):
            self.assertIn(item, self.content)
        self.assertIn("完整 UTF-8 JSON 的 Base64", self.content)
        self.assertIn("参数值必须是字面量 `null`", self.content)
        self.assertIn("用仓库内临时文件绕过确认", self.content)

    def test_skill_lists_the_complete_daily_command_surface_in_order(self):
        daily_section = self.content.split("## 五、每日正式流程", maxsplit=1)[1].split(
            "## 六、Observation 结果", maxsplit=1
        )[0]
        command_lines = re.findall(r"(?m)^daily ([a-z-]+)\b", daily_section)
        self.assertEqual(
            [
                "plan",
                "start",
                "attach-evidence",
                "seal-evidence",
                "complete-local",
                "prepare-model",
                "complete-model",
                "revise",
                "propose-actions",
                "finalize",
                "show",
                "verify",
            ],
            command_lines,
        )
        self.assertIn(
            "全部槽位处于 `available`、`unavailable` 或 `skipped` 终态",
            self.content,
        )
        self.assertIn("封存后不再 attach；新证据必须新建 ReviewRun", self.content)

    def test_skill_keeps_network_and_model_scope_inside_confirmed_manifest(self):
        self.assertIn("命令名称不自动授予联网、模型或业务写权限", self.content)
        self.assertIn(
            "当前 manifest 明确包含该来源/模型且用户授权覆盖本次调用",
            self.content,
        )
        self.assertIn("不得因零命中、歧义或缺字段自动扩大为全市场搜索", self.content)
        self.assertIn("把 request 的 `frozen_input` 作为唯一事实输入", self.content)
        self.assertIn("`frozen_input.normalized_evidence_by_step`", self.content)
        self.assertIn("不包含完整合并快照", self.content)
        self.assertIn("生成某一步时只能读取同编号数组", self.content)
        self.assertIn("真实模型响应必须只有顶层 `steps`", self.content)
        self.assertIn('"conclusion": "..."', self.content)
        self.assertIn('"counter_evidence": ["..."]', self.content)
        self.assertIn('"open_questions": ["..."]', self.content)
        self.assertIn("模型不得新增事实、数字、阈值、证据引用", self.content)
        self.assertIn("CLI 拒绝迟到响应", self.content)
        self.assertIn("不得改 response 来绕过边界或自动变基", self.content)

    def test_ordinary_review_cannot_fall_back_to_connectivity_diagnostic(self):
        for statement in (
            "这句话表示要生成一份证据充分、可供次日使用的正式复盘，不表示测试连接",
            "只有用户明确说“测试连通性”或“测试数据源”时",
            "仅含 `akshare:market` 与 `hhxg:snapshot` 的连通性 profile",
            "禁止执行 `daily propose-actions` 和 `daily finalize`",
            "普通“复盘”不得自动降级为这个 profile",
            "先让用户给出一份小型板块/股票清单",
        ):
            self.assertIn(statement, self.content)

    def test_formal_model_is_distinct_and_traceable(self):
        for statement in (
            "普通复盘在 plan 前必须取得精确的正式分析 model ID",
            "普通正式复盘必须传精确 `--model MODEL_ID --model-required`",
            "WorkBuddy 编排模型与正式分析模型是两个不同对象",
            "requested/actual model ID",
            "正式模型调用必须取得平台返回的 actual model ID",
            "actual ID 未返回或与已确认 ID 不一致时停止",
            "必须通过本节的 `prepare-model → complete-model` 留下 request/response 记录",
            "禁止经 `daily revise` 写入后标成“用户确认”",
            "`model_use=none` 只在用户明确选择不使用正式模型",
        ):
            self.assertIn(statement, self.content)

    def test_expired_objects_require_full_fresh_confirmation(self):
        for statement in (
            "RunManifest 或 FinalizationProposal 过期后",
            "完整展示新的正文、哈希、有效期和影响范围",
            "即使业务含义看起来相同，也禁止沿用旧确认",
            "重新 plan 并确认后创建新的 ReviewRun",
            "不能只报新 hash",
        ):
            self.assertIn(statement, self.content)

    def test_missing_core_evidence_cannot_publish_as_non_participation(self):
        for statement in (
            "槽位进入终态只表示采集流程结束，不表示证据够用",
            "STEP 1-6 的最低能力或完成状态仍有核心缺口",
            "必须在提案前停止",
            "不得把“来源失败、无法判断”包装成 `non_participation` 发布",
            "`non_participation` 只表示核心证据充分、分析已经完成",
        ):
            self.assertIn(statement, self.content)

    def test_temporary_files_and_user_facing_checks_stay_clean(self):
        for statement in (
            "只能写入操作系统临时目录",
            "不得创建 `data/evidence/_workbuddy_tmp/`",
            "不得向 `.workbuddy/memory/` 写入本次过程记录",
            "`next_day_checks` 只能写次日可直接观察的市场、板块或个股指标、单位与时点",
            "不得写 CLI 命令、capture/source 名、`allowed_objects`",
            "完整 `report_preview` 展示给用户",
        ):
            self.assertIn(statement, self.content)

    def test_skill_separates_read_only_proposal_from_atomic_publication(self):
        proposal_position = self.content.index("### 5.8 只读生成 FinalizationProposal")
        finalize_position = self.content.index("### 5.9 提交唯一正式 publication")
        self.assertLess(proposal_position, finalize_position)
        self.assertIn("只读提案不会写报告、应用动作或创建 publication", self.content)
        self.assertIn(
            "用户明确确认上一步完整提案和 `proposal_hash` 后执行",
            self.content,
        )
        self.assertIn("每个交易日只允许一个 current publication", self.content)
        self.assertIn(
            "reports/daily/<trade-date>/<run-id>/blobs/<report-hash>.md",
            self.content,
        )

    def test_skill_documents_logical_snapshot_migration_and_recovery(self):
        self.assertIn(
            "database plan-migration --database DB --backup-path BACKUP",
            self.content,
        )
        self.assertIn(
            "database migrate --database DB --plan-base64-stdin --plan-hash HASH --confirmed",
            self.content,
        )
        self.assertIn(
            "同一只读事务内对完整 schema、全部表记录（BLOB 规范化）",
            self.content,
        )
        self.assertIn("`application_id`、`user_version`", self.content)
        self.assertIn("包含已提交但尚未 checkpoint 的 WAL 页", self.content)
        self.assertIn("不是主文件逐字节哈希", self.content)
        self.assertIn("SQLite Online Backup", self.content)
        self.assertIn("`PRAGMA integrity_check`", self.content)
        self.assertIn("SQLite `BEGIN IMMEDIATE` 单写者门禁", self.content)
        self.assertIn("SQLite 原子回滚", self.content)
        self.assertIn("要求备份存在且逻辑快照匹配", self.content)
        self.assertIn("不得用旧备份自动覆盖主库或删除主库 sidecar", self.content)

    def test_skill_requires_structured_observation_results_and_versioned_weekly_input(self):
        self.assertIn("observation propose-result --observation-id ID", self.content)
        self.assertIn(
            "observation record-result --action-base64-stdin --action-hash HASH",
            self.content,
        )
        for reason in (
            "judgment_error",
            "insufficient_evidence",
            "observation_or_execution_deviation",
            "invalid_sample",
        ):
            self.assertIn(reason, self.content)
        self.assertIn("结果以不可变修订追加", self.content)
        self.assertIn("weekly create --end-date DATE", self.content)
        self.assertIn("只冻结该周期内 current daily publications", self.content)

    def test_skill_limits_verification_claim_to_structural_integrity(self):
        self.assertIn("`daily verify` 校验整条依赖链，失败返回非零", self.content)
        self.assertIn(
            "verify 通过只说明结构与哈希链有效，不说明市场判断正确",
            self.content,
        )
        self.assertIn(
            "不得把技术执行成功表述为投资判断正确或收益保证",
            self.content,
        )


if __name__ == "__main__":
    unittest.main()
