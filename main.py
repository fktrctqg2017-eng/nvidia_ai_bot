"""Точка входа Telegram-бота.

Запуск: python main.py
Конфигурация — через переменные окружения / файл .env (см. .env.example).
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import Database
from handlers import get_root_router
from middlewares import AccessControlMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("nvidia_ai_bot")


async def main() -> None:
    config.validate()

    db = Database(config.db_path)
    await db.connect()
    logger.info("База данных инициализирована: %s", config.db_path)

    purged = await db.purge_expired_subscriptions()
    if purged:
        logger.info("Удалено истёкших подписок при старте: %s", purged)

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Прокидываем зависимости во все хендлеры через workflow_data
    dp["db"] = db
    dp["config"] = config

    access_middleware = AccessControlMiddleware(db, config)
    dp.message.middleware(access_middleware)
    dp.callback_query.middleware(access_middleware)

    dp.include_router(get_root_router())

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        me = await bot.get_me()
        logger.info("Бот запущен: @%s (id=%s)", me.username, me.id)
        logger.info("Администраторы: %s", config.admin_ids)
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
