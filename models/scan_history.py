"""Audit trail for every /scan invocation."""

from datetime import datetime, timezone
from sqlalchemy import BigInteger, Integer, String, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from models.base import Base


class ScanHistory(Base):
    __tablename__ = "scan_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    data_manager_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    data_manager_name: Mapped[str] = mapped_column(String(100), nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_name: Mapped[str] = mapped_column(String(100), nullable=False)
    scan_started: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scan_finished: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    messages_checked: Mapped[int] = mapped_column(Integer, default=0)
    logs_found: Mapped[int] = mapped_column(Integer, default=0)
    logs_processed: Mapped[int] = mapped_column(Integer, default=0)
    already_processed: Mapped[int] = mapped_column(Integer, default=0)
    players_updated: Mapped[int] = mapped_column(Integer, default=0)
    players_not_found: Mapped[int] = mapped_column(Integer, default=0)
    invalid_logs: Mapped[int] = mapped_column(Integer, default=0)
    lore_mismatches: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    ep_total: Mapped[int] = mapped_column(Integer, default=0)
    tp_total: Mapped[int] = mapped_column(Integer, default=0)
    mp_total: Mapped[int] = mapped_column(Integer, default=0)
