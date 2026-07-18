import re
import asyncio
import logging

from database import db
from config import temp
from .test import CLIENT, start_clone_bot, parse_buttons
from .regix import custom_caption, media
from pyrogram import Client, filters
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

# Matches: https://t.me/username/123  or  https://t.me/c/1234567890/123
_TG_LINK_RE = re.compile(
    r"https?://t\.me/(?:c/(-?\d+)|([A-Za-z0-9_]{4,}))/(\d+)"
)


def _parse_msg_link(text):
    """
    Parse a Telegram message link.
    Returns (chat_ref, msg_id) where chat_ref is int or username str.
    Returns None on failure.
    """
    if not text:
        return None
    m = _TG_LINK_RE.search(text.strip())
    if not m:
        return None
    chat_id_raw, username, msg_id = m.group(1), m.group(2), m.group(3)
    if chat_id_raw:
        chat_ref = int(f"-100{chat_id_raw}")
    else:
        chat_ref = username
    return chat_ref, int(msg_id)


def _should_forward(msg, configs):
    """
    Return True if this message passes the user's /settings filter configuration.
    """
    filters_cfg = configs.get("filters", {})
    if msg.video       and filters_cfg.get("video",     True): return True
    if msg.document    and filters_cfg.get("document",  True): return True
    if msg.photo       and filters_cfg.get("photo",     True): return True
    if msg.audio       and filters_cfg.get("audio",     True): return True
    if msg.voice       and filters_cfg.get("voice",     True): return True
    if msg.animation   and filters_cfg.get("animation", True): return True
    if msg.sticker     and filters_cfg.get("sticker",   True): return True
    if msg.text        and filters_cfg.get("text",      True): return True
    return False


# ─── /fwd_bot command ────────────────────────────────────────────────────────

@Client.on_message(filters.private & filters.command(["fwd_bot"]))
async def fwd_bot_command(bot, message):
    user_id = message.from_user.id

    # Require a configured bot / userbot
    _bot = await db.get_bot(user_id)
    if not _bot:
        return await message.reply_text(
            "<b>You didn't add any bot/userbot. Please add one using /settings first.</b>"
        )

    # Require at least one target channel
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

    # ── Ask for FIRST message link ───────────────────────────────────────────
    first_ask = await bot.ask(
        message.chat.id,
        "<b>❪ FWD BOT ❫</b>\n\n"
        "Send the <b>first message link</b> from the bot.\n\n"
        "<b>Format:</b>\n"
        "• Public bot: <code>https://t.me/botusername/MESSAGE_ID</code>\n"
        "• Private chat: <code>https://t.me/c/CHAT_ID/MESSAGE_ID</code>\n\n"
        "/cancel — cancel this process",
        reply_markup=ReplyKeyboardRemove(),
    )
    if first_ask.text and first_ask.text.strip() == "/cancel":
        return await first_ask.reply("<b>Process cancelled.</b>")

    parsed_first = _parse_msg_link(first_ask.text or "")
    if not parsed_first:
        return await first_ask.reply(
            "<b>❌ Could not parse that link.</b>\n"
            "Use format: <code>https://t.me/botusername/123</code>"
        )
    chat_ref, first_id = parsed_first

    # ── Ask for LAST message link ────────────────────────────────────────────
    last_ask = await bot.ask(
        message.chat.id,
        "<b>Now send the <b>last message link</b> from the same bot.</b>\n\n"
        "<b>Format:</b>\n"
        "• Public bot: <code>https://t.me/botusername/MESSAGE_ID</code>\n"
        "• Private chat: <code>https://t.me/c/CHAT_ID/MESSAGE_ID</code>\n\n"
        "/cancel — cancel this process",
    )
    if last_ask.text and last_ask.text.strip() == "/cancel":
        return await last_ask.reply("<b>Process cancelled.</b>")

    parsed_last = _parse_msg_link(last_ask.text or "")
    if not parsed_last:
        return await last_ask.reply(
            "<b>❌ Could not parse that link.</b>\n"
            "Use format: <code>https://t.me/botusername/123</code>"
        )
    last_chat_ref, last_id = parsed_last

    # Both links must point to the same chat
    if str(chat_ref) != str(last_chat_ref):
        return await last_ask.reply(
            "<b>❌ Both links must be from the same bot/chat.</b>"
        )

    if last_id < first_id:
        return await last_ask.reply(
            "<b>❌ Last message ID must be ≥ first message ID.</b>"
        )

    total = last_id - first_id + 1

    # ── Confirmation prompt ──────────────────────────────────────────────────
    confirm = await message.reply_text(
        f"<b>❪ CONFIRM FWD BOT ❫</b>\n\n"
        f"<b>Source:</b> <code>{chat_ref}</code>\n"
        f"<b>First message ID:</b> <code>{first_id}</code>\n"
        f"<b>Last message ID:</b>  <code>{last_id}</code>\n"
        f"<b>Total IDs to scan:</b> <code>{total}</code>\n\n"
        f"Media filters and caption will follow your <b>/settings</b> config.\n\n"
        f"<b>Start forwarding?</b>",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes", callback_data=f"fwdbot_yes_{user_id}"),
            InlineKeyboardButton("❌ No",  callback_data="close_btn"),
        ]]),
    )

    # Store job data
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
            "A task is already running for this target chat. Please wait.",
            show_alert=True,
        )

    await query.answer()
    m = await query.message.edit_text("<code>Verifying your data, please wait...</code>")

    _bot = await db.get_bot(user_id)
    try:
        client = await start_clone_bot(CLIENT.client(_bot))
    except Exception as e:
        return await m.edit(f"<b>Error starting your bot/userbot:</b> <code>{e}</code>")

    # Verify write access to target channel
    try:
        k = await client.send_message(toid, "Testing")
        await k.delete()
    except Exception:
        await m.edit(
            "<b>Please make your bot/userbot admin in the target channel "
            "with full permissions.</b>"
        )
        try:
            await client.stop()
        except Exception:
            pass
        return

    chat_ref = data["chat_ref"]
    first_id = data["first_id"]
    last_id  = data["last_id"]

    configs      = await db.get_configs(user_id)
    caption      = configs.get("caption")
    forward_tag  = configs.get("forward_tag", False)
    protect      = configs.get("protect", False)
    button       = parse_buttons(configs.get("button") or "")

    # Mark as busy
    temp.forwardings += 1
    await db.add_frwd(user_id)
    temp.IS_FRWD_CHAT.append(toid)
    temp.lock[user_id]   = True
    temp.CANCEL[user_id] = False

    sleep = 0.5 if _bot["is_bot"] else 3
    fetched = forwarded = skipped = deleted = 0

    async def _update_progress():
        try:
            await m.edit(
                f"<b>❪ FWD BOT — In Progress ❫</b>\n\n"
                f"<b>Source:</b> <code>{chat_ref}</code>\n"
                f"<b>Range:</b> <code>{first_id}</code> → <code>{last_id}</code>\n\n"
                f"Fetched: <b>{fetched}</b> | Forwarded: <b>{forwarded}</b> | "
                f"Skipped: <b>{skipped}</b> | Deleted: <b>{deleted}</b>"
            )
        except Exception:
            pass

    async def _flush_batch(batch):
        """Forward a batch of message IDs and return how many were sent."""
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
            return await _flush_batch(batch)
        except Exception as e:
            logger.warning(f"fwd_bot forward error: {e}")
            return 0

    try:
        MSG = []  # batch accumulator (used when no custom caption)

        for chunk_start in range(first_id, last_id + 1, _CHUNK):
            if temp.CANCEL.get(user_id) is True:
                raise asyncio.CancelledError("cancelled by user")

            ids = list(range(chunk_start, min(chunk_start + _CHUNK, last_id + 1)))
            try:
                result = await client.get_messages(chat_ref, ids)
                msgs = result if isinstance(result, list) else [result]
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
                continue
            except Exception as e:
                logger.warning(f"fwd_bot get_messages error: {e}")
                await asyncio.sleep(1.5)
                continue

            for msg in msgs:
                fetched += 1
                if msg is None or getattr(msg, "empty", True):
                    deleted += 1
                    continue
                if msg.service:
                    deleted += 1
                    continue
                if not _should_forward(msg, configs):
                    skipped += 1
                    continue

                if not caption:
                    # Batch-forward mode
                    MSG.append(msg.id)
                    if len(MSG) >= 100:
                        forwarded += await _flush_batch(MSG)
                        MSG.clear()
                        await asyncio.sleep(3)
                else:
                    # Copy with custom caption
                    _media = media(msg)
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
                        logger.warning(f"fwd_bot copy error: {e}")
                    await asyncio.sleep(sleep)

            await _update_progress()
            await asyncio.sleep(0.35)

        # Flush remaining batch
        if MSG:
            forwarded += await _flush_batch(MSG)
            MSG.clear()

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

    # ── Cleanup ──────────────────────────────────────────────────────────────
    try:
        await client.stop()
    except Exception:
        pass
    await db.rmve_frwd(user_id)
    temp.forwardings -= 1
    temp.lock[user_id] = False
    if toid in temp.IS_FRWD_CHAT:
        temp.IS_FRWD_CHAT.remove(toid)
