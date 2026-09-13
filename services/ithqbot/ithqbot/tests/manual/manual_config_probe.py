from ithqbot.config.loader import load_config
from pathlib import Path
config = load_config(Path("./config.json"))
icatmsg = getattr(config.channels, "icatmsg", None)
print(type(icatmsg))
print(icatmsg)
