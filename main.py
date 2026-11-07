import base64
import binascii
import os
import secrets
from contextlib import asynccontextmanager
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import List, Optional
from datetime import datetime
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from db import Item, get_db, init_db


# 共享 API Key。为空时直接启动失败，避免「忘了配就没鉴权」这种静默降级。
# 生产值只存在于服务器的 /opt/task-backend/.env，绝不写进代码或仓库。
API_KEY = os.getenv("API_KEY", "").strip()
if not API_KEY:
    raise RuntimeError(
        "API_KEY 未配置，请在 .env 中设置 API_KEY（可用 openssl rand -hex 32 生成）"
    )

# 文档开关。未开启时 /docs 与 /openapi.json 一律 404，等同于不存在。
# 注意：每次请求都会重新读这个模块级变量（见 require_docs_auth），
# 所以测试可以用 monkeypatch 在运行时打开它来验证 401 / 200 两条路径。
_ENABLE_DOCS = os.getenv("ENABLE_DOCS", "").strip() == "1"


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 启动时确保表结构存在（内部已对数据库未就绪做重试）
    init_db()
    yield


app = FastAPI(
    title="Core API Service",
    description="支持全平台客户端接入的企业级标准 API",
    version="2.0.0",
    lifespan=lifespan,
    # 关闭 FastAPI 自带的三个文档入口：
    # 自带的 /docs 无法加鉴权，所以下面手写两个受保护的路由代替。
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# 收紧跨域来源。原生 App（iOS / Android）不受 CORS 约束，这里只影响浏览器端。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://hahahahaha.party",
        "https://www.hahahahaha.party",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    allow_headers=["Content-Type", "X-API-Key"],
)


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    """校验 X-API-Key 请求头。

    缺失和错误统一返回 401 / Unauthorized，不区分两者，避免泄露任何提示。
    compare_digest 做常量时间比较，防止按响应时间逐字节猜 key。
    """
    if x_api_key is None or not secrets.compare_digest(
        x_api_key.encode("utf-8"), API_KEY.encode("utf-8")
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


# ---------- 受保护的 API 文档 ----------

# 只用来生成登录框上的提示（realm）。用户名本身不校验。
DOCS_REALM = "docs"

# 所有文档入口的 401 都必须带这个头，否则浏览器不会弹登录框。
_DOCS_CHALLENGE = {"WWW-Authenticate": f'Basic realm="{DOCS_REALM}"'}


def _docs_unauthorized() -> HTTPException:
    """文档入口唯一的 401 出口，保证响应头处处一致。"""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
        headers=_DOCS_CHALLENGE,
    )


def _parse_basic_auth(raw: Optional[str]) -> Optional[bytes]:
    """从 Authorization 头里取出密码（bytes）。

    自己解析而不用 fastapi.security.HTTPBasic：内置实现会在解析层
    （scheme 不对、base64 非法、缺少冒号）直接抛 401，那个 401 的
    WWW-Authenticate 是默认的 `Basic`，不带 realm，与依赖里抛出的不一致。
    这里把所有失败都返回 None，由调用方统一转成同一种 401。

    返回 None 表示凭据不可用；返回 bytes 表示解析出的密码（可能是错密码）。
    """
    if not raw:
        return None

    # scheme 与凭据之间用空白分隔，scheme 按 RFC 7235 大小写不敏感。
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "basic":
        return None

    try:
        # validate=True：非 base64 字符（如 `!!!`）直接报错，而不是被忽略
        decoded = base64.b64decode(parts[1].strip(), validate=True)
    except (binascii.Error, ValueError):
        return None

    # 必须形如 user:password；用户名可以为空，冒号之后全部算密码。
    if b":" not in decoded:
        return None

    _username, password = decoded.split(b":", 1)
    return password


def require_docs_auth(authorization: Optional[str] = Header(default=None)) -> None:
    """文档入口的 Basic 鉴权。

    - ENABLE_DOCS 未开启时直接 404，与改造前的行为一致。
    - 用户名不校验，密码必须等于 API_KEY，用 compare_digest 常量时间比较。
    - 只认 Authorization: Basic 头。刻意不支持 ?key= 之类的 query 传参：
      query 会泄漏到浏览器历史、Referer 和 nginx access log。
    - 任何失败分支共用同一个 401（含 WWW-Authenticate: Basic realm="docs"）。
    """
    # 每次请求都读模块级变量，测试可 monkeypatch 打开
    if not _ENABLE_DOCS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    password = _parse_basic_auth(authorization)
    if password is None:
        raise _docs_unauthorized()

    # 按 bytes 比较：既避免非 ASCII 密码触发解码异常，也保持常量时间
    if not secrets.compare_digest(password, API_KEY.encode("utf-8")):
        raise _docs_unauthorized()


@app.get("/docs", include_in_schema=False, dependencies=[Depends(require_docs_auth)])
def docs_ui():
    """Swagger UI。数据源指向下面那个同样受保护的 /openapi.json。"""
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=f"{app.title} - Docs",
    )


@app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(require_docs_auth)])
def openapi_schema():
    """OpenAPI schema。app.openapi() 直接生成字典，与 openapi_url 是否设置无关。"""
    return JSONResponse(app.openapi())


# 所有业务端点统一挂在带鉴权依赖的 router 上，最终路径仍是 /api/v1/...
router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])


# 数据契约模型
class ItemCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    content: Optional[str] = Field(default=None, max_length=2000)
    completed: bool = False

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        # 去掉首尾空白，避免前端误传纯空格的标题
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("title 不能为空")
        return trimmed


class ItemUpdate(BaseModel):
    """局部更新模型：未传入的字段保持原值。"""
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    content: Optional[str] = Field(default=None, max_length=2000)
    completed: Optional[bool] = None

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("title 不能为空")
        return trimmed


class ItemResponse(BaseModel):
    # from_attributes：直接由 ORM 对象序列化
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    content: Optional[str]
    completed: bool
    created_at: datetime
    updated_at: datetime


class StatsResponse(BaseModel):
    total: int
    completed: int
    active: int


def _require_item(db: Session, item_id: int) -> Item:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


def _like_pattern(keyword: str) -> str:
    """转义 LIKE 通配符，避免用户输入的 % / _ 被当成模式匹配。"""
    escaped = (
        keyword.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _stats(db: Session) -> dict:
    total = db.scalar(select(func.count()).select_from(Item)) or 0
    completed = db.scalar(
        select(func.count()).select_from(Item).where(Item.completed.is_(True))
    ) or 0
    return {"total": total, "completed": completed, "active": total - completed}


# 探活接口（企业级部署必备，用于监控系统检查服务是否健康）
# 连带探测数据库，数据库不可用时返回 503，避免流量打到不可用实例
@app.get("/health")
def health_check(db: Session = Depends(get_db)):
    try:
        db.execute(select(1))
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "healthy"}


@router.get("/items", response_model=List[ItemResponse])
def get_items(
    db: Session = Depends(get_db),
    completed: Optional[bool] = Query(default=None, description="按完成状态过滤"),
    q: Optional[str] = Query(default=None, description="按标题/内容模糊搜索"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    stmt = select(Item)

    if completed is not None:
        stmt = stmt.where(Item.completed.is_(completed))

    keyword = (q or "").strip()
    if keyword:
        pattern = _like_pattern(keyword)
        stmt = stmt.where(
            or_(
                Item.title.ilike(pattern, escape="\\"),
                func.coalesce(Item.content, "").ilike(pattern, escape="\\"),
            )
        )

    # 按 id 升序，与改造前的返回顺序保持一致
    stmt = stmt.order_by(Item.id).offset(offset).limit(limit)
    return db.scalars(stmt).all()


@router.get("/items/stats", response_model=StatsResponse)
def get_stats(db: Session = Depends(get_db)):
    return _stats(db)


@router.get("/items/{item_id}", response_model=ItemResponse)
def get_item(item_id: int, db: Session = Depends(get_db)):
    return _require_item(db, item_id)


@router.post("/items", response_model=ItemResponse, status_code=status.HTTP_201_CREATED)
def create_item(item: ItemCreate, db: Session = Depends(get_db)):
    record = Item(title=item.title, content=item.content, completed=item.completed)
    db.add(record)
    db.commit()
    # 回读数据库生成的 id 与时间戳
    db.refresh(record)
    return record


@router.put("/items/{item_id}", response_model=ItemResponse)
def replace_item(item_id: int, payload: ItemCreate, db: Session = Depends(get_db)):
    """整体覆盖：未传入的可选字段会被重置为默认值。"""
    item = _require_item(db, item_id)
    item.title = payload.title
    item.content = payload.content
    item.completed = payload.completed
    db.commit()
    db.refresh(item)
    return item


@router.patch("/items/{item_id}", response_model=ItemResponse)
def update_item(item_id: int, payload: ItemUpdate, db: Session = Depends(get_db)):
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No fields to update")

    item = _require_item(db, item_id)
    for field, value in changes.items():
        setattr(item, field, value)
    db.commit()
    db.refresh(item)
    return item


@router.post("/items/{item_id}/toggle", response_model=ItemResponse)
def toggle_item(item_id: int, db: Session = Depends(get_db)):
    """前端勾选待办的快捷入口，无需关心当前状态。"""
    item = _require_item(db, item_id)
    item.completed = not item.completed
    db.commit()
    db.refresh(item)
    return item


@router.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(item_id: int, db: Session = Depends(get_db)):
    item = _require_item(db, item_id)
    db.delete(item)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/items/clear-completed", response_model=StatsResponse)
def clear_completed(db: Session = Depends(get_db)):
    """一键清除所有已完成待办，返回清理后的统计信息。"""
    db.execute(delete(Item).where(Item.completed.is_(True)))
    db.commit()
    return _stats(db)


# 放在文件末尾：此时 router 上已经挂好了全部业务端点。
# prefix 在 router 上定义，最终路径仍是 /api/v1/...，与改造前逐字一致。
app.include_router(router)

