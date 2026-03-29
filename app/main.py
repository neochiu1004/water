from __future__ import annotations

import asyncio
import os
from datetime import time
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Base, engine, get_db, session_scope
from .models import User
from .schemas import AppConfigOut, ManualDrinkIn, SummaryOut, UserSettingsIn, UserSettingsOut
from .services import (
    delete_message,
    record_drink,
    run_reminder_cycle,
    send_text,
    summary_for_user,
    telegram_api,
)


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Water Reminder")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

scheduler = AsyncIOScheduler(timezone=os.getenv("SCHEDULER_TIMEZONE", "Asia/Taipei"))
polling_task: Optional[asyncio.Task] = None
polling_offset = 0
DEFAULT_CHAT_ID = os.getenv("DEFAULT_CHAT_ID")
DASHBOARD_URL = os.getenv("DASHBOARD_URL")


@app.on_event("startup")
async def startup_event():
    Base.metadata.create_all(bind=engine)
    if not scheduler.running:
        scheduler.add_job(scheduled_reminder, CronTrigger(minute=0), id="hourly-reminder", replace_existing=True)
        scheduler.start()
    global polling_task
    if os.getenv("TELEGRAM_USE_POLLING", "true").lower() == "true" and polling_task is None:
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


def help_text() -> str:
    return (
        "可用指令：\n"
        "/status 查看今天進度\n"
        "/drink 2 手動記錄 2 杯\n"
        "/water 2 手動記錄 2 杯\n"
        "喝水 2 也可以直接記錄\n"
        "+2、2杯、2 也可以直接記錄\n"
        "/help 查看這份說明"
    )


def parse_manual_drink(text: str) -> Optional[int]:
    normalized = text.strip()
    if not normalized:
        return None

    shorthand = normalized
    if shorthand.startswith("+"):
        shorthand = shorthand[1:].strip()
    if shorthand.endswith("杯"):
        shorthand = shorthand[:-1].strip()
    if shorthand.isdigit():
        cups = int(shorthand)
        return cups if cups > 0 else None

    command_prefixes = ("/drink", "/water", "喝水", "喝了", "補水")
    for prefix in command_prefixes:
        if normalized.startswith(prefix):
            parts = normalized.split()
            if len(parts) < 2:
                return None
            try:
                cups = int(parts[1])
            except ValueError:
                return None
            return cups if cups > 0 else None
    return None


async def process_telegram_update(update: dict, db: Session):
    message = update.get("message") or {}
    callback = update.get("callback_query") or {}

    if message:
        chat_id = str(message.get("chat", {}).get("id"))
        text = (message.get("text") or "").strip()
        if not chat_id:
            return {"ok": True}

        user = db.scalar(select(User).where(User.chat_id == chat_id))
        if user is None and text.startswith("/start"):
            user = User(chat_id=chat_id)
            db.add(user)
            db.commit()
            await send_text(chat_id, "喝水提醒已啟用。\n\n" + help_text())
            return {"ok": True}

        if user is None:
            await send_text(chat_id, "請先傳送 /start 啟用喝水提醒。")
            return {"ok": True}

        if text.startswith("/status"):
            summary = summary_for_user(db, user)
            await send_text(
                chat_id,
                f"今日 {summary['daily_total']} / {summary['target']} 杯，本週達標 {summary['week_ok_days']} / 7 天，補杯數 {summary['debt_cups']}。",
            )
        elif text.startswith("/help"):
            await send_text(chat_id, help_text())
        elif parse_manual_drink(text) is not None:
            cups = parse_manual_drink(text)
            if cups is None:
                await send_text(chat_id, "用法：/drink 2、/water 2，也可直接輸入：喝水 2、+2、2杯、2")
                return {"ok": True}
            state, daily = record_drink(db, user, cups, source="telegram-command")
            db.commit()
            await send_text(chat_id, f"已記錄 {cups} 杯。今日累計 {daily.cups} / {daily.target}，補杯數 {state.debt_cups}。")
        elif (
            text.startswith("/drink")
            or text.startswith("/water")
            or text.startswith("喝水")
            or text.startswith("喝了")
            or text.startswith("補水")
            or text.startswith("+")
        ):
            await send_text(chat_id, "用法：/drink 2、/water 2，也可直接輸入：喝水 2、+2、2杯、2")
        return {"ok": True}

    if callback:
        data = callback.get("data") or ""
        if not data.startswith("WATER_"):
            return {"ok": True}
        chat_id = str(callback.get("message", {}).get("chat", {}).get("id"))
        message_id = callback.get("message", {}).get("message_id")
        cups = int(data.split("_", 1)[1])
        user = get_user_or_404(db, chat_id)
        state, daily = record_drink(db, user, cups, source="telegram-button")
        if message_id:
            await delete_message(chat_id, message_id)
            state.last_message_id = None
        db.commit()
        await send_text(chat_id, f"已記錄喝水 {cups} 杯。今日累計 {daily.cups} / {daily.target}，補杯數 {state.debt_cups}。")
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
                with session_scope() as db:
                    await process_telegram_update(update, db)
        except asyncio.CancelledError:
            raise
        except Exception:
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
    }


@app.post("/api/users/{chat_id}/settings")
async def update_settings(chat_id: str, payload: UserSettingsIn, db: Session = Depends(get_db)):
    user = get_user_or_404(db, chat_id)
    user.daily_target = payload.daily_target
    user.reminder_start = time(payload.reminder_start_hour, 0)
    user.reminder_end = time(payload.reminder_end_hour, 0)
    user.timezone = payload.timezone
    db.commit()
    return {"ok": True}


@app.post("/api/drink")
async def manual_drink(payload: ManualDrinkIn, db: Session = Depends(get_db)):
    user = get_user_or_404(db, payload.chat_id)
    state, daily = record_drink(db, user, payload.cups, source="web")
    db.commit()
    return {
        "ok": True,
        "daily_total": daily.cups,
        "target": daily.target,
        "debt_cups": state.debt_cups,
    }


@app.post("/api/reminders/run")
async def trigger_reminder():
    with session_scope() as db:
        sent = await run_reminder_cycle(db)
    return {"ok": True, "sent": sent}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, db: Session = Depends(get_db)):
    update = await request.json()
    return await process_telegram_update(update, db)
