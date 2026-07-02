import os
import asyncio
import logging
import tempfile

from database import db
from .test import CLIENT, start_clone_bot
from .topics import parse_range, get_topic_id, msg_url, topic_url
from pyrogram import Client, filters
from pyrogram.errors import FloodWait

logger = logging.getLogger(__name__)
CLIENT = CLIENT()

_CHUNK = 190       # ids per get_messages() call (pyrogram caps around 200)
_DELAY = 0.35      # seconds between chunks (flood-safe)
_NAME_DELAY = 0.2  # seconds between topic-name fetches


async def _get_scan_account(user_id, message):
    """Return a started Pyrogram client for the user's saved bot/userbot."""
    _bot = await db.get_bot(user_id)
    if not _bot:
        await message.reply_text(
            "<b>You didn't add any bot/userbot. Please add one using /settings first.</b>"
        )
        return None
    try:
        client = await start_clone_bot(CLIENT.client(_bot))
    except Exception as e:
        await message.reply_text(f"<b>Could not start your bot/userbot:</b> <code>{e}</code>")
        return None
    return client


async def _scan(acc, chat_ref, scan_start, scan_end, status_msg):
    """Fetch every message id in [scan_start, scan_end] in chunks.

    Returns {topic_id: {'min': int, 'max': int, 'name': None}}
    """
    topics = {}
    total = scan_end - scan_start + 1
    done = 0
    last_upd = -10

    for chunk_start in range(scan_start, scan_end + 1, _CHUNK):
        ids = list(range(chunk_start, min(chunk_start + _CHUNK, scan_end + 1)))
        try:
            result = await acc.get_messages(chat_ref, ids)
            msgs = result if isinstance(result, list) else [result]
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            continue
        except Exception as e:
            logger.warning(f"fbatch get_messages error: {e}")
            await asyncio.sleep(1.5)
            continue

        for msg in msgs:
            if msg is None or getattr(msg, "empty", True):
                continue
            if getattr(msg, "forum_topic_created", None) is not None:
                continue
            tid = get_topic_id(msg)
            if tid is None:
                continue
            mid = msg.id
            if tid not in topics:
                topics[tid] = {"min": mid, "max": mid, "name": None}
            else:
                if mid < topics[tid]["min"]:
                    topics[tid]["min"] = mid
                if mid > topics[tid]["max"]:
                    topics[tid]["max"] = mid

        done += len(ids)
        pct = done * 100 // total
        if pct >= last_upd + 10:
            last_upd = pct
            try:
                await status_msg.edit(
                    f"🔍 <b>Scanning...</b> {pct}% ({done}/{total} IDs)\n"
                    f"Topics found so far: <b>{len(topics)}</b>"
                )
            except Exception:
                pass

        await asyncio.sleep(_DELAY)

    return topics


async def _fetch_names(acc, chat_ref, topics):
    for tid in list(topics.keys()):
        try:
            msg = await acc.get_messages(chat_ref, tid)
            if msg and not getattr(msg, "empty", True):
                ftc = getattr(msg, "forum_topic_created", None)
                if ftc:
                    topics[tid]["name"] = (
                        getattr(ftc, "name", None) or getattr(ftc, "title", None)
                    )
        except Exception:
            pass
        await asyncio.sleep(_NAME_DELAY)


@Client.on_message(filters.private & filters.command(["fbatch"]))
async def fbatch_command(bot, message):
    user_id = message.from_user.id

    ask = await bot.ask(
        message.chat.id,
        "<b>❪ FORUM TOPIC SCANNER ❫</b>\n\n"
        "Send the <b>start-end link range</b> to scan this forum supergroup for topics.\n\n"
        "<b>Format:</b> <code>START_LINK-END_LINK</code>\n\n"
        "<b>Example:</b>\n"
        "<code>https://t.me/c/2932205861/116-https://t.me/c/2932205861/1642</code>\n\n"
        "/cancel - cancel this process",
    )
    if ask.text and ask.text.strip() == "/cancel":
        return await ask.reply("<b>Process cancelled.</b>")

    parsed = parse_range(ask.text or "")
    if not parsed:
        return await ask.reply(
            "<b>Could not parse that link range.</b>\n\n"
            "Use the format:\n<code>https://t.me/c/CHATID/MSGID-https://t.me/c/CHATID/MSGID</code>"
        )

    chat_ref, scan_start, scan_end = parsed
    if scan_end < scan_start:
        return await ask.reply("<b>End message ID must be >= start message ID.</b>")

    acc = await _get_scan_account(user_id, ask)
    if acc is None:
        return

    try:
        await acc.get_chat(chat_ref)
    except Exception as e:
        await ask.reply(
            f"<b>Could not access the source chat:</b> <code>{e}</code>\n"
            "Make sure your bot/userbot is a member of it."
        )
        try:
            await acc.stop()
        except Exception:
            pass
        return

    total_ids = scan_end - scan_start + 1
    status = await ask.reply(
        f"🔍 <b>Forum Topic Scanner</b> — started\n\n"
        f"Chat : <code>{chat_ref}</code>\n"
        f"Range: <code>{scan_start}</code> → <code>{scan_end}</code> ({total_ids} IDs)\n\n"
        f"⏳ Scanning..."
    )

    try:
        topics = await _scan(acc, chat_ref, scan_start, scan_end, status)
    except Exception as e:
        logger.error(f"fbatch scan: {e}")
        await status.edit(f"<b>Scan failed:</b> <code>{e}</code>")
        try:
            await acc.stop()
        except Exception:
            pass
        return

    if not topics:
        await status.edit(
            f"<b>No forum topics found</b> in range <code>{scan_start}</code> → <code>{scan_end}</code>.\n\n"
            "- Make sure the account can read this group.\n"
            "- Confirm it is a forum supergroup (Topics enabled).\n"
            "- All messages in range may be deleted."
        )
        try:
            await acc.stop()
        except Exception:
            pass
        return

    try:
        await status.edit(f"✅ Found <b>{len(topics)}</b> topic(s) — fetching names...")
        await _fetch_names(acc, chat_ref, topics)
    except Exception as e:
        logger.warning(f"fbatch names: {e}")
    finally:
        try:
            await acc.stop()
        except Exception:
            pass

    lines = [
        f"Forum Topic Scan Results\n"
        f"Chat     : {chat_ref}\n"
        f"Range    : {scan_start} -> {scan_end}  ({total_ids} IDs scanned)\n"
        f"Topics   : {len(topics)} active\n"
        f"{'=' * 55}\n"
    ]

    for tid in sorted(topics.keys()):
        info = topics[tid]
        name = info["name"] or f"Topic {tid}"
        first_mid = info["min"]
        last_mid = info["max"]

        lines.append(
            f"Topic  : {name}\n"
            f"ID     : {tid}\n"
            f"URL    : {topic_url(chat_ref, tid)}\n"
            f"First  : {msg_url(chat_ref, tid, first_mid)}\n"
            f"Last   : {msg_url(chat_ref, tid, last_mid)}\n"
            f"{'-' * 55}"
        )

    txt_content = "\n".join(lines)

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix=f"fbatch_{user_id}_")
        os.close(fd)
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(txt_content)

        caption = (
            f"✅ <b>{len(topics)} topics</b> found in range "
            f"<code>{scan_start}</code> → <code>{scan_end}</code>\n\n"
            f"Send this file to /fwdx to forward files from the listed topics."
        )
        await bot.send_document(message.chat.id, tmp_path, caption=caption)
    except Exception as e:
        logger.error(f"fbatch send_document: {e}")
        await message.reply(f"<b>Could not send result file:</b> <code>{e}</code>")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    try:
        await status.delete()
    except Exception:
        pass

