"""
Telegram MTProto Worker
- Lê telegram_sessions pendentes e completa login (phone -> code)
- Para sessoes ativas, escuta mensagens dos bots monitorados (telegram_monitored_bots)
- Extrai CPFs das mensagens e salva em telegram_captures
"""
import asyncio
import os
import re
import logging
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError
from supabase import create_client, Client

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("worker")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# session_id -> {"client": TelegramClient, "user_id": uuid}
active_clients: dict[str, dict] = {}

CPF_REGEX = re.compile(r"\b(\d{3}\.?\d{3}\.?\d{3}-?\d{2})\b")


def extract_cpf(text: str) -> str | None:
    if not text:
        return None
    m = CPF_REGEX.search(text)
    if not m:
        return None
    return re.sub(r"\D", "", m.group(1))


async def handle_pending_session(row: dict):
    """status='pending' -> envia codigo. status='code_sent' -> confirma codigo."""
    sid = row["id"]
    user_id = row["user_id"]
    phone = row["phone"]
    status = row["status"]

    try:
        if status == "pending":
            log.info(f"[{sid}] enviando codigo para {phone}")
            client = TelegramClient(StringSession(), API_ID, API_HASH)
            await client.connect()
            sent = await client.send_code_request(phone)
            session_str = client.session.save()
            sb.table("telegram_sessions").update({
                "status": "code_sent",
                "phone_code_hash": sent.phone_code_hash,
                "session_string": session_str,
                "last_error": None,
            }).eq("id", sid).execute()
            await client.disconnect()

        elif status == "code_submitted":
            # codigo deve estar em last_error (campo reusado) ou outra coluna
            # Aqui assumimos que o front grava o codigo em "phone_code_hash" como "hash|codigo"
            # ou usamos um campo dedicado. Por simplicidade lemos de last_error temporariamente.
            code = row.get("last_error")  # frontend grava o codigo aqui
            session_str = row.get("session_string")
            phone_code_hash = row.get("phone_code_hash")
            if not (code and session_str and phone_code_hash):
                return

            client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
            await client.connect()
            try:
                await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            except SessionPasswordNeededError:
                sb.table("telegram_sessions").update({
                    "status": "error",
                    "last_error": "Conta com 2FA nao suportada",
                }).eq("id", sid).execute()
                await client.disconnect()
                return
            except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
                sb.table("telegram_sessions").update({
                    "status": "pending",
                    "last_error": f"Codigo invalido: {e}",
                }).eq("id", sid).execute()
                await client.disconnect()
                return

            me = await client.get_me()
            new_session = client.session.save()
            sb.table("telegram_sessions").update({
                "status": "active",
                "session_string": new_session,
                "telegram_user_id": me.id,
                "last_error": None,
                "phone_code_hash": None,
            }).eq("id", sid).execute()
            await client.disconnect()
            log.info(f"[{sid}] login OK user={me.id}")

    except Exception as e:
        log.exception(f"[{sid}] erro: {e}")
        sb.table("telegram_sessions").update({
            "status": "error",
            "last_error": str(e)[:500],
        }).eq("id", sid).execute()


async def start_active_session(row: dict):
    sid = row["id"]
    user_id = row["user_id"]
    session_str = row["session_string"]
    if sid in active_clients:
        return

    log.info(f"[{sid}] iniciando listener")
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        log.warning(f"[{sid}] nao autorizada, marcando como error")
        sb.table("telegram_sessions").update({
            "status": "error", "last_error": "Sessao expirou"
        }).eq("id", sid).execute()
        await client.disconnect()
        return

    # pega bots monitorados deste user
    bots = sb.table("telegram_monitored_bots").select("bot_id,bot_username").eq(
        "user_id", user_id).eq("active", True).execute().data
    bot_ids = {b["bot_id"] for b in bots}
    bot_map = {b["bot_id"]: b.get("bot_username") for b in bots}

    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        sender_id = event.sender_id
        if sender_id not in bot_ids:
            return
        text = event.raw_text or ""
        cpf = extract_cpf(text)
        if not cpf:
            return
        try:
            sb.table("telegram_captures").insert({
                "user_id": user_id,
                "bot_id": sender_id,
                "bot_username": bot_map.get(sender_id),
                "cpf": cpf,
                "message_id": event.id,
                "message_text": text[:2000],
            }).execute()
            log.info(f"[{sid}] CPF capturado: {cpf} bot={sender_id}")
        except Exception as e:
            log.error(f"[{sid}] falha ao salvar: {e}")

    active_clients[sid] = {"client": client, "user_id": user_id}
    asyncio.create_task(client.run_until_disconnected())


async def poll_loop():
    while True:
        try:
            # 1) pendentes (envio de codigo + confirmacao)
            pend = sb.table("telegram_sessions").select("*").in_(
                "status", ["pending", "code_submitted"]).execute().data
            for row in pend:
                await handle_pending_session(row)

            # 2) ativas (start listener)
            act = sb.table("telegram_sessions").select("*").eq("status", "active").execute().data
            active_ids = {r["id"] for r in act}
            for row in act:
                if row["id"] not in active_clients:
                    await start_active_session(row)

            # 3) sessoes que sairam de active -> desconectar
            for sid in list(active_clients.keys()):
                if sid not in active_ids:
                    log.info(f"[{sid}] desconectando")
                    try:
                        await active_clients[sid]["client"].disconnect()
                    except Exception:
                        pass
                    active_clients.pop(sid, None)

        except Exception as e:
            log.exception(f"poll error: {e}")

        await asyncio.sleep(POLL_INTERVAL)


async def main():
    log.info("Worker iniciado")
    await poll_loop()


if __name__ == "__main__":
    asyncio.run(main())
