import os
import logging

import aiohttp

logger = logging.getLogger("price-alerts-bot")

PUSHOVER_TOKEN = os.getenv("PUSHOVER_TOKEN")
PUSHOVER_USER = os.getenv("PUSHOVER_USER")
PUSHOVER_SOUND = os.getenv("PUSHOVER_SOUND")

# Priority 2 = "Emergency": bypasses quiet hours / mute on iOS (a "critical
# alert"), repeats every PUSHOVER_RETRY_SECONDS until acknowledged or until
# PUSHOVER_EXPIRE_SECONDS passes. Pushover requires retry/expire only for
# priority 2 - see https://pushover.net/api#priority
DEFAULT_PRIORITY = int(os.getenv("PUSHOVER_PRIORITY", "2"))
PUSHOVER_RETRY_SECONDS = int(os.getenv("PUSHOVER_RETRY_SECONDS", "60"))
PUSHOVER_EXPIRE_SECONDS = int(os.getenv("PUSHOVER_EXPIRE_SECONDS", "3600"))


async def send_pushover(title: str, message: str, priority: int | None = None) -> None:
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        logger.warning("PUSHOVER_TOKEN/PUSHOVER_USER not set, skipping notification: %s", title)
        return
    priority = DEFAULT_PRIORITY if priority is None else priority
    data = {
        "token": PUSHOVER_TOKEN,
        "user": PUSHOVER_USER,
        "title": title,
        "message": message,
        "priority": str(priority),
    }
    if priority == 2:
        data["retry"] = str(PUSHOVER_RETRY_SECONDS)
        data["expire"] = str(PUSHOVER_EXPIRE_SECONDS)
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
