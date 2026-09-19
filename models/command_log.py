"""Tracks every command executed."""

from datetime import datetime, timezone
from sqlalchemy import BigInteger, Integer, String, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column
from models.base import Base


class CommandLog(Base):
    __tablename__ = "command_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discord_username: Mapped[str] = mapped_column(String(100), nullable=False)
    command: Mapped[str] = mapped_column(String(50), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel_name: Mapped[str] = mapped_column(String(100), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
