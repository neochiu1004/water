from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ManualDrinkIn(BaseModel):
    chat_id: str = Field(..., description="Telegram chat id")
    amount_ml: int = Field(..., ge=1, le=5000)


class LogUpdateIn(BaseModel):
    amount_ml: int = Field(..., ge=1, le=5000)


class UserSettingsIn(BaseModel):
    daily_target: int = Field(..., ge=250, le=10000)
    reminder_start_hour: int = Field(..., ge=0, le=23)
    reminder_end_hour: int = Field(..., ge=0, le=23)
    timezone: str = Field(default="Asia/Taipei")
    quick_add_amounts: list[int] = Field(default_factory=lambda: [250, 500, 750], min_length=1, max_length=6)


class UserSettingsOut(UserSettingsIn):
    chat_id: str


class AppConfigOut(BaseModel):
    default_chat_id: Optional[str] = None
    dashboard_url: Optional[str] = None


class SummaryOut(BaseModel):
    today: str
    timezone: str
    daily_total: int
    target: int
    debt_ml: int
    week_ok_days: int
    status_message: str
    time_blocks: list[dict]
    recent_7_days: list[dict]
    recent_logs: list[dict]
