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

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
CHANNEL_USERNAME = os.environ["CHANNEL_USERNAME"].lstrip("@")
FIREBASE_CREDENTIALS = os.environ["FIREBASE_CREDENTIALS"]

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("er404")

cred = credentials.Certificate(FIREBASE_CREDENTIALS)
firebase_admin.initialize_app(cred)
db = firestore.client()

C_MSG = db.collection("messages")
C_FBAN = db.collection("fban_log")
C_ADMIN = db.collection("admin_log")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def is_owner(update: Update):
    return update.effective_user and update.effective_user.id == OWNER_ID


def is_target_channel(chat: Chat | None):
    return chat and chat.username and chat.username.lower() == CHANNEL_USERNAME.lower()


# ===================== CHANNEL POSTS =====================
async def on_channel_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post or update.edited_channel_post
    if not msg or not is_target_channel(msg.chat):
        return

    sender = msg.sender_chat or msg.chat

    # 🔥 FIX 1: REAL user_id mapping (CRITICAL)
    user_id = getattr(msg.from_user, "id", None) or sender.id

    doc = {
        "message_id": msg.message_id,
        "chat_id": msg.chat.id,
        "user_id": user_id,
        "username": getattr(sender, "username", None),
        "display_name": getattr(sender, "title", None),
        "text": msg.text or msg.caption or "",
        "views": getattr(msg, "views", 0),
        "status": "active",
        "edited": bool(update.edited_channel_post),
        "timestamp": firestore.SERVER_TIMESTAMP,
        "telegram_date": msg.date.isoformat() if msg.date else now_iso(),
    }

    doc_id = f"{msg.chat.id}:{msg.message_id}"
    C_MSG.document(doc_id).set(doc, merge=True)
    log.info("stored %s", doc_id)


# ===================== FBAN EVENTS =====================
async def on_chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cmu: ChatMemberUpdated = update.chat_member or update.my_chat_member
    if not cmu or not is_target_channel(cmu.chat):
        return

    old = cmu.old_chat_member.status
    new = cmu.new_chat_member.status
    user = cmu.new_chat_member.user

    action = None
    if new == ChatMemberStatus.BANNED:
        action = "banned"
    elif old == ChatMemberStatus.BANNED and new != ChatMemberStatus.BANNED:
        action = "unbanned"

    if not action:
        return

    # 🔥 FIX 2: ensure DB update BEFORE log
    _bulk_update_user_messages(user.id, "fban" if action == "banned" else "active")

    C_FBAN.add({
        "user_id": user.id,
        "action": action,
        "timestamp": firestore.SERVER_TIMESTAMP,
    })

    await ctx.bot.send_message(
        OWNER_ID,
        f"FBAN EVENT\nUser: {user.id}\nAction: {action}"
    )


def _bulk_update_user_messages(user_id: int, status: str):
    q = C_MSG.where("user_id", "==", user_id).stream()
    batch = db.batch()
    count = 0

    for doc in q:
        batch.update(doc.reference, {"status": status})
        count += 1

    if count:
        batch.commit()

    return count


# ===================== COMMANDS =====================
async def cmd_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    uid = int(ctx.args[0])
    n = _bulk_update_user_messages(uid, "deleted")
    await update.message.reply_text(f"deleted {n}")


async def cmd_fban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    uid = int(ctx.args[0])

    n = _bulk_update_user_messages(uid, "fban")

    C_FBAN.add({
        "user_id": uid,
        "action": "banned",
        "source": "command"
    })

    await update.message.reply_text(f"fban {n}")


async def cmd_unfban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    uid = int(ctx.args[0])

    n = _bulk_update_user_messages(uid, "active")

    C_FBAN.add({
        "user_id": uid,
        "action": "unbanned",
        "source": "command"
    })

    await update.message.reply_text(f"unfban {n}")


async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_owner(update):
        await update.message.reply_text("pong")


# ===================== START =====================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, on_channel_post))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_CHANNEL_POST, on_channel_post))

    app.add_handler(ChatMemberHandler(on_chat_member))

    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("fban", cmd_fban))
    app.add_handler(CommandHandler("unfban", cmd_unfban))
    app.add_handler(CommandHandler("ping", cmd_ping))

    app.run_polling()


if __name__ == "__main__":
    main()
