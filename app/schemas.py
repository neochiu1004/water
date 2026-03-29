from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ManualDrinkIn(BaseModel):
    chat_id: str = Field(..., description="Telegram chat id")
    cups: int = Field(..., ge=1, le=20)


class UserSettingsIn(BaseModel):
    daily_target: int = Field(..., ge=1, le=40)
    reminder_start_hour: int = Field(..., ge=0, le=23)
    reminder_end_hour: int = Field(..., ge=0, le=23)
    timezone: str = Field(default="Asia/Taipei")


class UserSettingsOut(UserSettingsIn):
    chat_id: str


class AppConfigOut(BaseModel):
    default_chat_id: Optional[str] = None
    dashboard_url: Optional[str] = None


class SummaryOut(BaseModel):
    today: str
    daily_total: int
    target: int
    debt_cups: int
    week_ok_days: int
    recent_7_days: list[dict]
    recent_logs: list[dict]
