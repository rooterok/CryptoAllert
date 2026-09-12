import os
import logging

import aiohttp

logger = logging.getLogger("price-alerts-bot")

PUSHOVER_TOKEN = os.getenv("PUSHOVER_TOKEN")
PUSHOVER_USER = os.getenv("PUSHOVER_USER")
PUSHOVER_SOUND = os.getenv("PUSHOVER_SOUND")


async def send_pushover(title: str, message: str, priority: int = 0) -> None:
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        logger.warning("PUSHOVER_TOKEN/PUSHOVER_USER not set, skipping notification: %s", title)
        return
    data = {
        "token": PUSHOVER_TOKEN,
        "user": PUSHOVER_USER,
        "title": title,
        "message": message,
        "priority": str(priority),
    }
    if PUSHOVER_SOUND:
        data["sound"] = PUSHOVER_SOUND
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post("https://api.pushover.net/1/messages.json", data=data) as resp:
                if resp.status >= 300:
                    body = await resp.text()
                    logger.warning("Pushover error %s: %s", resp.status, body)
    except Exception as e:
        logger.warning("Failed to send Pushover notification: %s", e)
