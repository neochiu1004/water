from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import User, WaterDaily, WaterLog, WaterState


TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}" if TELEGRAM_TOKEN else ""
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "").strip()


def now_in_timezone(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def ensure_user_state(db: Session, user: User, local_today: date) -> WaterState:
    if user.state is None:
        user.state = WaterState(
            date_key=local_today,
            debt_cups=0,
            daily_total=0,
            status="idle",
        )
        db.add(user.state)
        db.flush()

    if user.state.date_key != local_today:
        user.state.date_key = local_today
        user.state.debt_cups = 0
        user.state.daily_total = 0
        user.state.status = "idle"
        user.state.last_message_id = None
    return user.state


def ensure_daily_record(db: Session, user: User, day: date) -> WaterDaily:
    stmt = select(WaterDaily).where(WaterDaily.user_id == user.id, WaterDaily.date_key == day)
    daily = db.scalar(stmt)
    if daily is None:
        daily = WaterDaily(
            user_id=user.id,
            date_key=day,
            cups=0,
            target=user.daily_target,
            achieved=False,
        )
        db.add(daily)
        db.flush()
    return daily


def build_recent7(db: Session, user: User, today: date) -> tuple[list[dict], int]:
    start = today - timedelta(days=6)
    rows = db.scalars(
        select(WaterDaily).where(
            WaterDaily.user_id == user.id,
            WaterDaily.date_key >= start,
            WaterDaily.date_key <= today,
        )
    ).all()
    row_map = {row.date_key.isoformat(): row for row in rows}
    recent = []
    ok_days = 0
    for offset in range(7):
        current = start + timedelta(days=offset)
        row = row_map.get(current.isoformat())
        cups = row.cups if row else 0
        target = row.target if row else user.daily_target
        achieved = row.achieved if row else False
        if achieved:
            ok_days += 1
        recent.append(
            {
                "date": current.isoformat(),
                "cups": cups,
                "target": target,
                "achieved": achieved,
            }
        )
    return recent, ok_days


def current_fail_streak(recent7: list[dict]) -> int:
    streak = 0
    for item in reversed(recent7):
        if item["achieved"]:
            break
        streak += 1
    return streak


def build_reminder_text(db: Session, user: User, state: WaterState, daily: WaterDaily, local_today: date) -> str:
    recent7, week_ok_days = build_recent7(db, user, local_today)
    recent3_marks = ["✅" if item["achieved"] else "❌" for item in recent7[-3:]]
    fail_streak = current_fail_streak(recent7)

    level = "💧"
    note = "記得補水一下"
    if state.debt_cups > 5:
        level = "🚨🚨🚨"
        note = "強制提醒：已超過 5 杯，請立刻喝水！"
    elif state.debt_cups >= 5:
        level = "🚨"
        note = "已累積很久沒喝水，建議現在先補足"
    elif state.debt_cups >= 3:
        level = "⚠️"
        note = "有點久沒喝水了"

    if daily.achieved:
        motivation = "🏆 今天已達標，繼續保持。"
    elif fail_streak >= 3:
        motivation = f"🔥 已連續 {fail_streak} 天未達標，今天至少先補到 {daily.target} 杯。"
    elif week_ok_days >= 5:
        motivation = f"🏅 本週已達標 {week_ok_days}/7 天，再撐一下就很漂亮。"
    else:
        motivation = f"再喝 {max(0, daily.target - daily.cups)} 杯，今天就能達標。"

    return (
        f"{level} 喝水提醒\n\n"
        f"目前應補杯數：{state.debt_cups}\n"
        f"今日已喝：{daily.cups} / {daily.target} 杯\n"
        f"本週達標：{week_ok_days} / 7 天\n"
        f"近三日：{' '.join(recent3_marks)}\n"
        f"{note}\n{motivation}\n\n"
        "請點擊按鈕記錄這次喝了幾杯水"
    )


def reminder_keyboard() -> dict:
    rows = [
        [
            {"text": "喝水*1", "callback_data": "WATER_1"},
            {"text": "喝水*2", "callback_data": "WATER_2"},
            {"text": "喝水*3", "callback_data": "WATER_3"},
        ],
        [
            {"text": "喝水*4", "callback_data": "WATER_4"},
            {"text": "喝水*5", "callback_data": "WATER_5"},
        ],
    ]
    if DASHBOARD_URL:
        rows.append([{"text": "查看喝水儀表板", "url": DASHBOARD_URL}])
    return {"inline_keyboard": rows}


async def telegram_api(method: str, payload: dict) -> dict:
    if not TELEGRAM_API_BASE:
        return {}
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(f"{TELEGRAM_API_BASE}/{method}", json=payload)
        if response.is_error:
            raise RuntimeError(
                f"Telegram API {method} failed: status={response.status_code} body={response.text} payload={payload}"
            )
        return response.json()


async def delete_message(chat_id: str, message_id: Optional[Union[str, int]]) -> None:
    if not message_id:
        return
    try:
        await telegram_api("deleteMessage", {"chat_id": chat_id, "message_id": int(message_id)})
    except Exception:
        return


async def send_reminder(user: User, text: str) -> Optional[str]:
    result = await telegram_api(
        "sendMessage",
        {
            "chat_id": user.chat_id,
            "text": text,
            "reply_markup": reminder_keyboard(),
        },
    )
    message = result.get("result") or {}
    return str(message.get("message_id")) if message.get("message_id") else None


async def send_text(chat_id: str, text: str) -> None:
    await telegram_api("sendMessage", {"chat_id": chat_id, "text": text})


def record_drink(db: Session, user: User, cups: int, source: str = "telegram") -> tuple[WaterState, WaterDaily]:
    local_now = now_in_timezone(user.timezone)
    local_today = local_now.date()
    state = ensure_user_state(db, user, local_today)
    daily = ensure_daily_record(db, user, local_today)

    debt_reduction = min(cups, max(0, state.debt_cups))
    state.debt_cups = max(0, state.debt_cups - debt_reduction)
    state.daily_total = daily.cups + cups
    state.last_drink_at = local_now.astimezone(timezone.utc).replace(tzinfo=None)
    state.status = "done" if state.daily_total >= daily.target else ("drank" if state.debt_cups == 0 else "waiting")

    daily.cups += cups
    daily.target = user.daily_target
    daily.achieved = daily.cups >= daily.target

    db.add(WaterLog(user_id=user.id, cups=cups, source=source, logged_at=datetime.utcnow()))
    db.flush()
    return state, daily


def summary_for_user(db: Session, user: User) -> dict:
    local_today = now_in_timezone(user.timezone).date()
    state = ensure_user_state(db, user, local_today)
    daily = ensure_daily_record(db, user, local_today)
    recent7, week_ok_days = build_recent7(db, user, local_today)
    recent_logs = db.scalars(
        select(WaterLog).where(WaterLog.user_id == user.id).order_by(WaterLog.logged_at.desc()).limit(10)
    ).all()
    return {
        "today": local_today.isoformat(),
        "daily_total": daily.cups,
        "target": daily.target,
        "debt_cups": state.debt_cups,
        "week_ok_days": week_ok_days,
        "recent_7_days": recent7,
        "recent_logs": [
            {
                "cups": row.cups,
                "source": row.source,
                "logged_at": row.logged_at.isoformat(),
            }
            for row in recent_logs
        ],
    }


async def run_reminder_cycle(db: Session) -> int:
    users = db.scalars(select(User).where(User.active.is_(True))).all()
    sent = 0
    for user in users:
        try:
            local_now = now_in_timezone(user.timezone)
            if not (user.reminder_start <= local_now.time().replace(second=0, microsecond=0) <= user.reminder_end):
                continue

            local_today = local_now.date()
            state = ensure_user_state(db, user, local_today)
            daily = ensure_daily_record(db, user, local_today)

            if daily.achieved:
                state.debt_cups = 0
                state.status = "done"
                continue

            if local_now.hour != user.reminder_start.hour or local_now.minute != 0:
                state.debt_cups += 1
            state.status = "waiting"

            await delete_message(user.chat_id, state.last_message_id)
            reminder_text = build_reminder_text(db, user, state, daily, local_today)
            state.last_message_id = await send_reminder(user, reminder_text)
            sent += 1
        except Exception as exc:
            print(f"run_reminder_cycle skipped user={user.chat_id} error={exc}")
    return sent
