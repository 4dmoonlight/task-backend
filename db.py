"""数据库连接与 ORM 模型定义。"""

import os
import time
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    func,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.sql import expression


def _database_url() -> str:
    """读取连接串。缺失时直接报错，避免静默退化成临时存储导致数据丢失。"""
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL 未配置，请在 .env 中设置，例如："
            "postgresql+psycopg2://taskuser:<password>@db:5432/taskdb"
        )
    return url


# pool_pre_ping：数据库重启后自动重建失效连接，避免接口偶发 500
engine = create_engine(_database_url(), pool_pre_ping=True, future=True)

# expire_on_commit=False：提交后仍可直接序列化对象，不必再回查一次数据库
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    completed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=expression.false(),
        index=True,
    )
    # 时间戳交给数据库生成，多实例部署时不受各容器时钟漂移影响
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


def init_db(retries: int = 15, delay: float = 2.0) -> None:
    """建表。容器启动顺序不确定，这里对数据库未就绪做重试。"""
    last_error: Optional[Exception] = None
    for _ in range(retries):
        try:
            Base.metadata.create_all(bind=engine)
            return
        except OperationalError as exc:
            last_error = exc
            time.sleep(delay)
    raise RuntimeError(f"数据库连接失败，已重试 {retries} 次") from last_error


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
