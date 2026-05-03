"""
#ΞЯ404 🐺 — Telegram → Firestore bridge bot
==========================================
Listens to a Telegram channel, mirrors every post to Firestore in real time.
Supports admin commands sent to the bot in DM by the OWNER:

    /del <user_id>       → soft-deletes all messages from that user_id
    /fban <user_id>      → marks that user_id status=fban + logs event
    /unfban <user_id>    → reverts fban
    /ping                → health check

Also reacts to channel chat-member events (ban / unban) and DM-notifies the OWNER.

Firestore collections written:
    messages    one doc per Telegram message
    fban_log    one doc per ban / unban event
    admin_log   one doc per admin command (audit trail)

ENV (see .env.example):
    BOT_TOKEN, CHANNEL_USERNAME (no @), OWNER_ID, FIREBASE_CREDENTIALS (path)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update, Chat, ChatMemberUpdated
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import firebase_admin
from firebase_admin import credentials, firestore

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

# ---------- Config ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
CHANNEL_USERNAME = os.environ["CHANNEL_USERNAME"].lstrip("@")
FIREBASE_CREDENTIALS = os.environ["FIREBASE_CREDENTIALS"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("er404")

# ---------- Firestore init ----------
cred = credentials.Certificate(FIREBASE_CREDENTIALS)
firebase_admin.initialize_app(cred)
db = firestore.client()

C_MSG = db.collection("messages")
C_FBAN = db.collection("fban_log")
C_ADMIN = db.collection("admin_log")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_owner(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == OWNER_ID)


def is_target_channel(chat: Chat | None) -> bool:
    if not chat:
        return False
    if chat.username and chat.username.lower() == CHANNEL_USERNAME.lower():
        return True
    return False


# =====================================================================
# Channel post → Firestore
# =====================================================================
async def on_channel_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.channel_post or update.edited_channel_post
    if msg is None or not is_target_channel(msg.chat):
        return

    text = msg.text or msg.caption or ""
    sender_chat = msg.sender_chat or msg.chat

    doc = {
        "message_id": msg.message_id,
        "chat_id": msg.chat.id,
        "user_id": sender_chat.id if sender_chat else None,
        "username": sender_chat.username if sender_chat else None,
        "display_name": sender_chat.title if sender_chat else None,
        "text": text,
        "views": msg.views or 0,
        "status": "active",
        "edited": bool(update.edited_channel_post),
        "timestamp": firestore.SERVER_TIMESTAMP,
        "telegram_date": msg.date.isoformat() if msg.date else now_iso(),
    }

    # idempotent doc id: chat_id:message_id
    doc_id = f"{msg.chat.id}:{msg.message_id}"
    C_MSG.document(doc_id).set(doc, merge=True)
    log.info("channel post stored: %s len=%d", doc_id, len(text))


# =====================================================================
# Channel member ban / unban → fban_log + DM owner
# =====================================================================
async def on_chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cmu: ChatMemberUpdated | None = update.chat_member or update.my_chat_member
    if not cmu or not is_target_channel(cmu.chat):
        return

    old_status = cmu.old_chat_member.status
    new_status = cmu.new_chat_member.status
    user = cmu.new_chat_member.user

    action: str | None = None
    if new_status == ChatMemberStatus.BANNED and old_status != ChatMemberStatus.BANNED:
        action = "banned"
    elif old_status == ChatMemberStatus.BANNED and new_status != ChatMemberStatus.BANNED:
        action = "unbanned"
    if action is None:
        return

    log_doc = {
        "user_id": user.id,
        "username": user.username,
        "display_name": user.full_name,
        "action": action,
        "by_admin_id": cmu.from_user.id if cmu.from_user else None,
        "timestamp": firestore.SERVER_TIMESTAMP,
    }
    C_FBAN.add(log_doc)

    # mirror status onto user's existing messages
    if action == "banned":
        _bulk_update_user_messages(user.id, status="fban")
    else:
        _bulk_update_user_messages(user.id, status="active")

    text = (
        f"<b>FBAN EVENT</b>\n"
        f"User ID: <code>{user.id}</code>\n"
        f"Username: @{user.username or '—'}\n"
        f"Action: <b>{action}</b>"
    )
    try:
        await ctx.bot.send_message(OWNER_ID, text, parse_mode=ParseMode.HTML)
    except Exception as e:
        log.warning("owner notify failed: %s", e)


def _bulk_update_user_messages(user_id: int, *, status: str) -> int:
    q = C_MSG.where("user_id", "==", user_id).stream()
    n = 0
    batch = db.batch()
    for snap in q:
        batch.update(snap.reference, {"status": status})
        n += 1
        if n % 400 == 0:
            batch.commit()
            batch = db.batch()
    if n:
        batch.commit()
    return n


# =====================================================================
# Admin commands (DM only, OWNER only)
# =====================================================================
async def cmd_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    if not ctx.args:
        await update.effective_message.reply_text("usage: /del <user_id>")
        return
    try:
        target = int(ctx.args[0])
    except ValueError:
        await update.effective_message.reply_text("user_id must be an integer")
        return

    n = _bulk_update_user_messages(target, status="deleted")
    C_ADMIN.add({
        "action": "del",
        "target_user_id": target,
        "affected": n,
        "by": OWNER_ID,
        "timestamp": firestore.SERVER_TIMESTAMP,
    })
    await update.effective_message.reply_text(f"✅ marked {n} message(s) as deleted for user {target}")


async def cmd_fban(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update) or not ctx.args:
        return
    try:
        target = int(ctx.args[0])
    except ValueError:
        return
    n = _bulk_update_user_messages(target, status="fban")
    C_FBAN.add({
        "user_id": target,
        "action": "banned",
        "by_admin_id": OWNER_ID,
        "source": "command",
        "timestamp": firestore.SERVER_TIMESTAMP,
    })
    C_ADMIN.add({"action": "fban", "target_user_id": target, "affected": n, "by": OWNER_ID, "timestamp": firestore.SERVER_TIMESTAMP})
    await update.effective_message.reply_text(f"⛔ fban applied to {target} (touched {n} messages)")


async def cmd_unfban(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update) or not ctx.args:
        return
    try:
        target = int(ctx.args[0])
    except ValueError:
        return
    n = _bulk_update_user_messages(target, status="active")
    C_FBAN.add({
        "user_id": target,
        "action": "unbanned",
        "by_admin_id": OWNER_ID,
        "source": "command",
        "timestamp": firestore.SERVER_TIMESTAMP,
    })
    C_ADMIN.add({"action": "unfban", "target_user_id": target, "affected": n, "by": OWNER_ID, "timestamp": firestore.SERVER_TIMESTAMP})
    await update.effective_message.reply_text(f"✅ fban lifted for {target} (touched {n} messages)")


async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    await update.effective_message.reply_text("pong 🐺")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    counts: dict[str, int] = {}
    for status in ("active", "fban", "deleted"):
        try:
            agg = C_MSG.where("status", "==", status).count().get()
            counts[status] = int(agg[0][0].value)
        except Exception as e:
            log.warning("count(%s) failed, falling back to scan: %s", status, e)
            counts[status] = sum(1 for _ in C_MSG.where("status", "==", status).stream())
    fban_events = sum(1 for _ in C_FBAN.stream())
    total = counts["active"] + counts["fban"] + counts["deleted"]

    text = (
        "📊 <b>STATS</b>\n"
        f"Total messages: <code>{total}</code>\n"
        f"  • Active:  <code>{counts['active']}</code>\n"
        f"  • FBAN:    <code>{counts['fban']}</code>\n"
        f"  • Deleted: <code>{counts['deleted']}</code>\n"
        f"FBAN events logged: <code>{fban_events}</code>"
    )
    C_ADMIN.add({"action": "stats", "by": OWNER_ID, "snapshot": counts, "timestamp": firestore.SERVER_TIMESTAMP})
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


# =====================================================================
# Bootstrap
# =====================================================================
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    # Channel post stream (new + edited)
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, on_channel_post))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_CHANNEL_POST, on_channel_post))

    # Ban / unban events
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # Owner commands
    app.add_handler(CommandHandler("del", cmd_del, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("fban", cmd_fban, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("unfban", cmd_unfban, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("ping", cmd_ping, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("stats", cmd_stats, filters=filters.ChatType.PRIVATE))

    log.info("ER404 bot starting · channel=@%s · owner=%s", CHANNEL_USERNAME, OWNER_ID)
    app.run_polling(allowed_updates=[
        "message", "edited_message",
        "channel_post", "edited_channel_post",
        "chat_member", "my_chat_member",
    ])


if __name__ == "__main__":
    main()
