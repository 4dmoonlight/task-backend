from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from typing import List, Optional
from datetime import datetime, timezone
from threading import Lock

app = FastAPI(
    title="Core API Service",
    description="支持全平台客户端接入的企业级标准 API",
    version="1.1.0"
)

# 开启跨域访问 (CORS)，允许 Web 前端及多端设备请求
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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


# 模拟内存存储（后续无缝替换为真实数据库）
items_db = []

# 自增主键独立维护，避免删除后出现 id 冲突
_next_id = 1

# 同步接口由线程池并发执行，写操作统一加锁
_lock = Lock()


def _now():
    return datetime.now(timezone.utc)


def _find_item(item_id: int):
    for item in items_db:
        if item["id"] == item_id:
            return item
    return None


def _require_item(item_id: int):
    item = _find_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


# 探活接口（企业级部署必备，用于监控系统检查服务是否健康）
@app.get("/health")
def health_check():
    return {"status": "healthy"}


@app.get("/api/v1/items", response_model=List[ItemResponse])
def get_items(
    completed: Optional[bool] = Query(default=None, description="按完成状态过滤"),
    q: Optional[str] = Query(default=None, description="按标题/内容模糊搜索"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    results = items_db

    if completed is not None:
        results = [item for item in results if item["completed"] == completed]

    if q:
        keyword = q.strip().lower()
        results = [
            item for item in results
            if keyword in item["title"].lower()
            or keyword in (item["content"] or "").lower()
        ]

    return results[offset:offset + limit]


@app.get("/api/v1/items/stats", response_model=StatsResponse)
def get_stats():
    completed = sum(1 for item in items_db if item["completed"])
    total = len(items_db)
    return {"total": total, "completed": completed, "active": total - completed}


@app.get("/api/v1/items/{item_id}", response_model=ItemResponse)
def get_item(item_id: int):
    return _require_item(item_id)


@app.post("/api/v1/items", response_model=ItemResponse, status_code=status.HTTP_201_CREATED)
def create_item(item: ItemCreate):
    global _next_id
    timestamp = _now()
    with _lock:
        new_record = {
            "id": _next_id,
            "title": item.title,
            "content": item.content,
            "completed": item.completed,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        _next_id += 1
        items_db.append(new_record)
    return new_record


@app.put("/api/v1/items/{item_id}", response_model=ItemResponse)
def replace_item(item_id: int, payload: ItemCreate):
    """整体覆盖：未传入的可选字段会被重置为默认值。"""
    with _lock:
        item = _require_item(item_id)
        item["title"] = payload.title
        item["content"] = payload.content
        item["completed"] = payload.completed
        item["updated_at"] = _now()
    return item


@app.patch("/api/v1/items/{item_id}", response_model=ItemResponse)
def update_item(item_id: int, payload: ItemUpdate):
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No fields to update")

    with _lock:
        item = _require_item(item_id)
        item.update(changes)
        item["updated_at"] = _now()
    return item


@app.post("/api/v1/items/{item_id}/toggle", response_model=ItemResponse)
def toggle_item(item_id: int):
    """前端勾选待办的快捷入口，无需关心当前状态。"""
    with _lock:
        item = _require_item(item_id)
        item["completed"] = not item["completed"]
        item["updated_at"] = _now()
    return item


@app.delete("/api/v1/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(item_id: int):
    with _lock:
        item = _require_item(item_id)
        items_db.remove(item)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/v1/items/clear-completed", response_model=StatsResponse)
def clear_completed():
    """一键清除所有已完成待办，返回清理后的统计信息。"""
    with _lock:
        items_db[:] = [item for item in items_db if not item["completed"]]
        total = len(items_db)
    return {"total": total, "completed": 0, "active": total}
