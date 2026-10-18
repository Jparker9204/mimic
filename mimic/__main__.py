import logging, sys, time
from . import config


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not config.BOT_TOKEN:
        logging.error("MIMIC_BOT_TOKEN is not set. Run: sudo mimic-set-token")
        time.sleep(30)
        return 1
    from .bot import MimicBot
    bot = MimicBot()
    logging.info("Mimic starting (db=%s, custom_rpc=%s)", config.DB_PATH, bool(config.CUSTOM_RPC))
    bot.build_app(config.BOT_TOKEN).run_polling(drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
