from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Union
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import User, WaterDaily, WaterLog, WaterState


TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}" if TELEGRAM_TOKEN else ""
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "").strip()
DASHBOARD_LAN_URL = os.getenv("DASHBOARD_LAN_URL", "").strip()
LEGACY_CUP_TO_ML = 200
LEGACY_CUP_THRESHOLD = 40


def now_in_timezone(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def migrate_legacy_cup_units(db: Session) -> int:
    changed = 0

    for user in db.scalars(select(User)).all():
        if 0 < user.daily_target <= LEGACY_CUP_THRESHOLD:
            user.daily_target *= LEGACY_CUP_TO_ML
            changed += 1

    for state in db.scalars(select(WaterState)).all():
        if 0 < state.debt_ml <= LEGACY_CUP_THRESHOLD:
            state.debt_ml *= LEGACY_CUP_TO_ML
            changed += 1
        if 0 < state.daily_total <= LEGACY_CUP_THRESHOLD:
            state.daily_total *= LEGACY_CUP_TO_ML
            changed += 1

    for daily in db.scalars(select(WaterDaily)).all():
        if 0 < daily.total_ml <= LEGACY_CUP_THRESHOLD:
            daily.total_ml *= LEGACY_CUP_TO_ML
            changed += 1
        if 0 < daily.target_ml <= LEGACY_CUP_THRESHOLD:
            daily.target_ml *= LEGACY_CUP_TO_ML
            changed += 1
        daily.achieved = daily.total_ml >= daily.target_ml

    for log in db.scalars(select(WaterLog)).all():
        if 0 < log.amount_ml <= 5:
            log.amount_ml *= LEGACY_CUP_TO_ML
            changed += 1

    if changed:
        db.flush()
    return changed


def ensure_user_state(db: Session, user: User, local_today: date) -> WaterState:
    if user.state is None:
        user.state = WaterState(
            date_key=local_today,
            debt_ml=0,
            daily_total=0,
            status="idle",
        )
        db.add(user.state)
        db.flush()

    if user.state.date_key != local_today:
        user.state.date_key = local_today
        user.state.debt_ml = 0
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
            total_ml=0,
            target_ml=user.daily_target,
            achieved=False,
        )
        db.add(daily)
        db.flush()
    return daily


def local_day_utc_bounds(tz_name: str, local_day: date) -> tuple[datetime, datetime]:
    tz = ZoneInfo(tz_name)
    start_local = datetime.combine(local_day, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).replace(tzinfo=None),
        end_local.astimezone(timezone.utc).replace(tzinfo=None),
    )


def local_date_for_log(user: User, log: WaterLog) -> date:
    return log.logged_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(user.timezone)).date()


def build_time_blocks(user: User, local_day: date) -> list[dict]:
    tz = ZoneInfo(user.timezone)
    total_slots = max(1, user.reminder_end.hour - user.reminder_start.hour + 1)
    base_slots, slot_remainder = divmod(total_slots, 3)
    base_target, target_remainder = divmod(user.daily_target, 3)
    block_names = ("晨間", "午間", "晚間")

    blocks = []
    current_hour = user.reminder_start.hour
    for index, name in enumerate(block_names):
        slot_count = base_slots + (1 if index < slot_remainder else 0)
        target_ml = base_target + (1 if index < target_remainder else 0)
        start_hour = current_hour
        end_hour = current_hour + slot_count - 1
        start_at = datetime.combine(local_day, time(start_hour, 0), tzinfo=tz)
        end_at = datetime.combine(local_day, time(min(end_hour, 23), 59, 59), tzinfo=tz)
        blocks.append(
            {
                "name": name,
                "index": index,
                "slot_count": slot_count,
                "target_ml": target_ml,
                "start_hour": start_hour,
                "end_hour": end_hour,
                "start_at": start_at,
                "end_at": end_at,
            }
        )
        current_hour = end_hour + 1
    return blocks


def block_step_ml(block: dict) -> int:
    slot_count = max(1, block["slot_count"])
    block_target = block["target_ml"]
    return max(100, round(block_target / slot_count / 50) * 50)


def reminder_step_ml(user: User) -> int:
    local_now = now_in_timezone(user.timezone)
    current_blocks = build_time_blocks(user, local_now.date())
    current_hour = local_now.hour
    for block in current_blocks:
        if block["start_hour"] <= current_hour <= block["end_hour"]:
            return block_step_ml(block)
    return block_step_ml(current_blocks[0])


def logs_for_local_day(db: Session, user: User, local_day: date) -> list[WaterLog]:
    start_utc, end_utc = local_day_utc_bounds(user.timezone, local_day)
    return db.scalars(
        select(WaterLog)
        .where(WaterLog.user_id == user.id, WaterLog.logged_at >= start_utc, WaterLog.logged_at < end_utc)
        .order_by(WaterLog.logged_at.asc())
    ).all()


def summarize_time_blocks(db: Session, user: User, local_day: date, local_now: Optional[datetime] = None) -> list[dict]:
    if local_now is None:
        local_now = now_in_timezone(user.timezone)
    blocks = build_time_blocks(user, local_day)
    logs = logs_for_local_day(db, user, local_day)
    cumulative_target = 0
    summaries = []

    for block in blocks:
        block_total_ml = 0
        for log in logs:
            log_local = log.logged_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(user.timezone))
            if block["start_at"] <= log_local <= block["end_at"]:
                block_total_ml += log.amount_ml

        cumulative_target += block["target_ml"]
        status = "upcoming"
        if local_now > block["end_at"]:
            status = "done"
        elif block["start_at"] <= local_now <= block["end_at"]:
            status = "current"

        summaries.append(
            {
                "name": block["name"],
                "label": f"{block['start_hour']:02d}:00-{block['end_hour']:02d}:59",
                "target_ml": block["target_ml"],
                "amount_ml": block_total_ml,
                "remaining_ml": max(0, block["target_ml"] - block_total_ml),
                "status": status,
                "cumulative_target_ml": cumulative_target,
                "slot_count": block["slot_count"],
                "start_hour": block["start_hour"],
                "end_hour": block["end_hour"],
            }
        )
    return summaries


def current_time_block(blocks: list[dict]) -> dict:
    for block in blocks:
        if block["status"] == "current":
            return block
    for block in blocks:
        if block["status"] == "upcoming":
            return block
    return blocks[-1]


def expected_total_by_now(blocks: list[dict], local_now: datetime) -> int:
    expected = 0
    current_hour = local_now.hour
    for block in blocks:
        if current_hour > block["end_hour"]:
            expected += block["target_ml"]
            continue
        if current_hour < block["start_hour"]:
            break
        elapsed_slots = max(0, current_hour - block["start_hour"])
        if block["slot_count"] <= 1:
            block_expected = 0
        else:
            block_expected = round(block["target_ml"] * elapsed_slots / (block["slot_count"] - 1))
        expected += min(block["target_ml"], block_expected)
        break
    return expected


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
        total_ml = row.total_ml if row else 0
        target_ml = row.target_ml if row else user.daily_target
        achieved = row.achieved if row else False
        if achieved:
            ok_days += 1
        recent.append(
            {
                "date": current.isoformat(),
                "amount_ml": total_ml,
                "target_ml": target_ml,
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


def build_status_message(daily: WaterDaily, blocks: list[dict], week_ok_days: int, fail_streak: int) -> str:
    current_block = current_time_block(blocks)
    if daily.achieved:
        rewards = [
            "獎賞自己一個小休息，今天這條線有守住。",
            "今天任務完成，可以把這天記成補水勝利日。",
            "很穩，今天的水量已經收滿，可以輕鬆維持。",
        ]
        return f"🏆 今天已達標。{rewards[week_ok_days % len(rewards)]}"
    if current_block["status"] == "current" and current_block["remaining_ml"] == 0:
        return f"✨ {current_block['name']}時段已達標，下一段照這個節奏就很好。"
    if fail_streak >= 3:
        return f"🔥 已連續 {fail_streak} 天未達標，這一段先補齊 {current_block['remaining_ml']} ml。"
    if week_ok_days >= 5:
        return f"🏅 本週已達標 {week_ok_days}/7 天，這一段再補 {current_block['remaining_ml']} ml 就很漂亮。"
    return f"再補 {current_block['remaining_ml']} ml，這個時段就能跟上節奏。"


def build_reminder_text(db: Session, user: User, state: WaterState, daily: WaterDaily, local_today: date) -> str:
    local_now = now_in_timezone(user.timezone)
    recent7, week_ok_days = build_recent7(db, user, local_today)
    recent3_marks = ["✅" if item["achieved"] else "❌" for item in recent7[-3:]]
    fail_streak = current_fail_streak(recent7)
    blocks = summarize_time_blocks(db, user, local_today, local_now)
    current_block = current_time_block(blocks)
    step_ml = block_step_ml(current_block)

    level = "💧"
    note = f"目前在 {current_block['name']}時段，目標 {current_block['target_ml']} ml。"
    if state.debt_ml >= step_ml * 4:
        level = "🚨🚨🚨"
        note = f"{current_block['name']}時段明顯落後，建議現在先喝 {min(state.debt_ml, step_ml * 2)} ml。"
    elif state.debt_ml >= step_ml * 2:
        level = "🚨"
        note = f"{current_block['name']}時段有點落後，這次先補 {step_ml} ml 會比較順。"
    elif state.debt_ml > 0:
        level = "⚠️"
        note = f"距離這個時段達標還差 {current_block['remaining_ml']} ml。"

    motivation = build_status_message(daily, blocks, week_ok_days, fail_streak)

    return (
        f"{level} 喝水提醒\n\n"
        f"時段：{current_block['name']} {current_block['label']}\n"
        f"建議這次先補：{step_ml} ml\n"
        f"目前應補水量：{state.debt_ml} ml\n"
        f"今日已喝：{daily.total_ml} / {daily.target_ml} ml\n"
        f"本時段進度：{current_block['amount_ml']} / {current_block['target_ml']} ml\n"
        f"本週達標：{week_ok_days} / 7 天\n"
        f"近三日：{' '.join(recent3_marks)}\n"
        f"{note}\n{motivation}\n\n"
        "請點擊按鈕記錄這次喝了多少 ml"
    )


def reminder_keyboard(user: User) -> dict:
    step_ml = reminder_step_ml(user)
    rows = [
        [
            {"text": f"{step_ml} ml", "callback_data": f"WATER_{step_ml}"},
            {"text": f"{step_ml * 2} ml", "callback_data": f"WATER_{step_ml * 2}"},
            {"text": f"{step_ml * 3} ml", "callback_data": f"WATER_{step_ml * 3}"},
        ],
        [
            {"text": f"{500} ml", "callback_data": "WATER_500"},
            {"text": f"{750} ml", "callback_data": "WATER_750"},
        ],
    ]
    if DASHBOARD_URL:
        rows.append([{"text": "查看喝水儀表板", "url": DASHBOARD_URL}])
    return {"inline_keyboard": rows}


def dashboard_links_for_chat(chat_id: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    seen: set[str] = set()

    for label, base_url in (("LAN", DASHBOARD_LAN_URL), ("Tailscale", DASHBOARD_URL)):
        if not base_url:
            continue
        separator = "&" if "?" in base_url else "?"
        full_url = f"{base_url}{separator}{urlencode({'chat_id': chat_id})}"
        if full_url in seen:
            continue
        seen.add(full_url)
        links.append((label, full_url))

    return links


def dashboard_url_for_chat(chat_id: str) -> str:
    links = dashboard_links_for_chat(chat_id)
    return links[0][1] if links else ""


def persistent_menu_keyboard(chat_id: str) -> dict:
    rows = [
        [{"text": "+250"}, {"text": "+500"}, {"text": "+750"}],
        [{"text": "250ml"}, {"text": "500ml"}, {"text": "/status"}],
    ]

    rows.append([{"text": "/dashboard"}, {"text": "/help"}])

    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "點底下按鈕快速補水，或輸入 /drink 300",
    }


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
            "reply_markup": reminder_keyboard(user),
        },
    )
    message = result.get("result") or {}
    return str(message.get("message_id")) if message.get("message_id") else None


async def send_text(chat_id: str, text: str) -> None:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": persistent_menu_keyboard(chat_id),
    }
    await telegram_api("sendMessage", payload)


def record_drink(db: Session, user: User, amount_ml: int, source: str = "telegram") -> tuple[WaterState, WaterDaily]:
    local_now = now_in_timezone(user.timezone)
    local_today = local_now.date()
    state = ensure_user_state(db, user, local_today)
    daily = ensure_daily_record(db, user, local_today)

    debt_reduction = min(amount_ml, max(0, state.debt_ml))
    state.debt_ml = max(0, state.debt_ml - debt_reduction)
    state.daily_total = daily.total_ml + amount_ml
    state.last_drink_at = local_now.astimezone(timezone.utc).replace(tzinfo=None)
    state.status = "done" if state.daily_total >= daily.target_ml else ("drank" if state.debt_ml == 0 else "waiting")

    daily.total_ml += amount_ml
    daily.target_ml = user.daily_target
    daily.achieved = daily.total_ml >= daily.target_ml

    db.add(WaterLog(user_id=user.id, amount_ml=amount_ml, source=source, logged_at=datetime.utcnow()))
    db.flush()
    return state, daily


def recalculate_user_day(db: Session, user: User, local_day: date) -> tuple[WaterState, WaterDaily]:
    state = ensure_user_state(db, user, local_day)
    daily = ensure_daily_record(db, user, local_day)
    logs = logs_for_local_day(db, user, local_day)
    total_ml = sum(log.amount_ml for log in logs)

    daily.total_ml = total_ml
    daily.target_ml = user.daily_target
    daily.achieved = daily.total_ml >= daily.target_ml

    state.date_key = local_day
    state.daily_total = total_ml
    state.last_drink_at = logs[-1].logged_at if logs else None

    local_now = now_in_timezone(user.timezone)
    if local_day == local_now.date():
        blocks = summarize_time_blocks(db, user, local_day, local_now)
        if daily.achieved:
            state.debt_ml = 0
            state.status = "done"
        else:
            state.debt_ml = max(0, expected_total_by_now(blocks, local_now) - daily.total_ml)
            state.status = "drank" if state.debt_ml == 0 else "waiting"
    else:
        state.debt_ml = 0
        state.status = "done" if daily.achieved else "idle"

    db.flush()
    return state, daily


def update_log_amount(db: Session, user: User, log: WaterLog, amount_ml: int) -> tuple[WaterState, WaterDaily]:
    log.amount_ml = amount_ml
    db.flush()
    return recalculate_user_day(db, user, local_date_for_log(user, log))


def delete_log_entry(db: Session, user: User, log: WaterLog) -> tuple[WaterState, WaterDaily]:
    local_day = local_date_for_log(user, log)
    db.delete(log)
    db.flush()
    return recalculate_user_day(db, user, local_day)


def summary_for_user(db: Session, user: User) -> dict:
    local_now = now_in_timezone(user.timezone)
    local_today = local_now.date()
    state = ensure_user_state(db, user, local_today)
    daily = ensure_daily_record(db, user, local_today)
    recent7, week_ok_days = build_recent7(db, user, local_today)
    blocks = summarize_time_blocks(db, user, local_today, local_now)
    fail_streak = current_fail_streak(recent7)
    if daily.achieved:
        state.debt_ml = 0
        state.status = "done"
    else:
        state.debt_ml = max(0, expected_total_by_now(blocks, local_now) - daily.total_ml)
        state.status = "drank" if state.debt_ml == 0 else "waiting"
    recent_logs = db.scalars(
        select(WaterLog).where(WaterLog.user_id == user.id).order_by(WaterLog.logged_at.desc()).limit(10)
    ).all()
    return {
        "today": local_today.isoformat(),
        "daily_total": daily.total_ml,
        "target": daily.target_ml,
        "debt_ml": state.debt_ml,
        "week_ok_days": week_ok_days,
        "status_message": build_status_message(daily, blocks, week_ok_days, fail_streak),
        "time_blocks": blocks,
        "recent_7_days": recent7,
        "recent_logs": [
            {
                "id": row.id,
                "amount_ml": row.amount_ml,
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
            blocks = summarize_time_blocks(db, user, local_today, local_now)

            if daily.achieved:
                state.debt_ml = 0
                state.status = "done"
                continue

            state.debt_ml = max(0, expected_total_by_now(blocks, local_now) - daily.total_ml)
            state.status = "waiting"

            await delete_message(user.chat_id, state.last_message_id)
            reminder_text = build_reminder_text(db, user, state, daily, local_today)
            state.last_message_id = await send_reminder(user, reminder_text)
            sent += 1
        except Exception as exc:
            print(f"run_reminder_cycle skipped user={user.chat_id} error={exc}")
    return sent
