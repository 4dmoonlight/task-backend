"""接口集成测试：在真实 Postgres 上验证读写、过滤与持久化。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, text

import db as db_module
from db import Item
from main import app

ITEMS = "/api/v1/items"

IS_POSTGRES = db_module.engine.dialect.name == "postgresql"


@pytest.fixture()
def client():
    # 进入上下文会触发 lifespan，由 init_db 建表
    with TestClient(app) as test_client:
        with db_module.engine.begin() as conn:
            # 每个用例前清空，保证用例之间互不影响
            if IS_POSTGRES:
                conn.execute(text("TRUNCATE TABLE items RESTART IDENTITY"))
            else:
                # 本地可用 SQLite 跑逻辑回归，CI 上始终是 Postgres
                conn.execute(delete(Item))
        yield test_client


def _create(client, **payload):
    response = client.post(ITEMS, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_health_reports_database_reachable(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_create_trims_title_and_sets_timestamps(client):
    item = _create(client, title="  买牛奶  ", content="去超市")
    assert item["title"] == "买牛奶"
    assert item["completed"] is False
    assert item["created_at"] and item["updated_at"]


@pytest.mark.parametrize("title", ["", "   "])
def test_create_rejects_blank_title(client, title):
    assert client.post(ITEMS, json={"title": title}).status_code == 422


def test_list_keeps_id_order_and_supports_filters(client):
    _create(client, title="待办一")
    _create(client, title="待办二", completed=True)
    _create(client, title="待办三")

    all_items = client.get(ITEMS).json()
    assert [i["title"] for i in all_items] == ["待办一", "待办二", "待办三"]
    ids = [i["id"] for i in all_items]
    assert ids == sorted(ids), "列表应按 id 升序返回"

    done = client.get(ITEMS, params={"completed": "true"}).json()
    assert [i["title"] for i in done] == ["待办二"]

    active = client.get(ITEMS, params={"completed": "false"}).json()
    assert [i["title"] for i in active] == ["待办一", "待办三"]


def test_list_pagination(client):
    for n in range(5):
        _create(client, title=f"任务{n}")

    page = client.get(ITEMS, params={"limit": 2, "offset": 2}).json()
    assert [i["title"] for i in page] == ["任务2", "任务3"]
    assert client.get(ITEMS, params={"limit": 0}).status_code == 422


def test_search_matches_title_and_content(client):
    _create(client, title="买牛奶", content="去超市")
    _create(client, title="写周报")

    assert [i["title"] for i in client.get(ITEMS, params={"q": "牛奶"}).json()] == ["买牛奶"]
    assert [i["title"] for i in client.get(ITEMS, params={"q": "超市"}).json()] == ["买牛奶"]
    assert client.get(ITEMS, params={"q": "不存在"}).json() == []


def test_search_escapes_like_wildcards(client):
    _create(client, title="买牛奶")
    # % 应作为普通字符匹配，而不是通配符返回全部数据
    assert client.get(ITEMS, params={"q": "%"}).json() == []
    _create(client, title="折扣 50% 商品")
    assert [i["title"] for i in client.get(ITEMS, params={"q": "50%"}).json()] == ["折扣 50% 商品"]


def test_get_item_and_missing_item(client):
    item = _create(client, title="待办")
    assert client.get(f"{ITEMS}/{item['id']}").json()["title"] == "待办"
    assert client.get(f"{ITEMS}/999").status_code == 404


def test_patch_updates_only_given_fields(client):
    item = _create(client, title="写周报", content="原内容")

    updated = client.patch(f"{ITEMS}/{item['id']}", json={"completed": True}).json()
    assert updated["completed"] is True
    assert updated["content"] == "原内容"
    assert updated["updated_at"] >= item["updated_at"]

    assert client.patch(f"{ITEMS}/{item['id']}", json={}).status_code == 400
    assert client.patch(f"{ITEMS}/999", json={"completed": True}).status_code == 404


def test_put_replaces_optional_fields(client):
    item = _create(client, title="写周报", content="原内容", completed=True)

    replaced = client.put(f"{ITEMS}/{item['id']}", json={"title": "写月报"}).json()
    assert replaced["title"] == "写月报"
    assert replaced["content"] is None
    assert replaced["completed"] is False


def test_toggle_flips_completed(client):
    item = _create(client, title="健身")
    assert client.post(f"{ITEMS}/{item['id']}/toggle").json()["completed"] is True
    assert client.post(f"{ITEMS}/{item['id']}/toggle").json()["completed"] is False
    assert client.post(f"{ITEMS}/999/toggle").status_code == 404


def test_delete_item_and_missing_item(client):
    item = _create(client, title="临时任务")
    assert client.delete(f"{ITEMS}/{item['id']}").status_code == 204
    assert client.delete(f"{ITEMS}/{item['id']}").status_code == 404


@pytest.mark.skipif(not IS_POSTGRES, reason="SQLite 会复用 rowid，该约束只在 Postgres 序列上成立")
def test_ids_are_not_reused_after_delete(client):
    item = _create(client, title="临时任务")
    client.delete(f"{ITEMS}/{item['id']}")
    assert _create(client, title="新任务")["id"] != item["id"]


def test_stats_and_clear_completed(client):
    _create(client, title="待办一")
    _create(client, title="待办二", completed=True)
    _create(client, title="待办三", completed=True)

    assert client.get(f"{ITEMS}/stats").json() == {"total": 3, "completed": 2, "active": 1}

    assert client.post(f"{ITEMS}/clear-completed").json() == {
        "total": 1, "completed": 0, "active": 1,
    }
    assert [i["title"] for i in client.get(ITEMS).json()] == ["待办一"]


def test_data_is_persisted_in_the_database(client):
    """绕过 API 直接查库，确认数据真的落到了 Postgres 而非进程内存。"""
    _create(client, title="持久化校验", content="重启后应仍然存在")

    with db_module.engine.connect() as conn:
        rows = conn.execute(
            text("SELECT title, content, completed FROM items")
        ).all()

    assert rows == [("持久化校验", "重启后应仍然存在", False)]
