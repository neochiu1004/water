from __future__ import annotations

import asyncio
import logging
import os
from datetime import time
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, select
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from .db import Base, engine, get_db, session_scope
from .models import User, WaterLog
from .schemas import AppConfigOut, LogUpdateIn, ManualDrinkIn, SummaryOut, UserSettingsIn, UserSettingsOut
from .services import (
    dashboard_links_for_chat,
    dashboard_url_for_chat,
    delete_log_entry,
    delete_message,
    migrate_legacy_cup_units,
    parse_quick_add_amounts,
    record_drink,
    render_summary_image,
    run_reminder_cycle,
    send_summary_photo,
    send_text,
    summary_for_user,
    telegram_api,
    serialize_quick_add_amounts,
    update_log_amount,
)


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="喝水提醒")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
logger = logging.getLogger("water-reminder")

scheduler = AsyncIOScheduler(timezone=os.getenv("SCHEDULER_TIMEZONE", "Asia/Taipei"))
polling_task: Optional[asyncio.Task] = None
polling_offset = 0
DEFAULT_CHAT_ID = os.getenv("DEFAULT_CHAT_ID")
DASHBOARD_URL = os.getenv("DASHBOARD_URL")


def ensure_schema_updates() -> None:
    inspector = inspect(engine)
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    if "quick_add_amounts" not in user_columns:
        with engine.begin() as conn:
            conn.execute(sql_text("ALTER TABLE users ADD COLUMN quick_add_amounts VARCHAR(128) DEFAULT '250,500,750'"))


@app.on_event("startup")
async def startup_event():
    Base.metadata.create_all(bind=engine)
    ensure_schema_updates()
    with session_scope() as db:
        migrated = migrate_legacy_cup_units(db)
        if migrated:
            logger.info("Migrated %s legacy cup-based values to ml", migrated)
    if not scheduler.running:
        scheduler.add_job(scheduled_reminder, CronTrigger(minute=0), id="hourly-reminder", replace_existing=True)
        scheduler.start()
    global polling_task
    if os.getenv("TELEGRAM_USE_POLLING", "true").lower() == "true" and polling_task is None:
        try:
            await telegram_api("deleteWebhook", {"drop_pending_updates": False})
            logger.info("Telegram webhook cleared for polling mode")
        except Exception as exc:
            logger.warning("Failed to clear Telegram webhook: %s", exc)
        polling_task = asyncio.create_task(poll_telegram_updates())


@app.on_event("shutdown")
async def shutdown_event():
    if scheduler.running:
        scheduler.shutdown(wait=False)
    global polling_task
    if polling_task is not None:
        polling_task.cancel()
        polling_task = None


async def scheduled_reminder():
    with session_scope() as db:
        await run_reminder_cycle(db)


def get_user_or_404(db: Session, chat_id: str) -> User:
    user = db.scalar(select(User).where(User.chat_id == chat_id))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found. Please send /start to the bot first.")
    return user


def help_text(chat_id: Optional[str] = None) -> str:
    lines = [
        "可用指令：",
        "/status 查看今天進度",
        "/drink 300 手動記錄 300 ml",
        "/drink -300 扣減今天 300 ml",
        "/water 300 手動記錄 300 ml",
        "喝水 300、喝水 -300 也可以直接記錄",
        "+300、-300、300ml、300cc 也可以直接記錄",
        "/help 查看這份說明",
    ]
    if chat_id:
        dashboard_url = dashboard_url_for_chat(chat_id)
        if dashboard_url:
            lines.append(f"儀表板：{dashboard_url}")
    return "\n".join(lines)


def parse_manual_drink(text: str) -> Optional[int]:
    normalized = text.strip()
    if not normalized:
        return None

    shorthand = normalized
    sign = 1
    if shorthand.startswith("+"):
        shorthand = shorthand[1:].strip()
    elif shorthand.startswith("-"):
        shorthand = shorthand[1:].strip()
        sign = -1
    for suffix in ("ml", "ML", "cc", "CC", "毫升", "杯"):
        if shorthand.endswith(suffix):
            shorthand = shorthand[: -len(suffix)].strip()
            break
    if shorthand.isdigit():
        amount_ml = int(shorthand)
        return sign * amount_ml if amount_ml > 0 else None

    command_prefixes = ("/drink", "/water", "喝水", "喝了", "補水")
    for prefix in command_prefixes:
        if normalized.startswith(prefix):
            parts = normalized.split()
            if len(parts) < 2:
                return None
            try:
                amount_raw = parts[1].lower().replace("ml", "").replace("cc", "").replace("毫升", "").replace("杯", "")
                amount_ml = int(amount_raw)
            except ValueError:
                return None
            return amount_ml if amount_ml != 0 else None
    return None


async def process_telegram_update(update: dict, db: Session):
    message = update.get("message") or {}
    callback = update.get("callback_query") or {}

    if message:
        chat_id = str(message.get("chat", {}).get("id"))
        text = (message.get("text") or "").strip()
        logger.info("Incoming Telegram message chat_id=%s text=%r", chat_id, text)
        if not chat_id:
            return {"ok": True}

        user = db.scalar(select(User).where(User.chat_id == chat_id))
        if user is None and text.startswith("/start"):
            user = User(chat_id=chat_id)
            db.add(user)
            db.commit()
            await send_text(chat_id, "喝水提醒已啟用。\n\n" + help_text(chat_id))
            return {"ok": True}

        if user is None:
            await send_text(chat_id, "請先傳送 /start 啟用喝水提醒。")
            return {"ok": True}

        if text.startswith("/status"):
            summary = summary_for_user(db, user)
            await send_text(
                chat_id,
                (
                    f"今日 {summary['daily_total']} / {summary['target']} ml，本週達標 {summary['week_ok_days']} / 7 天，"
                    f"待補水量 {summary['debt_ml']} ml。\n{summary['status_message']}\n{summary['fixed_plan']['summary_text']}"
                ),
            )
            try:
                await send_summary_photo(chat_id, render_summary_image(user, summary), "喝水紀錄快照")
            except Exception as exc:
                logger.warning("Failed to send hydration snapshot: %s", exc)
        elif text.startswith("/dashboard") or text == "喝水儀表板":
            dashboard_links = dashboard_links_for_chat(chat_id)
            if dashboard_links:
                lines = ["喝水儀表板："]
                for label, url in dashboard_links:
                    lines.append(f"{label}：{url}")
                await send_text(chat_id, "\n".join(lines))
            else:
                await send_text(chat_id, "目前尚未設定儀表板連結。")
        elif text.startswith("/help"):
            await send_text(chat_id, help_text(chat_id))
        elif parse_manual_drink(text) is not None:
            amount_ml = parse_manual_drink(text)
            if amount_ml is None:
                await send_text(chat_id, "用法：/drink 300、/drink -300，也可直接輸入：喝水 300、喝水 -300、+300、-300、300ml、300cc")
                return {"ok": True}
            state, daily, applied_amount_ml = record_drink(db, user, amount_ml, source="telegram-command")
            db.commit()
            summary = summary_for_user(db, user)
            action_text = "已記錄" if applied_amount_ml >= 0 else "已扣減"
            await send_text(
                chat_id,
                (
                    f"{action_text} {abs(applied_amount_ml)} ml。今日累計 {daily.total_ml} / {daily.target_ml} ml，"
                    f"待補水量 {state.debt_ml} ml。\n{summary['status_message']}\n{summary['fixed_plan']['summary_text']}"
                ),
            )
            try:
                await send_summary_photo(chat_id, render_summary_image(user, summary), "喝水紀錄快照")
            except Exception as exc:
                logger.warning("Failed to send hydration snapshot: %s", exc)
        elif (
            text.startswith("/drink")
            or text.startswith("/water")
            or text.startswith("喝水")
            or text.startswith("喝了")
            or text.startswith("補水")
            or text.startswith("+")
        ):
            await send_text(chat_id, "用法：/drink 300、/drink -300，也可直接輸入：喝水 300、喝水 -300、+300、-300、300ml、300cc")
        else:
            await send_text(chat_id, help_text(chat_id))
        return {"ok": True}

    if callback:
        data = callback.get("data") or ""
        logger.info("Incoming Telegram callback data=%r", data)
        if not data.startswith("WATER_"):
            return {"ok": True}
        chat_id = str(callback.get("message", {}).get("chat", {}).get("id"))
        callback_id = callback.get("id")
        if callback_id:
            try:
                await telegram_api("answerCallbackQuery", {"callback_query_id": callback_id})
            except Exception:
                pass
        user = get_user_or_404(db, chat_id)
        if data == "WATER_STATUS":
            summary = summary_for_user(db, user)
            await send_text(
                chat_id,
                (
                    f"今日 {summary['daily_total']} / {summary['target']} ml，本週達標 {summary['week_ok_days']} / 7 天，"
                    f"待補水量 {summary['debt_ml']} ml。\n{summary['status_message']}\n{summary['fixed_plan']['summary_text']}"
                ),
            )
            try:
                await send_summary_photo(chat_id, render_summary_image(user, summary), "喝水紀錄快照")
            except Exception as exc:
                logger.warning("Failed to send hydration snapshot: %s", exc)
            return {"ok": True}
        amount_ml = int(data.split("_", 1)[1])
        state, daily, applied_amount_ml = record_drink(db, user, amount_ml, source="telegram-button")
        db.commit()
        summary = summary_for_user(db, user)
        await send_text(
            chat_id,
            (
                f"已記錄喝水 {abs(applied_amount_ml)} ml。今日累計 {daily.total_ml} / {daily.target_ml} ml，"
                f"待補水量 {state.debt_ml} ml。\n{summary['status_message']}\n{summary['fixed_plan']['summary_text']}"
            ),
        )
        try:
            await send_summary_photo(chat_id, render_summary_image(user, summary), "喝水紀錄快照")
        except Exception as exc:
            logger.warning("Failed to send hydration snapshot: %s", exc)
        return {"ok": True}

    return {"ok": True}


async def poll_telegram_updates():
    global polling_offset
    while True:
        try:
            response = await telegram_api(
                "getUpdates",
                {
                    "offset": polling_offset,
                    "timeout": 20,
                    "allowed_updates": ["message", "callback_query"],
                },
            )
            for update in response.get("result", []):
                polling_offset = max(polling_offset, int(update["update_id"]) + 1)
                logger.info("Processing Telegram update_id=%s", update.get("update_id"))
                with session_scope() as db:
                    await process_telegram_update(update, db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Telegram polling loop failed: %s", exc)
            await asyncio.sleep(5)
        else:
            await asyncio.sleep(1)


@app.get("/")
async def dashboard():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/config", response_model=AppConfigOut)
async def app_config():
    return {"default_chat_id": DEFAULT_CHAT_ID, "dashboard_url": DASHBOARD_URL}


@app.get("/api/users/{chat_id}/summary", response_model=SummaryOut)
async def user_summary(chat_id: str, db: Session = Depends(get_db)):
    user = get_user_or_404(db, chat_id)
    return summary_for_user(db, user)


@app.get("/api/users/{chat_id}/settings", response_model=UserSettingsOut)
async def get_settings(chat_id: str, db: Session = Depends(get_db)):
    user = get_user_or_404(db, chat_id)
    return {
        "chat_id": user.chat_id,
        "daily_target": user.daily_target,
        "reminder_start_hour": user.reminder_start.hour,
        "reminder_end_hour": user.reminder_end.hour,
        "timezone": user.timezone,
        "quick_add_amounts": parse_quick_add_amounts(user.quick_add_amounts),
    }


@app.post("/api/users/{chat_id}/settings")
async def update_settings(chat_id: str, payload: UserSettingsIn, db: Session = Depends(get_db)):
    user = get_user_or_404(db, chat_id)
    user.daily_target = payload.daily_target
    user.reminder_start = time(payload.reminder_start_hour, 0)
    user.reminder_end = time(payload.reminder_end_hour, 0)
    user.timezone = payload.timezone
    user.quick_add_amounts = serialize_quick_add_amounts(payload.quick_add_amounts)
    db.commit()
    return {"ok": True}


@app.post("/api/drink")
async def manual_drink(payload: ManualDrinkIn, db: Session = Depends(get_db)):
    user = get_user_or_404(db, payload.chat_id)
    state, daily, applied_amount_ml = record_drink(db, user, payload.amount_ml, source="web")
    db.commit()
    return {
        "ok": True,
        "applied_amount_ml": applied_amount_ml,
        "daily_total": daily.total_ml,
        "target": daily.target_ml,
        "debt_ml": state.debt_ml,
    }


@app.patch("/api/users/{chat_id}/logs/{log_id}")
async def edit_log(chat_id: str, log_id: int, payload: LogUpdateIn, db: Session = Depends(get_db)):
    user = get_user_or_404(db, chat_id)
    log = db.scalar(select(WaterLog).where(WaterLog.id == log_id, WaterLog.user_id == user.id))
    if log is None:
        raise HTTPException(status_code=404, detail="Log not found")
    update_log_amount(db, user, log, payload.amount_ml)
    db.commit()
    return {"ok": True}


@app.delete("/api/users/{chat_id}/logs/{log_id}")
async def remove_log(chat_id: str, log_id: int, db: Session = Depends(get_db)):
    user = get_user_or_404(db, chat_id)
    log = db.scalar(select(WaterLog).where(WaterLog.id == log_id, WaterLog.user_id == user.id))
    if log is None:
        raise HTTPException(status_code=404, detail="Log not found")
    delete_log_entry(db, user, log)
    db.commit()
    return {"ok": True}


@app.post("/api/reminders/run")
async def trigger_reminder():
    with session_scope() as db:
        sent = await run_reminder_cycle(db)
    return {"ok": True, "sent": sent}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, db: Session = Depends(get_db)):
    update = await request.json()
    return await process_telegram_update(update, db)
