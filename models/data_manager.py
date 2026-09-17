"""Weekly Data Manager statistics — one row per DM per ISO week."""

from datetime import datetime, timezone
from sqlalchemy import BigInteger, Integer, String, DateTime, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from models.base import Base


class DataManagerStats(Base):
    __tablename__ = "data_manager_stats"
    __table_args__ = (
        UniqueConstraint("discord_user_id", "iso_year", "iso_week", name="uq_dm_week"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    discord_username: Mapped[str] = mapped_column(String(100), nullable=False)
    iso_year: Mapped[int] = mapped_column(Integer, nullable=False)
    iso_week: Mapped[int] = mapped_column(Integer, nullable=False)
    logs_processed: Mapped[int] = mapped_column(Integer, default=0)
    scans_completed: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
