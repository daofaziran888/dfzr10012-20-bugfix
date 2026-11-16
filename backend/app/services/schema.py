"""流数据格式定义业务规则：状态流转、字段校验、版本快照与筛选口径都收在这里。

发布格式时必须把「字段名称 + 字段类型 + 格式版本」一次性写齐，并在
``schema_snapshot`` 里留下一份字段版本快照；详情面板、导出报告和流数据输出端
都消费同一份快照，避免刷新后格式版本回退、字段类型丢失或输出契约不一致。
"""
from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "schema"
SNAPSHOT_MODULE = "schema_snapshot"
REQUIRED_FIELDS = ["定义编号", "流名称", "字段名称"]
STATUS_ORDER = ["草稿", "已发布", "已废弃", "已冻结"]
# 动作 -> (允许的来源状态集合, 目标状态)。发布只向前推进，不再把状态写回草稿。
ACTION_RULES: dict[str, tuple[tuple[str, ...], str]] = {
    "发布格式": (("草稿", "已发布", "已废弃"), "已发布"),
    "废弃格式": (("已发布",), "已废弃"),
    "冻结格式": (("已废弃",), "已冻结"),
}
NEGATIVE_ACTIONS = []

# 创建/修订时允许落库的格式定义字段，避免合法字段被静默丢弃。
EDITABLE_FIELDS = [
    "定义编号",
    "流名称",
    "字段编号",
    "字段名称",
    "字段类型",
    "默认值",
    "可空标识",
    "格式版本",
    "定义状态",
]
# 修订请求里兼容中英文键名，前端和外部调用都可以提交。
REVISION_ALIASES = {
    "字段编号": ("字段编号", "field", "field_id", "fieldId"),
    "字段名称": ("字段名称", "name", "field_name", "fieldName"),
    "字段类型": ("字段类型", "type", "field_type", "fieldType"),
    "格式版本": ("格式版本", "version", "format_version", "formatVersion"),
    "写入模式": ("写入模式", "mode", "write_mode", "writeMode"),
    "输出配置编号": ("输出配置编号", "sink", "sink_code", "sinkCode", "配置编号"),
    "修订号": ("修订号", "revision_id", "revisionId", "revision"),
}


class SchemaService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("定义编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        # 列表同样以最新确认快照为准，刷新前后、列表与详情看到的是同一份契约。
        return [self._overlay_snapshot(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        result = self._overlay_snapshot(entry)
        result["历史快照"] = self.list_snapshots(str(entry.get("定义编号") or ""))
        return result

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        with store.lock:
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            # 保留所有合法字段，字段类型与格式版本不能在这一步被丢掉。
            entry.update({field: values.get(field) for field in EDITABLE_FIELDS if field in values})
            entry["status"] = STATUS_ORDER[0]
            entry.setdefault("定义状态", STATUS_ORDER[0])
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
        return entry, []

    # ------------------------------------------------------------------
    # 快照读取：详情、报告和输出端共用同一份数据
    # ------------------------------------------------------------------
    def list_snapshots(self, code: str, field_id: str | None = None) -> list[dict[str, Any]]:
        if not code:
            return []
        rows = store.rows(SNAPSHOT_MODULE)
        matched = [
            deepcopy(row)
            for row in rows
            if str(row.get("定义编号")) == code
            and (field_id is None or str(row.get("字段编号")) == field_id)
        ]
        matched.sort(key=lambda row: int(row.get("修订序号", 0)))
        return matched

    def latest_snapshot(self, code: str, field_id: str | None = None) -> dict[str, Any] | None:
        snapshots = self.list_snapshots(code, field_id)
        for snapshot in reversed(snapshots):
            if snapshot.get("当前"):
                return snapshot
        return snapshots[-1] if snapshots else None

    def snapshot_for_entry(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """按行上的字段编号取快照；老记录没有字段编号时退回到最新确认快照。"""
        code = str(entry.get("定义编号") or "")
        field_id = str(entry.get("字段编号") or "").strip()
        snapshot = self.latest_snapshot(code, field_id) if field_id else None
        return snapshot or self.latest_snapshot(code)

    def snapshot_for_sink(self, sink_code: str, field_id: str | None = None) -> dict[str, Any] | None:
        if not sink_code:
            return None
        rows = [
            row for row in store.rows(SNAPSHOT_MODULE)
            if str(row.get("输出配置编号")) == sink_code and row.get("当前")
        ]
        if not rows:
            return None
        if field_id:
            scoped = [row for row in rows if str(row.get("字段编号")) == field_id]
            rows = scoped or rows
        rows.sort(key=lambda row: int(row.get("修订序号", 0)))
        return deepcopy(rows[-1])

    def report(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for entry in store.rows(MODULE):
            code = str(entry.get("定义编号") or "")
            snapshot = self.snapshot_for_entry(entry)
            items.append({
                "定义编号": code,
                "流名称": entry.get("流名称"),
                "字段编号": snapshot.get("字段编号") if snapshot else entry.get("字段编号"),
                "字段名称": snapshot.get("字段名称") if snapshot else entry.get("字段名称"),
                "字段类型": snapshot.get("字段类型") if snapshot else entry.get("字段类型"),
                "格式版本": snapshot.get("格式版本") if snapshot else entry.get("格式版本"),
                "定义状态": snapshot.get("定义状态") if snapshot else entry.get("定义状态"),
                "修订序号": snapshot.get("修订序号") if snapshot else None,
                "输出配置编号": snapshot.get("输出配置编号") if snapshot else None,
                "写入模式": snapshot.get("写入模式") if snapshot else None,
            })
        return {
            "module": MODULE,
            "total": len(items),
            "items": items,
            "snapshots": [deepcopy(row) for row in store.rows(SNAPSHOT_MODULE)],
        }

    # ------------------------------------------------------------------
    # 发布/修订：字段类型与格式版本一次写齐
    # ------------------------------------------------------------------
    def publish_revision(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        payload = self._normalise_revision(values)
        # 串行进入临界区；事务保证任何一步失败都退回上一完整快照。
        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"格式定义 {entry_id} 不存在或已归档", None
            if str(entry.get("status")) == STATUS_ORDER[-1]:
                return None, f"格式定义 {entry_id} 已冻结，字段版本不可再修订", None

            code = str(entry.get("定义编号") or "").strip()
            if not code:
                return None, "格式定义缺少定义编号，无法写入字段版本快照", None

            name = payload["字段名称"] or str(entry.get("字段名称") or "").strip()
            type_ = payload["字段类型"] or str(entry.get("字段类型") or "").strip()
            version = payload["格式版本"] or str(entry.get("格式版本") or "").strip()
            # 兼容「amount:int」这种字段类型连写，拆出完整名称和类型。
            if ":" in type_:
                raw_name, raw_type = type_.split(":", 1)
                if raw_name.strip():
                    name = payload["字段名称"] or raw_name.strip()
                if raw_type.strip():
                    type_ = raw_type.strip()
            field_id = (
                payload["字段编号"]
                or str(entry.get("字段编号") or "").strip()
                or f"field-{entry_id}"
            )
            missing = [
                label for label, value in (
                    ("字段名称", name),
                    ("字段类型", type_),
                    ("格式版本", version),
                ) if not value
            ]
            if missing:
                return None, f"缺少必填字段：{'、'.join(missing)}", None

            sink_code = payload["输出配置编号"] or str(entry.get("输出配置编号") or "").strip()
            mode = payload["写入模式"] or ""
            sink = self._find_sink(sink_code) if sink_code else None
            if sink_code and sink is None:
                return None, f"输出配置 {sink_code} 不存在或已归档，字段契约无法同步", None
            if sink_code and not mode:
                mode = str(sink.get("写入模式") or "").strip() if sink else ""
                if not mode:
                    return None, f"输出配置 {sink_code} 缺少写入模式，字段契约无法同步", None

            # 校验只认最新确认的字段版本快照，行上的旧值不能作为依据。
            if payload["字段编号"]:
                confirmed = self.latest_snapshot(code, field_id)
            else:
                confirmed = self.latest_snapshot(code, field_id) or self.latest_snapshot(code)
            contract = self.snapshot_for_sink(sink_code) if sink_code else None
            conflict = self._check_contract_conflict(
                confirmed=confirmed,
                contract=contract,
                field_id=field_id,
                name=name,
                type_=type_,
                version=version,
            )
            if conflict:
                return None, conflict, None

            revision_key = payload["修订号"] or "|".join([
                code, field_id, name, type_, version, sink_code, mode,
            ])
            existing = next(
                (
                    row for row in store.rows(SNAPSHOT_MODULE)
                    if str(row.get("修订键")) == revision_key
                ),
                None,
            )
            if existing is not None:
                # 重复修订（刷新后重放、并发重试）只生效一次：后到的请求整单拒绝。
                return None, "该修订已确认，未重复写入字段版本快照", deepcopy(existing)

            same_version = [
                row for row in store.rows(SNAPSHOT_MODULE)
                if str(row.get("定义编号")) == code
                and str(row.get("字段编号")) == field_id
                and str(row.get("格式版本")) == version
            ]
            if same_version:
                return (
                    None,
                    f"格式版本 {version} 已存在确认快照，同一版本只接受一次修订",
                    None,
                )

            snapshots = store.rows(SNAPSHOT_MODULE)
            for row in snapshots:
                if str(row.get("定义编号")) == code and str(row.get("字段编号")) == field_id:
                    row["当前"] = False
            sequence = max(
                (
                    int(row.get("修订序号", 0))
                    for row in snapshots
                    if str(row.get("定义编号")) == code
                    and str(row.get("字段编号")) == field_id
                ),
                default=0,
            ) + 1
            snapshot = {
                "id": max((int(row.get("id", 0)) for row in snapshots), default=0) + 1,
                "定义编号": code,
                "字段编号": field_id,
                "字段名称": name,
                "字段类型": type_,
                "格式版本": version,
                "写入模式": mode,
                "输出配置编号": sink_code,
                "修订键": revision_key,
                "修订序号": sequence,
                "当前": True,
                "定义状态": "已发布",
                "创建时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            snapshots.append(snapshot)

            # 字段名称、字段类型、格式版本必须一次写齐，不能只更新其中一项。
            entry.update({
                "字段编号": field_id,
                "字段名称": name,
                "字段类型": type_,
                "格式版本": version,
                "定义状态": "已发布",
                "输出配置编号": sink_code or entry.get("输出配置编号"),
                "status": "已发布",
                "pending": True,
                "abnormal": False,
            })

            if sink is not None:
                # 消费侧写回自身状态：输出端与格式定义共享同一份快照。
                contracts = dict(sink.get("字段契约表") or {})
                contracts[field_id] = {
                    "字段名称": name,
                    "字段类型": type_,
                    "格式版本": version,
                    "写入模式": mode,
                    "修订序号": sequence,
                }
                sink.update({
                    "写入模式": mode,
                    "字段编号": field_id,
                    "字段名称": name,
                    "字段类型": type_,
                    "格式版本": version,
                    "字段契约": f"{field_id}: {name}:{type_}",
                    "契约版本": version,
                    "契约快照序号": sequence,
                    "契约状态": "已同步",
                    "字段契约表": contracts,
                })

            result = deepcopy(entry)
            result["当前快照"] = deepcopy(snapshot)
            result["历史快照"] = self.list_snapshots(code, field_id)
            return result, f"格式定义已发布，字段版本快照保留 {name}:{type_} / {version}", deepcopy(snapshot)

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"格式定义 {entry_id} 不存在或已归档", None
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于流数据格式定义可执行范围", None

        if action == "发布格式":
            # 发布入口统一走修订逻辑：即使没有携带新字段，也要落一份当前快照。
            revision_values = dict(values or {})
            revision_values.setdefault("字段名称", entry.get("字段名称"))
            revision_values.setdefault("字段类型", entry.get("字段类型"))
            revision_values.setdefault("格式版本", entry.get("格式版本"))
            revision_values.setdefault("字段编号", entry.get("字段编号"))
            return self.publish_revision(entry_id, revision_values)

        sources, target = ACTION_RULES[action]
        if str(entry.get("status")) not in sources:
            return (
                None,
                f"当前状态「{entry.get('status')}」不允许执行「{action}」，"
                f"需先处于「{'、'.join(sources)}」",
                None,
            )
        with store.transaction():
            entry["status"] = target
            entry["定义状态"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            return deepcopy(entry), f"格式定义已{action}", None

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _overlay_snapshot(self, entry: dict[str, Any]) -> dict[str, Any]:
        """把最新确认快照叠加到行数据上，避免行上的旧字段类型/版本泄漏出去。"""
        result = deepcopy(entry)
        snapshot = self.snapshot_for_entry(entry)
        result["当前快照"] = deepcopy(snapshot) if snapshot else None
        if snapshot:
            result["字段编号"] = snapshot.get("字段编号")
            result["字段名称"] = snapshot.get("字段名称")
            result["字段类型"] = snapshot.get("字段类型")
            result["格式版本"] = snapshot.get("格式版本")
        return result

    @staticmethod
    def _normalise_revision(values: dict[str, Any]) -> dict[str, str]:
        payload: dict[str, str] = {}
        for field, aliases in REVISION_ALIASES.items():
            for alias in aliases:
                raw = values.get(alias)
                if raw is not None and str(raw).strip():
                    payload[field] = str(raw).strip()
                    break
            payload.setdefault(field, "")
        return payload

    @staticmethod
    def _find_sink(sink_code: str) -> dict[str, Any] | None:
        if not sink_code:
            return None
        for row in store.rows("sink"):
            if str(row.get("配置编号")) == sink_code:
                return row
        return None

    @staticmethod
    def _version_rank(version: str) -> int | None:
        match = re.search(r"(\d+)", str(version or ""))
        return int(match.group(1)) if match else None

    def _check_contract_conflict(
        self,
        *,
        confirmed: dict[str, Any] | None,
        contract: dict[str, Any] | None,
        field_id: str,
        name: str,
        type_: str,
        version: str,
    ) -> str | None:
        confirmed = confirmed or contract
        if not confirmed:
            return None
        if str(confirmed.get("字段编号") or "") != field_id:
            return None
        confirmed_version = str(confirmed.get("格式版本") or "")
        confirmed_type = str(confirmed.get("字段类型") or "")
        confirmed_name = str(confirmed.get("字段名称") or "")

        incoming_rank = self._version_rank(version)
        confirmed_rank = self._version_rank(confirmed_version)
        if (
            incoming_rank is not None
            and confirmed_rank is not None
            and incoming_rank < confirmed_rank
        ):
            return (
                f"格式版本 {version} 早于已确认快照 {confirmed_version}，"
                "拒绝回退旧版本"
            )
        if confirmed_version and version == confirmed_version:
            if confirmed_type and type_ != confirmed_type:
                return (
                    f"格式版本 {version} 已与输出契约确认字段类型 {confirmed_type}，"
                    f"不能在保留旧版本号的情况下写入新类型 {type_}"
                )
            if confirmed_name and name != confirmed_name:
                return (
                    f"格式版本 {version} 已与输出契约确认字段名称 {confirmed_name}，"
                    f"不能改写成 {name}；请提升格式版本后再修订"
                )
        return None
