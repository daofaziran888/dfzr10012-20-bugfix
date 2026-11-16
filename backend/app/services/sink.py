"""流数据输出端业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.store import store

MODULE = "sink"
REQUIRED_FIELDS = ["配置编号", "输出类型", "目标地址"]
EDITABLE_FIELDS = [
    "配置编号",
    "输出类型",
    "目标地址",
    "写入模式",
    "批处理大小",
    "刷新间隔",
    "重试策略",
    "配置状态",
]
STATUS_ORDER = ["草稿", "已启用", "已暂停", "已关闭"]
ACTION_RULES = {"启用输出": "草稿", "暂停输出": "已启用", "关闭输出": "已暂停"}
NEGATIVE_ACTIONS = []


def _contract_snapshot(sink_code: str, field_id: str | None = None) -> dict[str, Any] | None:
    """输出端只认格式定义写下的最新字段版本快照，不自行推断契约。"""
    rows = [
        row for row in store.rows("schema_snapshot")
        if str(row.get("输出配置编号")) == sink_code and row.get("当前")
    ]
    if not rows:
        return None
    if field_id:
        scoped = [row for row in rows if str(row.get("字段编号")) == field_id]
        rows = scoped or rows
    rows.sort(key=lambda row: int(row.get("修订序号", 0)))
    return deepcopy(rows[-1])


class SinkService:
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
            rows = [row for row in rows if keyword in str(row.get("配置编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._with_contract(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        result = self._with_contract(entry)
        result["字段契约快照"] = _contract_snapshot(
            str(entry.get("配置编号") or ""),
            str(entry.get("字段编号") or "").strip() or None,
        )
        return result

    @staticmethod
    def _with_contract(row: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(row)
        field_id = str(row.get("字段编号") or "").strip()
        snapshot = _contract_snapshot(str(row.get("配置编号") or ""), field_id or None)
        if snapshot:
            # 消费侧写回自身状态：页面读到的版本与格式定义快照保持一致。
            result["字段编号"] = snapshot.get("字段编号")
            result["字段名称"] = snapshot.get("字段名称")
            result["字段类型"] = snapshot.get("字段类型")
            result["格式版本"] = snapshot.get("格式版本")
            result["契约版本"] = snapshot.get("格式版本")
            result["契约状态"] = "已同步"
        return result

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in EDITABLE_FIELDS if field in values})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"输出配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于流数据输出端可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"输出配置已{action}"
