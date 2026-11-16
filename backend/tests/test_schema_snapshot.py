"""格式定义字段版本快照的回归测试：版本落库、原子保存、幂等与跨页面契约同步。"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.schema import SchemaService
from app.services.sink import SinkService
from app.store import Store, store


@pytest.fixture(autouse=True)
def fresh_store(monkeypatch: pytest.MonkeyPatch) -> Store:
    """每个用例一份独立仓库，避免用例之间互相污染。"""
    isolated = Store()
    monkeypatch.setattr("app.services.schema.store", isolated)
    monkeypatch.setattr("app.services.sink.store", isolated)
    return isolated


@pytest.fixture
def service() -> SchemaService:
    return SchemaService()


def _entry(store_: Store, module: str, code_field: str, code: str) -> dict:
    return next(row for row in store_.rows(module) if row.get(code_field) == code)


def test_acceptance_sample_shows_format_v2_everywhere(service: SchemaService, fresh_store: Store) -> None:
    detail = service.get_entry(2)
    assert detail is not None
    assert (detail["字段编号"], detail["字段名称"], detail["字段类型"], detail["格式版本"]) == (
        "field-2", "amount", "int", "format-v2",
    )

    rows, _ = service.list_entries(keyword="SCHE-0002")
    assert rows[0]["格式版本"] == "format-v2"
    assert rows[0]["字段类型"] == "int"

    report = next(item for item in service.report()["items"] if item["定义编号"] == "SCHE-0002")
    assert (report["字段类型"], report["格式版本"], report["写入模式"]) == ("int", "format-v2", "append")

    sink = SinkService().get_entry(2)
    assert sink is not None
    assert (sink["写入模式"], sink["格式版本"], sink["字段类型"]) == ("append", "format-v2", "int")


def test_repeated_publish_only_applies_once(service: SchemaService, fresh_store: Store) -> None:
    before = len(fresh_store.rows("schema_snapshot"))
    _, first, _ = service.run_action(2, "发布格式", {})
    _, second, _ = service.run_action(2, "发布格式", {})
    assert "已确认" in second or "未重复" in second
    assert len(fresh_store.rows("schema_snapshot")) == before


def test_same_version_with_new_type_is_rejected(service: SchemaService) -> None:
    entry, message, _ = service.publish_revision(
        2, {"字段编号": "field-2", "字段名称": "amount", "字段类型": "decimal", "格式版本": "format-v2"},
    )
    assert entry is None
    assert "format-v2" in message and "int" in message
    # 版本号没有推进，字段类型也不能被偷写进去。
    assert service.get_entry(2)["字段类型"] == "int"
    assert SinkService().get_entry(2)["字段类型"] == "int"


def test_old_version_cannot_roll_back(service: SchemaService) -> None:
    entry, message, _ = service.publish_revision(
        2, {"字段编号": "field-2", "字段名称": "amount", "字段类型": "int", "格式版本": "format-v1"},
    )
    assert entry is None
    assert "回退" in message
    assert service.get_entry(2)["格式版本"] == "format-v2"


def test_new_version_writes_types_and_contract_together(service: SchemaService, fresh_store: Store) -> None:
    entry, message, snapshot = service.publish_revision(
        2,
        {
            "字段编号": "field-2",
            "字段名称": "amount",
            "字段类型": "bigint",
            "格式版本": "format-v3",
            "输出配置编号": "SINK-0002",
        },
    )
    assert entry is not None, message
    assert snapshot is not None
    assert (snapshot["字段类型"], snapshot["格式版本"]) == ("bigint", "format-v3")

    history = service.list_snapshots("SCHE-0002", "field-2")
    assert [(row["格式版本"], row["当前"]) for row in history] == [("format-v2", False), ("format-v3", True)]

    sink = SinkService().get_entry(2)
    assert (sink["字段类型"], sink["格式版本"], sink["写入模式"]) == ("bigint", "format-v3", "append")
    assert sink["契约状态"] == "已同步"


def test_save_failure_rolls_back_entry_snapshot_and_contract(
    service: SchemaService, fresh_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    before_entry = dict(fresh_store.find("schema", 2))
    before_sink = dict(fresh_store.find("sink", 2))
    before_snapshots = [dict(row) for row in fresh_store.rows("schema_snapshot")]

    class FailingSnapshots(list):
        def append(self, item):  # noqa: ANN001 - 测试桩
            raise RuntimeError("模拟落库失败")

    real_rows = fresh_store._tables["schema_snapshot"]
    # 事务回滚会把整表换成备份副本，因此注入失败后不需要手动还原包装列表。
    fresh_store._tables["schema_snapshot"] = FailingSnapshots(real_rows)
    with pytest.raises(RuntimeError):
        service.publish_revision(
            2,
            {"字段编号": "field-2", "字段名称": "amount", "字段类型": "bigint",
             "格式版本": "format-v9", "输出配置编号": "SINK-0002"},
        )

    assert fresh_store.find("schema", 2) == before_entry
    assert fresh_store.find("sink", 2) == before_sink
    assert fresh_store.rows("schema_snapshot") == before_snapshots


def test_concurrent_revisions_accept_only_one(service: SchemaService, fresh_store: Store) -> None:
    results: list[tuple[bool, str]] = []

    def submit() -> None:
        entry, message, _ = service.publish_revision(
            2,
            {"字段编号": "field-2", "字段名称": "amount", "字段类型": "bigint",
             "格式版本": "format-v4", "输出配置编号": "SINK-0002"},
        )
        results.append((entry is not None, message))

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(1 for ok, _ in results if ok) == 1
    accepted = [row for row in fresh_store.rows("schema_snapshot") if row["格式版本"] == "format-v4"]
    assert len(accepted) == 1


def test_http_flat_payload_keeps_field_type_and_version() -> None:
    client = TestClient(app)
    response = client.post("/api/schema/2/revisions", json={"字段类型": "decimal", "格式版本": "format-v2"})
    body = response.json()
    assert body["ok"] is False
    assert "format-v2" in body["message"]

    response = client.post(
        "/api/schema/2/revisions",
        json={"字段编号": "field-2", "字段名称": "amount", "字段类型": "decimal", "格式版本": "format-v6"},
    )
    body = response.json()
    assert body["ok"] is True, body
    assert body["entry"]["字段类型"] == "decimal"
    assert body["entry"]["格式版本"] == "format-v6"

    detail = client.get("/api/schema/2").json()
    assert (detail["字段类型"], detail["格式版本"]) == ("decimal", "format-v6")
