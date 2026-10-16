from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional

app = FastAPI(
    title="Core API Service",
    description="支持全平台客户端接入的企业级标准 API",
    version="1.0.0"
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
    title: str
    content: Optional[str] = None

class ItemResponse(BaseModel):
    id: int
    title: str
    content: Optional[str]

# 模拟内存存储（后续无缝替换为真实数据库）
items_db = []

# 探活接口（企业级部署必备，用于监控系统检查服务是否健康）
@app.get("/health")
def health_check():
    return {"status": "healthy"}

@app.get("/api/v1/items", response_model=List[ItemResponse])
def get_items():
    return items_db

@app.post("/api/v1/items", response_model=ItemResponse)
def create_item(item: ItemCreate):
    new_record = {
        "id": len(items_db) + 1,
        "title": item.title,
        "content": item.content
    }
    items_db.append(new_record)
    return new_record
