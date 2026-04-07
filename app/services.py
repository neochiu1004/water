from __future__ import annotations

import io
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Union
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import User, WaterDaily, WaterLog, WaterState

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - optional dependency on low-resource hosts
    Image = None
    ImageDraw = None
    ImageFont = None


TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}" if TELEGRAM_TOKEN else ""
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "").strip()
DASHBOARD_LAN_URL = os.getenv("DASHBOARD_LAN_URL", "").strip()
LEGACY_CUP_TO_ML = 200
LEGACY_CUP_THRESHOLD = 40
SUMMARY_IMAGE_WIDTH = 1080
SUMMARY_IMAGE_HEIGHT = 900
DEFAULT_QUICK_ADD_AMOUNTS = [250, 500, 750]
FIXED_DRINK_PLAN = [
    {"key": "wake", "name": "起床", "label": "起床", "amount_ml": 300},
    {"key": "morning", "name": "早上10點", "label": "10:00", "amount_ml": 500},
    {"key": "afternoon", "name": "下午3~4點", "label": "15:00-16:00", "amount_ml": 500},
    {"key": "evening", "name": "晚上7~8點", "label": "19:00-20:00", "amount_ml": 300},
    {"key": "bedtime", "name": "睡前", "label": "22:00", "amount_ml": 100},
]


def now_in_timezone(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def load_font(size: int, bold: bool = False):
    if ImageFont is None:
        raise RuntimeError("Pillow is not installed")
    candidates = [
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


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


def parse_quick_add_amounts(raw: Optional[str]) -> list[int]:
    values: list[int] = []
    for chunk in (raw or "").split(","):
        item = chunk.strip()
        if not item:
            continue
        try:
            amount = int(item)
        except ValueError:
            continue
        if 1 <= amount <= 5000 and amount not in values:
            values.append(amount)
    return values[:6] or DEFAULT_QUICK_ADD_AMOUNTS.copy()


def serialize_quick_add_amounts(values: list[int]) -> str:
    cleaned: list[int] = []
    for amount in values:
        if 1 <= amount <= 5000 and amount not in cleaned:
            cleaned.append(amount)
    return ",".join(str(amount) for amount in (cleaned[:6] or DEFAULT_QUICK_ADD_AMOUNTS))


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


def build_fixed_plan_summary(daily_total: int) -> dict:
    cumulative_target = 0
    checkpoints: list[dict] = []
    for item in FIXED_DRINK_PLAN:
        cumulative_target += item["amount_ml"]
        checkpoints.append(
            {
                **item,
                "cumulative_target_ml": cumulative_target,
                "completed": daily_total >= cumulative_target,
            }
        )

    completed_checkpoint = next((item for item in reversed(checkpoints) if item["completed"]), None)
    next_checkpoint = next((item for item in checkpoints if not item["completed"]), None)
    is_completed = next_checkpoint is None
    remaining_ml = 0 if is_completed else max(0, next_checkpoint["cumulative_target_ml"] - daily_total)
    achieved_ml = min(daily_total, checkpoints[-1]["cumulative_target_ml"])

    if is_completed:
        summary_text = "固定計畫：今天已完成或超前"
    elif completed_checkpoint is None:
        summary_text = (
            f"固定計畫：下一個 {next_checkpoint['name']}，"
            f"目標累積 {next_checkpoint['cumulative_target_ml']} ml，尚差 {remaining_ml} ml"
        )
    else:
        summary_text = (
            f"固定計畫：已完成 {completed_checkpoint['name']} {completed_checkpoint['cumulative_target_ml']} ml；"
            f"下一個 {next_checkpoint['name']}，目標累積 {next_checkpoint['cumulative_target_ml']} ml，尚差 {remaining_ml} ml"
        )

    return {
        "checkpoints": checkpoints,
        "completed_checkpoint": completed_checkpoint,
        "next_checkpoint": next_checkpoint,
        "is_completed": is_completed,
        "remaining_ml": remaining_ml,
        "achieved_ml": achieved_ml,
        "total_target_ml": checkpoints[-1]["cumulative_target_ml"],
        "summary_text": summary_text,
    }


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
    fixed_plan = build_fixed_plan_summary(daily.total_ml)
    current_block = current_time_block(blocks)
    step_ml = block_step_ml(current_block)
    daily_remaining_ml = max(0, daily.target_ml - daily.total_ml)
    block_remaining_ml = current_block["remaining_ml"]
    suggested_step_ml = step_ml
    suggested_step_text = f"{suggested_step_ml} ml"

    if daily.achieved:
        suggested_step_text = "今天已達標"
    elif state.debt_ml >= step_ml * 4:
        suggested_step_ml = min(daily_remaining_ml, block_remaining_ml, max(step_ml, step_ml * 2))
        suggested_step_text = f"{max(step_ml, suggested_step_ml)} ml"
    elif state.debt_ml > 0:
        suggested_step_ml = min(step_ml, daily_remaining_ml, block_remaining_ml)
        suggested_step_text = f"{max(100, suggested_step_ml)} ml"
    elif block_remaining_ml > 0:
        suggested_step_text = f"若想補齊本時段，可再喝 {min(step_ml, block_remaining_ml)} ml"

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
        note = (
            f"距離今日目標還差 {max(0, daily.target_ml - daily.total_ml)} ml；"
            f"距離這個時段達標還差 {current_block['remaining_ml']} ml。"
        )

    motivation = build_status_message(daily, blocks, week_ok_days, fail_streak)

    return (
        f"{level} 喝水提醒\n\n"
        f"時段：{current_block['name']} {current_block['label']}\n"
        f"建議這次先補：{suggested_step_text}\n"
        f"目前應補水量：{state.debt_ml} ml\n"
        f"今日已喝：{daily.total_ml} / {daily.target_ml} ml\n"
        f"本時段進度：{current_block['amount_ml']} / {current_block['target_ml']} ml\n"
        f"{fixed_plan['summary_text']}\n"
        f"本週達標：{week_ok_days} / 7 天\n"
        f"近三日：{' '.join(recent3_marks)}\n"
        f"{note}\n{motivation}\n\n"
        "請點擊按鈕記錄這次喝了多少 ml"
    )


def reminder_keyboard(user: User) -> dict:
    custom_amounts = parse_quick_add_amounts(user.quick_add_amounts)
    rows = []
    for index in range(0, len(custom_amounts), 3):
        chunk = custom_amounts[index : index + 3]
        rows.append([{"text": f"+{amount}", "callback_data": f"WATER_{amount}"} for amount in chunk])
    footer_row = [{"text": "查看狀態", "callback_data": "WATER_STATUS"}]
    dashboard_url = dashboard_url_for_chat(user.chat_id)
    if dashboard_url:
        footer_row.append({"text": "喝水儀表板", "url": dashboard_url})
    rows.append(footer_row)
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


def render_summary_image(user: User, summary: dict) -> bytes:
    if Image is None or ImageDraw is None or ImageFont is None:
        raise RuntimeError("Summary image rendering requires Pillow")
    image = Image.new("RGB", (SUMMARY_IMAGE_WIDTH, SUMMARY_IMAGE_HEIGHT), "#F6FBF6")
    draw = ImageDraw.Draw(image)

    title_font = load_font(60, bold=True)
    heading_font = load_font(34, bold=True)
    body_font = load_font(26)
    small_font = load_font(22)

    draw.rounded_rectangle((40, 36, 1040, 864), radius=34, fill="#FFFFFF", outline="#D8EBDF")
    draw.text((78, 78), "喝水紀錄快照", font=title_font, fill="#1C3729")
    draw.text((82, 154), f"今日 {summary['daily_total']} / {summary['target']} ml", font=heading_font, fill="#2D6043")

    progress_left = 82
    progress_top = 220
    progress_right = 998
    progress_bottom = 260
    draw.rounded_rectangle((progress_left, progress_top, progress_right, progress_bottom), radius=20, fill="#EDF6EF")
    ratio = 0 if not summary["target"] else min(1, summary["daily_total"] / summary["target"])
    fill_right = progress_left + int((progress_right - progress_left) * ratio)
    draw.rounded_rectangle((progress_left, progress_top, max(progress_left + 24, fill_right), progress_bottom), radius=20, fill="#69C792")
    draw.text((82, 284), f"待補水量 {summary['debt_ml']} ml", font=body_font, fill="#6B8B79")
    draw.text((640, 284), f"本週達標 {summary['week_ok_days']} / 7", font=body_font, fill="#6B8B79")
    fixed_plan_text = (summary.get("fixed_plan") or {}).get("summary_text", "")
    if fixed_plan_text:
        draw.text((82, 326), fixed_plan_text, font=small_font, fill="#456A55")

    blocks = summary.get("time_blocks", [])[:3]
    block_top = 360
    for index, block in enumerate(blocks):
        card_left = 82 + index * 302
        card_right = card_left + 270
        draw.rounded_rectangle((card_left, block_top, card_right, block_top + 180), radius=26, fill="#F9FFFB", outline="#D8EBDF")
        draw.text((card_left + 22, block_top + 22), block["name"], font=heading_font, fill="#214A33")
        draw.text((card_left + 22, block_top + 64), block["label"], font=small_font, fill="#6B8B79")
        draw.rounded_rectangle((card_left + 22, block_top + 104, card_right - 22, block_top + 124), radius=10, fill="#EAF4ED")
        block_ratio = 0 if not block["target_ml"] else min(1, block["amount_ml"] / block["target_ml"])
        block_fill_right = card_left + 22 + int((card_right - card_left - 44) * block_ratio)
        draw.rounded_rectangle((card_left + 22, block_top + 104, max(card_left + 40, block_fill_right), block_top + 124), radius=10, fill="#8FE3C9")
        draw.text((card_left + 22, block_top + 138), f"{block['amount_ml']} / {block['target_ml']} ml", font=small_font, fill="#355F47")

    log_top = 596
    draw.text((82, log_top), "最近紀錄", font=heading_font, fill="#1C3729")
    recent_logs = summary.get("recent_logs", [])[:5]
    row_top = log_top + 56
    if not recent_logs:
        draw.text((82, row_top), "目前還沒有紀錄", font=body_font, fill="#6B8B79")
    for index, item in enumerate(recent_logs):
        y = row_top + index * 46
        timestamp = item.get("logged_at_local", item["logged_at"]).replace("T", " ")[:16]
        draw.text((82, y), timestamp, font=small_font, fill="#456A55")
        sign = "+" if item["amount_ml"] >= 0 else "-"
        draw.text((760, y), f"{sign}{abs(item['amount_ml'])} ml", font=small_font, fill="#214A33")

    footer = now_in_timezone(user.timezone).strftime("更新時間 %Y-%m-%d %H:%M")
    draw.text((82, 814), footer, font=small_font, fill="#7A9A86")

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


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


async def send_summary_photo(chat_id: str, png_bytes: bytes, caption: Optional[str] = None) -> None:
    if not TELEGRAM_API_BASE:
        return
    user: Optional[User] = None
    from .db import session_scope

    with session_scope() as db:
        user = db.scalar(select(User).where(User.chat_id == chat_id))
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    if user is not None:
        data["reply_markup"] = json.dumps(reminder_keyboard(user))
    files = {"photo": ("hydration-summary.png", png_bytes, "image/png")}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{TELEGRAM_API_BASE}/sendPhoto", data=data, files=files)
        if response.is_error:
            raise RuntimeError(f"Telegram API sendPhoto failed: status={response.status_code} body={response.text}")


async def delete_message(chat_id: str, message_id: Optional[Union[str, int]]) -> None:
    if not message_id:
        return
    try:
        await telegram_api("deleteMessage", {"chat_id": chat_id, "message_id": int(message_id)})
    except Exception:
        return


async def clear_legacy_reply_keyboard(chat_id: str) -> None:
    try:
        result = await telegram_api(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": "\u2060",
                "disable_notification": True,
                "reply_markup": {"remove_keyboard": True},
            },
        )
        message = result.get("result") or {}
        if message.get("message_id"):
            await delete_message(chat_id, message["message_id"])
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
    user: Optional[User] = None
    if chat_id:
        from .db import session_scope

        with session_scope() as db:
            user = db.scalar(select(User).where(User.chat_id == chat_id))
    await clear_legacy_reply_keyboard(chat_id)
    payload = {
        "chat_id": chat_id,
        "text": text,
    }
    if user is not None:
        payload["reply_markup"] = reminder_keyboard(user)
    await telegram_api("sendMessage", payload)


def record_drink(db: Session, user: User, amount_ml: int, source: str = "telegram") -> tuple[WaterState, WaterDaily, int]:
    local_now = now_in_timezone(user.timezone)
    local_today = local_now.date()
    state = ensure_user_state(db, user, local_today)
    daily = ensure_daily_record(db, user, local_today)
    if amount_ml == 0:
        raise ValueError("amount_ml cannot be zero")

    applied_amount_ml = amount_ml if amount_ml > 0 else -min(daily.total_ml, abs(amount_ml))

    debt_reduction = min(max(applied_amount_ml, 0), max(0, state.debt_ml))
    state.debt_ml = max(0, state.debt_ml - debt_reduction)
    state.daily_total = max(0, daily.total_ml + applied_amount_ml)
    state.last_drink_at = local_now.astimezone(timezone.utc).replace(tzinfo=None)
    state.status = "done" if state.daily_total >= daily.target_ml else ("drank" if state.debt_ml == 0 else "waiting")

    daily.total_ml = max(0, daily.total_ml + applied_amount_ml)
    daily.target_ml = user.daily_target
    daily.achieved = daily.total_ml >= daily.target_ml

    db.add(WaterLog(user_id=user.id, amount_ml=applied_amount_ml, source=source, logged_at=datetime.utcnow()))
    db.flush()
    return state, daily, applied_amount_ml


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
    fixed_plan = build_fixed_plan_summary(daily.total_ml)
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
        "timezone": user.timezone,
        "daily_total": daily.total_ml,
        "target": daily.target_ml,
        "debt_ml": state.debt_ml,
        "week_ok_days": week_ok_days,
        "status_message": build_status_message(daily, blocks, week_ok_days, fail_streak),
        "time_blocks": blocks,
        "recent_7_days": recent7,
        "fixed_plan": fixed_plan,
        "completed_checkpoint": fixed_plan["completed_checkpoint"],
        "next_checkpoint": fixed_plan["next_checkpoint"],
        "is_completed": fixed_plan["is_completed"],
        "remaining_ml": fixed_plan["remaining_ml"],
        "recent_logs": [
            {
                "id": row.id,
                "amount_ml": row.amount_ml,
                "source": row.source,
                "logged_at": row.logged_at.isoformat(),
                "logged_at_local": row.logged_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(user.timezone)).isoformat(),
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
