# Water Reminder

Telegram 喝水提醒 + 網頁紀錄儀表板。

## 功能

- Telegram `/start` 註冊使用者
- 每小時自動提醒喝水
- Telegram inline button 記錄喝水杯數
- `/status` 與 `/drink 2` 指令
- 網頁查看今日進度、近 7 天趨勢、最近喝水紀錄

## 本機啟動

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
export $(grep -v '^#' .env | xargs)
uvicorn app.main:app --reload
```

開啟 [http://localhost:8000](http://localhost:8000)

## Telegram 接收模式

如果服務部署在 `192.168.31.28` 這種內網位址，建議直接用輪詢模式，不需要公開網址：

```bash
cp .env.example .env
docker compose --env-file .env up -d --build
```

輪詢模式會主動向 Telegram 拉更新，適合內網部署。

如果你之後有公開 HTTPS 網址，也可以切回 webhook 模式：

- `.env` 設 `TELEGRAM_USE_POLLING=false`
- 把 webhook 指到：

```bash
curl "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook?url=https://your-domain/telegram/webhook"
```

## Docker 部署

```bash
cp .env.example .env
docker compose --env-file .env up -d --build
```

## 主要 API

- `GET /api/users/{chat_id}/summary`
- `POST /api/drink`
- `POST /api/users/{chat_id}/settings`
- `POST /api/reminders/run`
