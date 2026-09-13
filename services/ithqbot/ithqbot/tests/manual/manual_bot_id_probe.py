from ithqbot.config.loader import load_config
from pathlib import Path
config = load_config(Path("./config.json"))
icatmsg_config = getattr(config.channels, "icatmsg", None)
print("Before injection:", icatmsg_config)

if isinstance(icatmsg_config, dict) and "bot_id" not in icatmsg_config:
    if hasattr(config, "bot") and config.bot:
        icatmsg_config["bot_id"] = config.bot.id

print("After injection:", icatmsg_config)
