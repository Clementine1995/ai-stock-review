# 本文件只读校验 ReviewRun 的事件、证据、修订和正式报告依赖链，不修复或改写任何状态。

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Callable

from stock_review.evidence.build_evidence_bundle import read_capture_payload
from stock_review.review_runs.action_set import ActionSet, FinalizationProposal
from stock_review.review_runs.canonical_hash import canonical_hash
from stock_review.review_runs.evidence_bundle import EvidenceBundle, EvidenceCapture
from stock_review.review_runs.review_revision import RevisionBundle
from stock_review.review_runs.review_run_event import ReviewRunEvent
from stock_review.review_runs.run_daily_review import load_run_manifest
from stock_review.review_runs.review_run import project_review_run
from stock_review.review_runs.validate_model_response import validate_revision_evidence
from stock_review.storage.review_run_repository import ReviewRunRepository


@dataclass(frozen=True)
class VerificationCheck:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True)
class DailyVerificationResult:
    run_id: str
    valid: bool
    state: str
    checks: tuple[VerificationCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "valid": self.valid,
            "state": self.state,
            "checks": [check.to_dict() for check in self.checks],
        }


# 每项检查独立收口错误，让调用方一次看到完整的损坏位置。
def verify_daily_review(
    run_id: str,
    repository: ReviewRunRepository,
) -> DailyVerificationResult:
    checks: list[VerificationCheck] = []
    context: dict[str, Any] = {}

    def run_check(name: str, operation: Callable[[], str]) -> None:
        try:
            detail = operation()
        except Exception as error:  # noqa: BLE001
            checks.append(VerificationCheck(name, False, f"{type(error).__name__}: {error}"))
        else:
            checks.append(VerificationCheck(name, True, detail))

    def verify_manifest() -> str:
        run_record = repository.get_review_run(run_id)
        if run_record is None:
            raise ValueError(f"未找到 ReviewRun：{run_id}")
        _verify_storage_hash(run_record, "ReviewRun")
        manifest = load_run_manifest(repository, run_id)
        manifest.validate_hash()
        if manifest.run_id != run_id:
            raise ValueError("RunManifest 的 run_id 与查询目标不一致。")
        context["manifest"] = manifest
        return f"{manifest.manifest_id} / {manifest.content_hash}"

    run_check("run_manifest", verify_manifest)
    manifest = context.get("manifest")
    if manifest is None:
        return DailyVerificationResult(run_id, False, "unknown", tuple(checks))

    def verify_events() -> str:
        records = repository.list_review_run_events(run_id)
        events: list[ReviewRunEvent] = []
        for position, record in enumerate(records, start=1):
            _verify_storage_hash(record, f"ReviewRunEvent #{position}")
            event = ReviewRunEvent.from_dict(record["payload"])
            if record["sequence_number"] != position:
                raise ValueError("ReviewRun 事件序号不连续。")
            expected_previous = events[-1].event_id if events else None
            if record["previous_event_id"] != expected_previous:
                raise ValueError("ReviewRun 事件前序指针断裂。")
            events.append(event)
        projection = project_review_run(manifest, tuple(events))
        context["events"] = tuple(events)
        context["projection"] = projection
        return f"{len(events)} 个事件，状态 {projection.state}"

    run_check("event_stream", verify_events)

    def verify_captures() -> str:
        verified = 0
        for slot in manifest.captures:
            record = repository.get_evidence_capture(f"CAPTURE-{slot.operation_id}")
            if record is None:
                continue
            _verify_storage_hash(record, f"EvidenceCapture {slot.slot_id}")
            capture = EvidenceCapture.from_dict(record["payload"]["capture"])
            if capture.slot_id != slot.slot_id or capture.operation_id != slot.operation_id:
                raise ValueError(f"采集槽位 {slot.slot_id} 的身份绑定错误。")
            read_capture_payload(Path(record["payload"]["output_path"]), capture)
            verified += 1
        projection = context.get("projection")
        if projection is not None:
            terminal = sum(item.status != "pending" for item in projection.capture_slots)
            if verified != terminal:
                raise ValueError("事件流中的终态 capture 与数据库或文件数量不一致。")
        return f"{verified} 个不可变 capture 文件通过哈希校验"

    run_check("evidence_captures", verify_captures)

    def verify_bundle() -> str:
        projection = context.get("projection")
        if projection is None or not projection.evidence_bundle_id:
            return "当前状态尚未封存 EvidenceBundle"
        record = repository.get_evidence_bundle(projection.evidence_bundle_id)
        if record is None:
            raise ValueError("事件流引用的 EvidenceBundle 不存在。")
        _verify_storage_hash(record, "EvidenceBundle")
        bundle = EvidenceBundle.from_dict(record["payload"])
        bundle.validate_against_manifest(manifest)
        if bundle.content_hash != projection.evidence_bundle_hash:
            raise ValueError("EvidenceBundle 与封存事件哈希不一致。")
        context["bundle"] = bundle
        return f"{bundle.evidence_bundle_id} / {bundle.content_hash}"

    run_check("evidence_bundle", verify_bundle)

    def verify_model_artifacts() -> str:
        events = context.get("events", ())
        request_events = [event for event in events if event.event_type == "model_requested"]
        completed_events = [event for event in events if event.event_type == "model_completed"]
        request_records = repository.list_model_requests(run_id)
        response_records = repository.list_model_responses(run_id)
        if len(request_records) != len(request_events):
            raise ValueError("模型请求记录与 model_requested 事件数量不一致。")
        if len(response_records) != len(completed_events):
            raise ValueError("模型响应记录与 model_completed 事件数量不一致。")

        requests: dict[str, dict[str, Any]] = {}
        for event in request_events:
            record = repository.get_model_request(event.subject_id)
            if record is None:
                raise ValueError(f"model_requested 事件缺少请求记录：{event.subject_id}")
            _verify_storage_hash(record, f"ModelRequest {event.subject_id}")
            payload = record["payload"]
            fingerprint = str(record.get("request_fingerprint") or "")
            unsigned = {key: value for key, value in payload.items() if key != "request_fingerprint"}
            if (
                payload.get("request_id") != event.subject_id
                or payload.get("run_id") != run_id
                or payload.get("trade_date") != manifest.trade_date
                or payload.get("manifest_hash") != manifest.content_hash
                or payload.get("request_fingerprint") != fingerprint
                or canonical_hash(unsigned) != fingerprint
                or event.model_request_hash != fingerprint
                or event.operation_id != payload.get("operation_id")
                or event.model_id != payload.get("model_id")
                or event.evidence_bundle_id != payload.get("evidence_bundle_id")
                or event.evidence_bundle_hash != payload.get("evidence_bundle_hash")
                or event.base_review_revision_id != payload.get("base_revision_id")
                or event.expected_current_revision_id
                != payload.get("expected_current_revision_id")
            ):
                raise ValueError(f"模型请求 {event.subject_id} 的记录、指纹或事件绑定不一致。")
            requests[event.subject_id] = record

        response_hashes: set[str] = set()
        completed_operations: dict[str, ReviewRunEvent] = {}
        for event in completed_events:
            response_id = _stable_model_artifact_id(
                "MODELRESPONSE", run_id, event.operation_id
            )
            record = repository.get_model_response(response_id)
            if record is None:
                raise ValueError(f"model_completed 事件缺少响应记录：{response_id}")
            _verify_storage_hash(record, f"ModelResponse {response_id}")
            request = requests.get(event.subject_id)
            if request is None:
                raise ValueError(f"模型响应引用了不存在的请求：{event.subject_id}")
            payload = record["payload"]
            request_payload = request["payload"]
            response_hash = str(payload.get("response_hash") or "")
            if (
                record.get("model_response_id") != response_id
                or record.get("run_id") != run_id
                or record.get("model_request_id") != event.subject_id
                or record.get("request_fingerprint") != request.get("request_fingerprint")
                or record.get("evidence_bundle_id")
                != request.get("evidence_bundle_id")
                or record.get("base_review_revision_id")
                != request.get("base_review_revision_id")
                or record.get("expected_current_revision_id")
                != request.get("expected_current_revision_id")
                or payload.get("model_response_id") != response_id
                or payload.get("request_id") != event.subject_id
                or payload.get("request_fingerprint") != request.get("request_fingerprint")
                or payload.get("operation_id") != event.operation_id
                or canonical_hash(payload.get("response")) != response_hash
                or event.model_request_hash != request.get("request_fingerprint")
                or event.model_response_hash != response_hash
                or event.evidence_bundle_id != request.get("evidence_bundle_id")
                or event.evidence_bundle_hash
                != request_payload.get("evidence_bundle_hash")
                or event.model_id != request_payload.get("model_id")
                or event.base_review_revision_id != request.get("base_review_revision_id")
                or event.expected_current_revision_id
                != request.get("expected_current_revision_id")
            ):
                raise ValueError(f"模型响应 {response_id} 的记录、请求或事件绑定不一致。")
            response_hashes.add(response_hash)
            completed_operations[event.operation_id] = event

        revision_events = [event for event in events if event.event_type == "revision_added"]
        for operation_id, event in completed_operations.items():
            matching = [item for item in revision_events if item.operation_id == operation_id]
            if len(matching) != 1:
                raise ValueError(f"模型完成 operation {operation_id} 未绑定唯一修订事件。")
            revision_record = repository.get_review_revision(matching[0].review_revision_id)
            if revision_record is None:
                raise ValueError(f"模型完成 operation {operation_id} 缺少修订记录。")
            revision = RevisionBundle.from_dict(revision_record["payload"])
            if event.model_response_hash not in revision.model_response_hashes:
                raise ValueError(f"模型完成 operation {operation_id} 的响应未进入对应修订。")

        for event in revision_events:
            record = repository.get_review_revision(event.review_revision_id)
            if record is None:
                continue
            revision = RevisionBundle.from_dict(record["payload"])
            unknown_hashes = set(revision.model_response_hashes) - response_hashes
            if unknown_hashes:
                raise ValueError(
                    f"修订 {revision.review_revision_id} 引用了缺失的模型响应哈希。"
                )
        context["completed_model_response_hashes"] = response_hashes
        projection = context.get("projection")
        if (
            manifest.model_use.mode == "required"
            and projection is not None
            and projection.state == "published"
            and not response_hashes
        ):
            raise ValueError("已发布运行违反 required 模型约束：没有已完成的模型响应。")
        return f"{len(request_events)} 个请求、{len(completed_events)} 个响应通过交叉校验"

    run_check("model_artifacts", verify_model_artifacts)

    def verify_revisions() -> str:
        events = context.get("events", ())
        bundle = context.get("bundle")
        count = 0
        for event in events:
            if event.event_type != "revision_added":
                continue
            record = repository.get_review_revision(event.review_revision_id)
            if record is None:
                raise ValueError(f"事件引用的复盘修订不存在：{event.review_revision_id}")
            _verify_storage_hash(record, f"RevisionBundle {event.review_revision_id}")
            revision = RevisionBundle.from_dict(record["payload"])
            if revision.replaces_review_revision_id != event.expected_current_revision_id:
                raise ValueError(f"修订 {event.review_revision_id} 的替代关系与事件不一致。")
            if bundle is not None:
                validate_revision_evidence(revision, bundle, repository)
            count += 1
        projection = context.get("projection")
        if (
            manifest.model_use.mode == "required"
            and projection is not None
            and projection.state == "published"
        ):
            current_record = repository.get_review_revision(
                projection.current_review_revision_id
            )
            if current_record is None:
                raise ValueError("required 模型约束无法核对当前正式修订。")
            current_revision = RevisionBundle.from_dict(current_record["payload"])
            completed_hashes = set(
                context.get("completed_model_response_hashes", set())
            )
            if not completed_hashes.intersection(
                current_revision.model_response_hashes
            ):
                raise ValueError(
                    "已发布运行违反 required 模型约束：当前正式修订未继承模型响应。"
                )
        return f"{count} 个事件可达修订通过内容与证据边界校验"

    run_check("review_revisions", verify_revisions)

    def verify_publication() -> str:
        projection = context.get("projection")
        if projection is None or not projection.publication_id:
            return "当前运行尚未发布"
        publications = repository.list_review_publications(manifest.trade_date)
        publication = next(
            (
                item
                for item in publications
                if item["publication_id"] == projection.publication_id
            ),
            None,
        )
        if publication is None:
            raise ValueError("publication_completed 事件引用的 publication 不存在。")
        _verify_storage_hash(publication, "ReviewPublication")
        if publication["run_id"] != run_id:
            raise ValueError("ReviewPublication 的 run_id 与事件流不一致。")
        report_path = Path(str(publication["payload"].get("report_path") or ""))
        if not report_path.is_file():
            raise ValueError(f"正式报告文件不存在：{report_path}")
        report_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
        if report_hash != publication["report_hash"]:
            raise ValueError("正式报告文件与 publication 的 report_hash 不一致。")
        action_set_id = publication.get("action_set_id")
        if action_set_id:
            action_record = repository.get_action_set(action_set_id)
            if action_record is None:
                raise ValueError("ReviewPublication 引用的 ActionSet 不存在。")
            _verify_storage_hash(action_record, "ActionSet")
            domain_payload = action_record["payload"].get("domain_action_set")
            if not isinstance(domain_payload, dict):
                raise ValueError("存储 ActionSet 缺少领域对象快照。")
            action_set = ActionSet.from_dict(domain_payload)
            if action_set.report_hash != report_hash:
                raise ValueError("ActionSet 与正式报告哈希不一致。")
            proposal_id = action_record.get("proposal_id")
            proposal_record = repository.get_finalization_proposal(proposal_id)
            if proposal_record is None:
                raise ValueError("ActionSet 引用的 FinalizationProposal 不存在。")
            _verify_storage_hash(proposal_record, "FinalizationProposal")
            proposal = FinalizationProposal.from_dict(proposal_record["payload"])
            if proposal.action_set.content_hash != action_set.content_hash:
                raise ValueError("FinalizationProposal 与 ActionSet 内容不一致。")
        current = repository.get_current_review_publication(manifest.trade_date)
        current_label = (
            "当前正式版本"
            if current is not None and current["publication_id"] == projection.publication_id
            else "已被后续正式版本替代"
        )
        return f"{projection.publication_id} / {report_hash} / {current_label}"

    run_check("review_publication", verify_publication)
    state = getattr(context.get("projection"), "state", "unknown")
    return DailyVerificationResult(
        run_id=run_id,
        valid=all(check.ok for check in checks),
        state=state,
        checks=tuple(checks),
    )


# show 返回运行投影、当前版本锚点和截至该交易日仍待回填的 Observation。
def describe_daily_review(
    run_id: str,
    repository: ReviewRunRepository,
) -> dict[str, Any]:
    manifest = load_run_manifest(repository, run_id)
    events = tuple(
        ReviewRunEvent.from_dict(record["payload"])
        for record in repository.list_review_run_events(run_id)
    )
    projection = project_review_run(manifest, events)
    revision = None
    if projection.current_review_revision_id:
        revision_record = repository.get_review_revision(
            projection.current_review_revision_id
        )
        revision = revision_record["payload"] if revision_record is not None else None
    publication = None
    if projection.publication_id:
        publication = next(
            (
                item
                for item in repository.list_review_publications(manifest.trade_date)
                if item["publication_id"] == projection.publication_id
            ),
            None,
        )
    observations = [
        item
        for item in repository.list_observations(review_date=manifest.trade_date)
        if item.get("run_id") == run_id
    ]
    if publication is not None:
        # 已发布运行只展示同一 publication 固定的最终修订和 ActionSet 所创建的 Observation。
        observations = [
            item
            for item in observations
            if item.get("review_revision_id") == publication["review_revision_id"]
            and item.get("action_set_id") == publication["action_set_id"]
        ]
    # 到期项来自历史已发布运行，单独展示，不能混入目标运行自己的 Observation 依赖。
    due_observations = [
        item
        for item in repository.list_observations(
            due_on_or_before=manifest.trade_date
        )
        if item.get("current_result", {}).get("status") == "pending"
    ]
    return {
        "run": projection.to_dict(),
        "manifest": manifest.to_dict(),
        "current_review_revision": revision,
        "publication": publication,
        "observations": observations,
        "due_observations": due_observations,
    }


def _verify_storage_hash(record: dict[str, Any], label: str) -> None:
    payload = record.get("payload")
    stored_hash = record.get("content_hash")
    if not isinstance(payload, dict) or stored_hash != canonical_hash(payload):
        raise ValueError(f"{label} 的存储内容哈希不一致。")


def _stable_model_artifact_id(prefix: str, run_id: str, operation_id: str) -> str:
    digest = canonical_hash({"run_id": run_id, "operation_id": operation_id})[:24]
    return f"{prefix}-{digest}"
