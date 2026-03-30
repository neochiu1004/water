from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Time, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    chat_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Taipei")
    daily_target: Mapped[int] = mapped_column(Integer, default=3000)
    reminder_start: Mapped[time] = mapped_column(Time, default=time(6, 0))
    reminder_end: Mapped[time] = mapped_column(Time, default=time(22, 0))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    state: Mapped["WaterState"] = relationship(back_populates="user", uselist=False, cascade="all, delete-orphan")
    daily_records: Mapped[list["WaterDaily"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    logs: Mapped[list["WaterLog"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class WaterState(Base):
    __tablename__ = "water_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    date_key: Mapped[date] = mapped_column(Date, index=True)
    debt_ml: Mapped[int] = mapped_column("debt_cups", Integer, default=0)
    daily_total: Mapped[int] = mapped_column(Integer, default=0)
    last_drink_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_message_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="idle")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user: Mapped[User] = relationship(back_populates="state")


class WaterDaily(Base):
    __tablename__ = "water_daily"
    __table_args__ = (UniqueConstraint("user_id", "date_key", name="uq_water_daily_user_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    date_key: Mapped[date] = mapped_column(Date, index=True)
    total_ml: Mapped[int] = mapped_column("cups", Integer, default=0)
    target_ml: Mapped[int] = mapped_column("target", Integer, default=3000)
    achieved: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship(back_populates="daily_records")


class WaterLog(Base):
    __tablename__ = "water_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount_ml: Mapped[int] = mapped_column("cups", Integer)
    source: Mapped[str] = mapped_column(String(32), default="telegram")
    logged_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    user: Mapped[User] = relationship(back_populates="logs")
