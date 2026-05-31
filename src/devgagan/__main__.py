import os
import sys
import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
USER_SESSION = os.environ.get("USER_SESSION")


async def run_userbot():
    if not USER_SESSION:
        logger.info("USER_SESSION not set. Userbot (devgagan2) is idle.")
        while True:
            await asyncio.sleep(3600)

    if not API_ID or not API_HASH:
        logger.error("API_ID or API_HASH not set. Cannot start userbot. Idling.")
        while True:
            await asyncio.sleep(3600)

    try:
        from pyrogram import Client, idle

        logger.info("Starting userbot session...")
        async with Client(
            name="userbot",
            api_id=int(API_ID),
            api_hash=API_HASH,
            session_string=USER_SESSION,
        ) as userbot:
            me = await userbot.get_me()
            logger.info(f"Userbot started as @{me.username} (ID: {me.id})")
            await idle()
    except Exception as e:
        logger.error(f"Userbot failed to start: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run_userbot())
