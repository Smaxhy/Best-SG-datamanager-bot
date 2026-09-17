"""Tracks every Discord message the bot has successfully processed."""

from datetime import datetime, timezone
from sqlalchemy import BigInteger, Integer, String, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column
from models.base import Base


class ProcessedLog(Base):
    __tablename__ = "processed_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_message_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    discord_channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    data_manager_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    data_manager_name: Mapped[str] = mapped_column(String(100), nullable=False)
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    lore_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ep_change: Mapped[int] = mapped_column(Integer, default=0)
    tp_change: Mapped[int] = mapped_column(Integer, default=0)
    mp_change: Mapped[int] = mapped_column(Integer, default=0)
    detachment: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="success")  # success | error
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
