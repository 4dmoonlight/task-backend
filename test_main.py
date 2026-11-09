"""接口集成测试：在真实 Postgres 上验证读写、过滤与持久化。"""

import base64
import os

# main.py 在 import 时就会读取 API_KEY，缺失直接抛 RuntimeError，所以必须在
# import main 之前设好。这里用固定的测试值，绝不使用生产密钥。
#
# 用直接赋值而不是 setdefault：如果开发者本地 shell 里恰好导出了生产 API_KEY，
# setdefault 会保留它，而 client fixture 发的是下面的测试 key，结果全部 401。
# 测试必须不依赖环境里的残留变量。
TEST_API_KEY = "test-api-key"
os.environ["API_KEY"] = TEST_API_KEY
# 同理：确保「文档默认关闭」这条用例不受本机 ENABLE_DOCS 影响。
os.environ.pop("ENABLE_DOCS", None)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, text

import db as db_module
import main as main_module
from db import Item
from main import app

ITEMS = "/api/v1/items"
AUTH = {"X-API-Key": TEST_API_KEY}

IS_POSTGRES = db_module.engine.dialect.name == "postgresql"


@pytest.fixture()
def client():
    # 进入上下文会触发 lifespan，由 init_db 建表。
    # 通过默认请求头带上测试 key，现有用例无需逐个改动。
    with TestClient(app, headers=AUTH) as test_client:
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


# ---------- 鉴权 ----------
# client fixture 的默认请求头里带了测试 key，而 httpx 无法用「传 None」来去掉
# 一个默认 header，所以负向用例改用不带默认头的裸 client。


def test_missing_api_key_is_rejected(client):
    with TestClient(app) as anon:
        assert anon.get(ITEMS).status_code == 401


def test_wrong_api_key_is_rejected(client):
    with TestClient(app) as anon:
        response = anon.get(ITEMS, headers={"X-API-Key": "not-the-right-key"})
    assert response.status_code == 401


def test_correct_api_key_is_accepted(client):
    response = client.get(ITEMS)
    assert response.status_code == 200
    assert response.json() == []


def test_health_stays_public_without_api_key(client):
    with TestClient(app) as anon:
        response = anon.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_docs_are_disabled_by_default(client):
    # 测试环境没有设置 ENABLE_DOCS，等价于生产默认配置：
    # /docs、/redoc、/openapi.json 三个文档入口全部关闭
    with TestClient(app) as anon:
        assert anon.get("/docs").status_code == 404
        assert anon.get("/redoc").status_code == 404
        assert anon.get("/openapi.json").status_code == 404


# ---------- 受保护的文档入口 ----------
# require_docs_auth 每次请求都会重新读模块级 _ENABLE_DOCS，
# 所以可以用 monkeypatch 在运行时打开开关，验证 401 与 200 两条路径。

# 所有文档 401 的响应头必须完全一致：带 realm 浏览器才会弹登录框。
# 注意是精确相等，不是「包含 Basic」——之前内置 HTTPBasic 在解析层失败时
# 给出的是不带 realm 的 `Basic`，只断言包含就会漏掉这个不一致。
EXPECTED_CHALLENGE = 'Basic realm="docs"'

DOCS_PATHS = ["/docs", "/openapi.json"]


@pytest.fixture()
def docs_enabled(monkeypatch):
    monkeypatch.setattr(main_module, "_ENABLE_DOCS", True)


def _basic(pair: str) -> str:
    """把 `user:password` 编码成 Authorization 头的值。"""
    return "Basic " + base64.b64encode(pair.encode("utf-8")).decode("ascii")


def _assert_challenged(response):
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == EXPECTED_CHALLENGE


@pytest.mark.parametrize("path", DOCS_PATHS)
def test_docs_reject_missing_credentials(docs_enabled, path):
    with TestClient(app) as anon:
        response = anon.get(path)
    _assert_challenged(response)


@pytest.mark.parametrize("path", DOCS_PATHS)
def test_docs_reject_wrong_password(docs_enabled, path):
    with TestClient(app) as anon:
        response = anon.get(path, auth=("docs", "wrong-password"))
    _assert_challenged(response)


@pytest.mark.parametrize("path", DOCS_PATHS)
def test_docs_reject_non_basic_scheme(docs_enabled, path):
    """Bearer 等其他方案必须走同一条 401 出口，头与其它失败一致。"""
    with TestClient(app) as anon:
        response = anon.get(path, headers={"Authorization": "Bearer abc123"})
    _assert_challenged(response)


@pytest.mark.parametrize("path", DOCS_PATHS)
def test_docs_reject_invalid_base64(docs_enabled, path):
    """Basic 后面不是合法 base64。内置 HTTPBasic 在解析层抛的 401 不带 realm。"""
    with TestClient(app) as anon:
        response = anon.get(path, headers={"Authorization": "Basic !!!"})
    _assert_challenged(response)


@pytest.mark.parametrize("path", DOCS_PATHS)
def test_docs_reject_when_base64_has_no_colon(docs_enabled, path):
    """解出来的内容里没有冒号，无法拆成 user:password。"""
    with TestClient(app) as anon:
        response = anon.get(
            path,
            headers={"Authorization": _basic("no-colon-here")},
        )
    _assert_challenged(response)


@pytest.mark.parametrize("path", DOCS_PATHS)
def test_docs_reject_non_ascii_password(docs_enabled, path):
    """非 ASCII 密码：不能因为解码异常变成 500，必须仍是标准 401。"""
    with TestClient(app) as anon:
        response = anon.get(
            path,
            headers={"Authorization": _basic("docs:这不是密钥")},
        )
    _assert_challenged(response)


def test_docs_accept_correct_password(docs_enabled):
    with TestClient(app) as anon:
        response = anon.get("/docs", auth=("docs", TEST_API_KEY))
    assert response.status_code == 200
    # Swagger UI 的 HTML，且数据源指向同一个受保护的 schema
    assert "swagger" in response.text.lower()
    assert "/openapi.json" in response.text


def test_docs_accept_empty_username(docs_enabled):
    """用户名不校验，空用户名也应通过。"""
    with TestClient(app) as anon:
        response = anon.get("/docs", headers={"Authorization": _basic(":" + TEST_API_KEY)})
    assert response.status_code == 200


def test_docs_username_is_not_checked(docs_enabled):
    """用户名不校验，只校验密码——固定写死 docs 只是为了让登录框有提示。"""
    with TestClient(app) as anon:
        response = anon.get("/openapi.json", auth=("any-name", TEST_API_KEY))
    assert response.status_code == 200


def test_openapi_json_accepts_correct_password(docs_enabled):
    with TestClient(app) as anon:
        response = anon.get("/openapi.json", auth=("docs", TEST_API_KEY))
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "Core API Service"
    # 业务端点仍然在 schema 里，路径与改造前一致
    assert "/api/v1/items" in schema["paths"]
    # 两个文档端点自身都不进 schema
    assert "/docs" not in schema["paths"]
    assert "/openapi.json" not in schema["paths"]


def test_docs_query_key_is_not_accepted(docs_enabled):
    """禁止把密钥放 URL query：会泄漏到浏览器历史、Referer 与 nginx access log。"""
    with TestClient(app) as anon:
        response = anon.get("/docs", params={"key": TEST_API_KEY})
    _assert_challenged(response)


# ---------- 自托管的 Swagger UI 静态资源 ----------

DOCS_ASSETS = [
    ("/docs/swagger-ui-bundle.js", "application/javascript"),
    ("/docs/swagger-ui.css", "text/css"),
]


@pytest.mark.parametrize("path", [p for p, _ in DOCS_ASSETS])
def test_docs_assets_hidden_when_docs_disabled(client, path):
    with TestClient(app) as anon:
        assert anon.get(path).status_code == 404
        # 即使带了正确凭据，未开启时也是 404：开关优先于鉴权
        assert anon.get(path, auth=("docs", TEST_API_KEY)).status_code == 404


@pytest.mark.parametrize("path", [p for p, _ in DOCS_ASSETS])
def test_docs_assets_reject_missing_credentials(docs_enabled, path):
    with TestClient(app) as anon:
        response = anon.get(path)
    _assert_challenged(response)


@pytest.mark.parametrize("path", [p for p, _ in DOCS_ASSETS])
def test_docs_assets_reject_wrong_password(docs_enabled, path):
    with TestClient(app) as anon:
        response = anon.get(path, auth=("docs", "wrong-password"))
    _assert_challenged(response)


@pytest.mark.parametrize("path,media_type", DOCS_ASSETS)
def test_docs_assets_served_with_correct_type(docs_enabled, path, media_type):
    with TestClient(app) as anon:
        response = anon.get(path, auth=("docs", TEST_API_KEY))
    assert response.status_code == 200
    # Starlette 会给 text/* 追加 "; charset=utf-8"，所以按前缀比
    assert response.headers["content-type"].split(";")[0].strip() == media_type
    assert len(response.content) > 0


def test_docs_page_has_no_external_resources(docs_enabled):
    with TestClient(app) as anon:
        response = anon.get("/docs", auth=("docs", TEST_API_KEY))
    assert response.status_code == 200
    html = response.text
    assert "cdn.jsdelivr.net" not in html
    assert "fastapi.tiangolo.com" not in html
    # 更彻底的一条：页面里不应出现任何 https:// 外链
    #（上面两个域名都是 https 形式，这条能把同类外链一并锁死）。
    # 刻意不断言 http://：favicon 的 data URI 里含 SVG 命名空间
    # http://www.w3.org/2000/svg，那是 XML 语法的一部分，不是网络请求，
    # 断言 http:// 会误伤。
    assert "https://" not in html
    assert "/docs/swagger-ui-bundle.js" in html
    assert "/docs/swagger-ui.css" in html
