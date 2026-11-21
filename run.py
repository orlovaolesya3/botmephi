import asyncio
import logging

import uvicorn

import tg_bot
import admin as admin_module

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


async def run_bot():
    try:
        await tg_bot.main()
    except Exception as e:
        logging.exception(f"bot crashed: {e}")


async def run_admin():
    try:
        config = uvicorn.Config(
            admin_module.app,
            host="0.0.0.0",
            port=8000,
            log_level="info",
        )
        server = uvicorn.Server(config)
        await server.serve()
    except Exception as e:
        logging.exception(f"admin crashed: {e}")


async def main():
    await asyncio.gather(run_bot(), run_admin())


if __name__ == "__main__":
    asyncio.run(main())