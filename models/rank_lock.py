"""Tracks rank-locked players."""

from datetime import datetime, timezone
from sqlalchemy import BigInteger, Integer, String, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from models.base import Base


class RankLock(Base):
    __tablename__ = "rank_locks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    locked_by_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    locked_by_name: Mapped[str] = mapped_column(String(100), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    locked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
