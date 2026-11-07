import os
import secrets
from contextlib import asynccontextmanager
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
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

# 生产默认不暴露 /docs 与 /openapi.json；本地开发时设 ENABLE_DOCS=1 即可打开。
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
    docs_url="/docs" if _ENABLE_DOCS else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _ENABLE_DOCS else None,
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

