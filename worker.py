"""
Telegram MTProto worker for PROSPERITY AND LOYALTY GROUP
Listens to monitored bots, extracts CPFs from messages, sends to Lovable Cloud
via the worker-bridge edge function (no SUPABASE_SERVICE_ROLE_KEY required).
"""
import asyncio
import os
import re
import logging
from typing import Any, Dict, List

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession

load_dotenv()

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
BRIDGE_URL = os.environ["BRIDGE_URL"].rstrip("/")
WORKER_SECRET = os.environ["WORKER_SHARED_SECRET"]
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))

CPF_RE = re.compile(r"\b(\d{3}\.?\d{3}\.?\d{3}-?\d{2})\b")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("worker")


async def bridge(action: str, **kwargs) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            BRIDGE_URL,
            headers={
                "Content-Type": "application/json",
                "x-worker-secret": WORKER_SECRET,
            },
            json={"action": action, **kwargs},
        )
        r.raise_for_status()
        return r.json()


def normalize_cpf(s: str) -> str:
    return re.sub(r"\D", "", s)


# Currently active Telethon clients indexed by user_id
running: Dict[str, asyncio.Task] = {}


async def run_session(session_info: Dict[str, Any]):
    user_id = session_info["user_id"]
    bots = {int(b["bot_id"]): b for b in session_info.get("bots", [])}
    if not bots:
        log.info("user %s has no active bots, skipping", user_id)
        return

    client = TelegramClient(
        StringSession(session_info["session_string"]),
        API_ID,
        API_HASH,
    )

    try:
        await client.connect()
        if not await client.is_user_authorized():
            log.warning("session for %s not authorized, marking error", user_id)
            await bridge("update_session", user_id=user_id,
                         status="error", last_error="not authorized")
            return

        log.info("listening for %s on bots %s", user_id, list(bots.keys()))

        @client.on(events.NewMessage(chats=list(bots.keys())))
        async def handler(event):
            try:
                text = event.message.message or ""
                cpfs = CPF_RE.findall(text)
                if not cpfs:
                    return
                bot_id = event.chat_id
                bot_meta = bots.get(bot_id, {})
                rows = [
                    {
                        "user_id": user_id,
                        "bot_id": bot_id,
                        "bot_username": bot_meta.get("bot_username"),
                        "cpf": normalize_cpf(c),
                        "message_text": text[:2000],
                        "message_id": event.message.id,
                    }
                    for c in set(cpfs)
                ]
                await bridge("insert_captures_bulk", rows=rows)
                log.info("captured %d CPF(s) for %s from bot %s",
                         len(rows), user_id, bot_id)
            except Exception as exc:
                log.exception("handler error: %s", exc)

        await client.run_until_disconnected()
    except Exception as exc:
        log.exception("session %s crashed: %s", user_id, exc)
        try:
            await bridge("update_session", user_id=user_id,
                         status="error", last_error=str(exc)[:500])
        except Exception:
            pass
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def supervisor():
    while True:
        try:
            data = await bridge("list_active_sessions")
            sessions = data.get("sessions", [])
            wanted = {s["user_id"] for s in sessions}

            # stop sessions that no longer exist
            for uid in list(running.keys()):
                if uid not in wanted:
                    log.info("stopping session for %s", uid)
                    running[uid].cancel()
                    running.pop(uid, None)

            # start new sessions
            for s in sessions:
                uid = s["user_id"]
                if uid not in running or running[uid].done():
                    log.info("starting session for %s", uid)
                    running[uid] = asyncio.create_task(run_session(s))
        except Exception as exc:
            log.exception("supervisor error: %s", exc)

        await asyncio.sleep(POLL_INTERVAL)


async def main():
    log.info("worker starting, bridge=%s", BRIDGE_URL)
    # quick health check
    pong = await bridge("ping")
    log.info("bridge ping: %s", pong)
    await supervisor()


if __name__ == "__main__":
    asyncio.run(main())
