"""
/fwd_bot  — Forward media from a bot's private chat to a target channel.

Flow:
  1. User sends /fwd_bot
  2. Bot asks user to FORWARD the first message from the source bot
  3. Bot asks user to FORWARD the last message from the same source bot
  4. For channel-origin messages the ID is read directly from forward_origin.message_id
  5. For user/bot-origin messages (DM with a bot) the original message ID is NOT
     included in Telegram's forward header — pyrofork's MessageOriginUser has no
     message_id field. We recover it by having the userbot search chat history
     around the forwarded message's exact timestamp (forward_origin.date).
  6. If only a bot-token is configured (no userbot), we fall back to asking the
     user to type the message number or paste a link.
"""

import re
import asyncio
import logging
from datetime import timedelta
from urllib.parse import urlparse, parse_qs

from database import db
from config import temp
from .test import CLIENT, start_clone_bot, parse_buttons
from .regix import custom_caption, media
from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

logger = logging.getLogger(__name__)
CLIENT = CLIENT()

_CHUNK = 190


# ─── helpers ─────────────────────────────────────────────────────────────────

def _parse_any_link(text: str):
    """
    Accept several link/number formats and return (chat_ref, msg_id) or None.
      • https://t.me/username/123
      • https://t.me/c/1234567890/123
      • tg://openmessage?user_id=111&message_id=222
      • bare integer (msg-id only; caller must supply chat_ref separately)
    """
    text = (text or "").strip()

    # bare integer
    if text.isdigit():
        return None, int(text)

    # tg://openmessage?user_id=X&message_id=Y
    om = re.search(r"tg://openmessage\?([^\s]+)", text)
    if om:
        params = parse_qs(om.group(1))
        uid = params.get("user_id", [None])[0]
        mid = params.get("message_id", [None])[0]
        if uid and mid:
            return int(uid), int(mid)

    # https://t.me/c/CHATID/MSGID  or  https://t.me/username/MSGID
    m = re.search(r"https?://t\.me/(?:c/(-?\d+)|([A-Za-z0-9_]{4,}))/(\d+)", text)
    if m:
        chat_id_raw, username, msg_id = m.group(1), m.group(2), m.group(3)
        chat_ref = int(f"-100{chat_id_raw}") if chat_id_raw else username
        return chat_ref, int(msg_id)

    return None, None


async def _date_search(client, chat_ref, target_date, is_bot: bool):
    """
    Recover the original message ID for a bot-DM forward by searching the
    userbot's chat history with the source bot near target_date.

    Only works when a userbot session (not a bot token) is configured.
    Returns the message ID (int) or None.
    """
    if is_bot:
        return None  # bot tokens cannot access arbitrary DM histories

    try:
        # offset_date returns messages OLDER than the given date;
        # shift +2 s so the target message is included in the window.
        offset = target_date + timedelta(seconds=2)
        target_ts = target_date.timestamp()

        candidates = []
        async for msg in client.get_chat_history(
            chat_id=chat_ref,
            limit=10,
            offset_date=offset,
        ):
            candidates.append(msg)

        if not candidates:
            return None

        best = min(candidates, key=lambda m: abs(m.date.timestamp() - target_ts))
        # Accept only if within 3 seconds of the forwarded timestamp
        if abs(best.date.timestamp() - target_ts) <= 3:
            return best.id
    except Exception as e:
        logger.warning(f"fwd_bot _date_search: {e}")

    return None


async def _resolve_forward(msg, client, is_bot: bool):
    """
    Extract (chat_ref, msg_id, error_str) from a forwarded Pyrogram Message.

    Uses forward_origin (pyrofork's new API):
      - MessageOriginChannel  → chat + message_id available directly
      - MessageOriginUser     → sender_user available; message_id NOT in header
                                → fall back to date-based history search
      - MessageOriginChat     → group/channel without post id
      - MessageOriginHiddenUser → anonymous user, no id
    """
    origin = getattr(msg, "forward_origin", None)
    if origin is None:
        return None, None, "That is not a forwarded message. Please <b>forward</b> a message from the source bot."

    fwd_date = origin.date          # datetime of the original message
    origin_type = getattr(origin, "type", None)

    # ── Channel post ──────────────────────────────────────────────────────────
    if origin_type == enums.MessageOriginType.CHANNEL:
        chat = origin.chat
        chat_ref = chat.username if getattr(chat, "username", None) else chat.id
        msg_id = getattr(origin, "message_id", None)
        if msg_id:
            return chat_ref, msg_id, None
        return None, None, (
            "Could not read the message ID from that channel forward.\n"
            "The channel may have forward-protection enabled."
        )

    # ── Known user / bot ──────────────────────────────────────────────────────
    if origin_type == enums.MessageOriginType.USER:
        user = origin.sender_user
        # Prefer username so the userbot peer lookup works reliably
        chat_ref = getattr(user, "username", None) or user.id

        # Try history search (requires userbot)
        msg_id = await _date_search(client, chat_ref, fwd_date, is_bot)
        if msg_id:
            return chat_ref, msg_id, None

        # Fallback: ask user to provide it
        name = f"@{user.username}" if getattr(user, "username", None) else str(user.id)
        if is_bot:
            hint = (
                f"A <b>userbot session</b> is needed to auto-detect message IDs from "
                f"bot DM chats. Please add one in /settings.\n\n"
                f"Or send the message number (e.g. <code>92559</code>) or "
                f"<code>tg://openmessage</code> link."
            )
        else:
            hint = (
                f"Could not find the message in the chat history with <b>{name}</b> "
                f"(the message may be too old or the chat isn't accessible).\n\n"
                f"Please send the message number directly (e.g. <code>92559</code>) "
                f"or paste a <code>tg://openmessage?user_id=…&message_id=…</code> link."
            )
        return chat_ref, None, hint

    # ── Group chat (no post id) ───────────────────────────────────────────────
    if origin_type == enums.MessageOriginType.CHAT:
        chat = origin.sender_chat
        chat_ref = getattr(chat, "username", None) or chat.id
        return None, None, (
            f"This message is from a group chat (<b>{getattr(chat, 'title', chat_ref)}</b>). "
            f"Group messages don't have accessible post IDs. "
            f"Please send the message link instead."
        )

    # ── Hidden / anonymous user ───────────────────────────────────────────────
    return None, None, (
        "The original sender is hidden (anonymous forward). "
        "Please forward a message whose sender tag is visible."
    )


def _should_forward(msg, configs):
    """Return True if this message passes the user's /settings filter config."""
    f = configs.get("filters", {})
    if msg.video     and f.get("video",     True): return True
    if msg.document  and f.get("document",  True): return True
    if msg.photo     and f.get("photo",     True): return True
    if msg.audio     and f.get("audio",     True): return True
    if msg.voice     and f.get("voice",     True): return True
    if msg.animation and f.get("animation", True): return True
    if msg.sticker   and f.get("sticker",   True): return True
    if msg.text      and f.get("text",      True): return True
    return False


# ─── /fwd_bot command ────────────────────────────────────────────────────────

@Client.on_message(filters.private & filters.command(["fwd_bot"]))
async def fwd_bot_command(bot, message):
    user_id = message.from_user.id

    _bot_cfg = await db.get_bot(user_id)
    if not _bot_cfg:
        return await message.reply_text(
            "<b>You didn't add any bot/userbot. Please add one using /settings first.</b>"
        )

    channels = await db.get_user_channels(user_id)
    if not channels:
        return await message.reply_text(
            "<b>Please set a target chat in /settings before forwarding.</b>"
        )

    # ── Pick target channel ──────────────────────────────────────────────────
    if len(channels) > 1:
        buttons = [[KeyboardButton(ch["title"])] for ch in channels]
        buttons.append([KeyboardButton("cancel")])
        ch_ask = await bot.ask(
            message.chat.id,
            "<b>Choose your target chat:</b>",
            reply_markup=ReplyKeyboardMarkup(
                buttons, one_time_keyboard=True, resize_keyboard=True
            ),
        )
        if ch_ask.text and ch_ask.text.lower() in ("/cancel", "cancel"):
            return await message.reply_text(
                "<b>Process cancelled.</b>", reply_markup=ReplyKeyboardRemove()
            )
        toid = next(
            (ch["chat_id"] for ch in channels if ch["title"] == ch_ask.text), None
        )
        if not toid:
            return await message.reply_text(
                "<b>Wrong channel chosen!</b>", reply_markup=ReplyKeyboardRemove()
            )
    else:
        toid = channels[0]["chat_id"]

    is_bot_token = _bot_cfg.get("is_bot", True)

    # Start the client early so we can use it for date-based ID resolution
    try:
        client = await start_clone_bot(CLIENT.client(_bot_cfg))
    except Exception as e:
        return await message.reply_text(
            f"<b>Could not start your bot/userbot:</b> <code>{e}</code>"
        )

    # ── Step 1: First message ────────────────────────────────────────────────
    first_ask = await bot.ask(
        message.chat.id,
        "<b>❪ FWD BOT — Step 1 / 2 ❫</b>\n\n"
        "📩 <b>Forward the FIRST message</b> from the source bot "
        "(forward with the sender's tag — do <u>not</u> forward anonymously).\n\n"
        "/cancel — cancel",
        reply_markup=ReplyKeyboardRemove(),
    )
    if first_ask.text and first_ask.text.strip() == "/cancel":
        await _stop_client(client)
        return await first_ask.reply("<b>Process cancelled.</b>")

    chat_ref, first_id, err = await _resolve_forward(first_ask, client, is_bot_token)

    # If we got the chat_ref but not the msg_id, try accepting a typed value
    if chat_ref and not first_id and err:
        prompt = await first_ask.reply(
            f"<b>ℹ️ {err}</b>\n\n"
            f"<b>Source detected:</b> <code>{chat_ref}</code>\n"
            f"Please send the <b>first message number</b> now:"
        )
        num_ask = await bot.ask(message.chat.id, "Send the first message number:")
        if num_ask.text and num_ask.text.strip() == "/cancel":
            await _stop_client(client)
            return await num_ask.reply("<b>Process cancelled.</b>")
        _, first_id = _parse_any_link(num_ask.text or "")
        if not first_id:
            await _stop_client(client)
            return await num_ask.reply("<b>❌ Invalid number. Please restart /fwd_bot.</b>")

    elif err and not chat_ref:
        await _stop_client(client)
        return await first_ask.reply(f"<b>❌ {err}</b>")

    # ── Step 2: Last message ─────────────────────────────────────────────────
    last_ask = await bot.ask(
        message.chat.id,
        f"<b>❪ FWD BOT — Step 2 / 2 ❫</b>\n\n"
        f"✅ First message: <code>#{first_id}</code> from <code>{chat_ref}</code>\n\n"
        f"📩 Now <b>forward the LAST message</b> from the same source bot.\n\n"
        f"/cancel — cancel",
    )
    if last_ask.text and last_ask.text.strip() == "/cancel":
        await _stop_client(client)
        return await last_ask.reply("<b>Process cancelled.</b>")

    last_chat_ref, last_id, err2 = await _resolve_forward(last_ask, client, is_bot_token)

    if last_chat_ref and not last_id and err2:
        prompt2 = await last_ask.reply(
            f"<b>ℹ️ {err2}</b>\n\n"
            f"Please send the <b>last message number</b> now:"
        )
        num_ask2 = await bot.ask(message.chat.id, "Send the last message number:")
        if num_ask2.text and num_ask2.text.strip() == "/cancel":
            await _stop_client(client)
            return await num_ask2.reply("<b>Process cancelled.</b>")
        _, last_id = _parse_any_link(num_ask2.text or "")
        if not last_id:
            await _stop_client(client)
            return await num_ask2.reply("<b>❌ Invalid number. Please restart /fwd_bot.</b>")

    elif err2 and not last_chat_ref:
        await _stop_client(client)
        return await last_ask.reply(f"<b>❌ {err2}</b>")

    # Validate both from same source
    if last_chat_ref and str(chat_ref) != str(last_chat_ref):
        await _stop_client(client)
        return await last_ask.reply(
            f"<b>❌ Source mismatch!</b>\n"
            f"First from <code>{chat_ref}</code> but last from <code>{last_chat_ref}</code>.\n"
            f"Both must be from the same bot/chat."
        )

    if last_id < first_id:
        first_id, last_id = last_id, first_id

    total = last_id - first_id + 1

    # Stop client; it will be restarted in the confirm callback
    await _stop_client(client)

    # ── Confirm ──────────────────────────────────────────────────────────────
    confirm = await message.reply_text(
        f"<b>❪ CONFIRM FWD BOT ❫</b>\n\n"
        f"<b>Source:</b> <code>{chat_ref}</code>\n"
        f"<b>First ID:</b> <code>{first_id}</code>\n"
        f"<b>Last  ID:</b> <code>{last_id}</code>\n"
        f"<b>Total to scan:</b> <code>{total}</code>\n\n"
        f"Caption & filters follow your <b>/settings</b> config.\n\n"
        f"<b>Start forwarding?</b>",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes", callback_data=f"fwdbot_yes_{user_id}"),
            InlineKeyboardButton("❌ No",  callback_data="close_btn"),
        ]]),
    )

    temp.FWDX_JOBS[user_id] = {
        "type":     "fwd_bot",
        "chat_ref": chat_ref,
        "first_id": first_id,
        "last_id":  last_id,
        "toid":     toid,
        "msg_id":   confirm.id,
    }


# ─── Confirmation callback ────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^fwdbot_yes_"))
async def fwd_bot_confirm(bot, query):
    user_id = query.from_user.id
    data = temp.FWDX_JOBS.pop(user_id, None)

    if not data or data.get("type") != "fwd_bot" or data["msg_id"] != query.message.id:
        return await query.answer(
            "This confirmation has expired. Send /fwd_bot again.", show_alert=True
        )
    if temp.lock.get(user_id) and str(temp.lock.get(user_id)) == "True":
        return await query.answer(
            "Please wait until your previous task completes.", show_alert=True
        )

    toid = data["toid"]
    if toid in temp.IS_FRWD_CHAT:
        return await query.answer(
            "A task is already running for this target chat. Please wait.", show_alert=True
        )

    await query.answer()
    m = await query.message.edit_text("<code>Verifying your data, please wait...</code>")

    _bot_cfg = await db.get_bot(user_id)
    try:
        client = await start_clone_bot(CLIENT.client(_bot_cfg))
    except Exception as e:
        return await m.edit(f"<b>Error starting your bot/userbot:</b> <code>{e}</code>")

    try:
        k = await client.send_message(toid, "Testing")
        await k.delete()
    except Exception:
        await m.edit(
            "<b>Please make your bot/userbot admin in the target channel with full permissions.</b>"
        )
        return await _stop_client(client)

    chat_ref = data["chat_ref"]
    first_id = data["first_id"]
    last_id  = data["last_id"]

    configs     = await db.get_configs(user_id)
    caption     = configs.get("caption")
    forward_tag = configs.get("forward_tag", False)
    protect     = configs.get("protect", False)
    button      = parse_buttons(configs.get("button") or "")

    temp.forwardings += 1
    await db.add_frwd(user_id)
    temp.IS_FRWD_CHAT.append(toid)
    temp.lock[user_id]   = True
    temp.CANCEL[user_id] = False

    sleep    = 0.5 if _bot_cfg["is_bot"] else 3
    fetched  = forwarded = skipped = deleted = 0

    async def _update():
        try:
            await m.edit(
                f"<b>❪ FWD BOT — In Progress ❫</b>\n\n"
                f"<b>Source:</b> <code>{chat_ref}</code>\n"
                f"<b>Range:</b> <code>{first_id}</code> → <code>{last_id}</code>\n\n"
                f"Fetched: <b>{fetched}</b> | Forwarded: <b>{forwarded}</b> | "
                f"Skipped: <b>{skipped}</b> | Deleted: <b>{deleted}</b>\n\n"
                f"<i>Send /cancel to stop.</i>"
            )
        except Exception:
            pass

    async def _flush(batch):
        if not batch:
            return 0
        try:
            await client.forward_messages(
                chat_id=toid,
                from_chat_id=chat_ref,
                protect_content=protect,
                message_ids=batch,
                drop_author=not forward_tag,
            )
            return len(batch)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            return await _flush(batch)
        except Exception as e:
            logger.warning(f"fwd_bot flush: {e}")
            return 0

    try:
        MSG = []

        for chunk_start in range(first_id, last_id + 1, _CHUNK):
            if temp.CANCEL.get(user_id) is True:
                raise asyncio.CancelledError()

            ids = list(range(chunk_start, min(chunk_start + _CHUNK, last_id + 1)))
            try:
                result = await client.get_messages(chat_ref, ids)
                msgs = result if isinstance(result, list) else [result]
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
                continue
            except Exception as e:
                logger.warning(f"fwd_bot get_messages: {e}")
                await asyncio.sleep(1.5)
                continue

            for msg in msgs:
                fetched += 1
                if msg is None or getattr(msg, "empty", True) or msg.service:
                    deleted += 1
                    continue
                if not _should_forward(msg, configs):
                    skipped += 1
                    continue

                if not caption:
                    MSG.append(msg.id)
                    if len(MSG) >= 100:
                        forwarded += await _flush(MSG)
                        MSG.clear()
                        await asyncio.sleep(3)
                else:
                    _media   = media(msg)
                    _caption = custom_caption(msg, caption)
                    try:
                        if _media and _caption:
                            await client.send_cached_media(
                                chat_id=toid,
                                file_id=_media,
                                caption=_caption,
                                reply_markup=button,
                                protect_content=protect,
                            )
                        else:
                            await client.copy_message(
                                chat_id=toid,
                                from_chat_id=chat_ref,
                                message_id=msg.id,
                                caption=_caption,
                                reply_markup=button,
                                protect_content=protect,
                            )
                        forwarded += 1
                    except FloodWait as e:
                        await asyncio.sleep(e.value + 1)
                    except Exception as e:
                        logger.warning(f"fwd_bot copy: {e}")
                    await asyncio.sleep(sleep)

            await _update()
            await asyncio.sleep(0.35)

        if MSG:
            forwarded += await _flush(MSG)

    except asyncio.CancelledError:
        try:
            await m.edit(
                f"<b>❌ Forwarding cancelled.</b>\n\n"
                f"Fetched: {fetched} | Forwarded: {forwarded} | "
                f"Skipped: {skipped} | Deleted: {deleted}"
            )
        except Exception:
            pass
        temp.CANCEL[user_id] = False

    except Exception as e:
        logger.error(f"fwd_bot error: {e}")
        try:
            await m.edit(f"<b>Error:</b> <code>{e}</code>")
        except Exception:
            pass

    else:
        try:
            await m.edit(
                f"<b>🎉 FWD BOT — Completed!</b>\n\n"
                f"<b>Source:</b> <code>{chat_ref}</code>\n"
                f"<b>Range:</b> <code>{first_id}</code> → <code>{last_id}</code>\n\n"
                f"Fetched: <b>{fetched}</b> | Forwarded: <b>{forwarded}</b> | "
                f"Skipped: <b>{skipped}</b> | Deleted: <b>{deleted}</b>"
            )
        except Exception:
            pass

    # ── Cleanup ───────────────────────────────────────────────────────────────
    await _stop_client(client)
    await db.rmve_frwd(user_id)
    temp.forwardings -= 1
    temp.lock[user_id] = False
    if toid in temp.IS_FRWD_CHAT:
        temp.IS_FRWD_CHAT.remove(toid)


# ─── Utility ─────────────────────────────────────────────────────────────────

async def _stop_client(client):
    try:
        await client.stop()
    except Exception:
        pass
