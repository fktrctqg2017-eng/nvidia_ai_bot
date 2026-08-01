"""Middleware, которое на каждое обновление вычисляет и прокидывает в хендлеры
роль пользователя (0/1/2), флаг активной подписки и профиль. Само по себе оно
НИЧЕГО не блокирует, кроме забаненных пользователей — им бот не отвечает
вообще (см. ниже), а обычная логика доступа к разделам/диалогу проверяется
точечно в хендлерах (см. subscriptions.py).

В data прокидываются:
    is_owner       — bool, ровно уровень 2;
    is_admin       — bool, уровень 1 ИЛИ 2 (админ или владелец) — сохраняет
                     совместимость со всем существующим кодом, где is_admin
                     означал "полный доступ";
    role_level     — int, 0/1/2, для мест, где нужно явно различать админа и владельца;
    is_subscriber  — bool, есть ли активная подписка (обычная/кастомная/admin-виртуальная);
    user_plan      — subscriptions.EffectivePlan | None.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

import roles
import subscriptions
from config import Config
from database import Database


class AccessControlMiddleware(BaseMiddleware):
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        user_id = user.id
        role = await roles.get_user_role(self.db, self.config, user_id)

        data["is_owner"] = role.is_owner
        data["is_admin"] = role.is_admin
        data["role_level"] = role.level

        # Забаненный пользователь не должен получать вообще никакого ответа
        # от бота (кроме как быть тихо проигнорированным) — владелец/админы
        # неприкасаемы для бана на уровне интерфейса (см. handlers/admin.py),
        # но проверяем и здесь на случай рассинхронизации данных.
        if not role.is_admin:
            profile = await self.db.get_user_profile(user_id)
            if profile is not None and profile.is_banned:
                if isinstance(event, CallbackQuery):
                    await event.answer("🚫 Вы заблокированы в этом боте.", show_alert=True)
                # Сообщения от забаненных просто игнорируются молча.
                return None

        plan = await subscriptions.get_user_plan(self.db, user_id, is_admin=role.is_admin)
        data["is_subscriber"] = plan is not None
        data["user_plan"] = plan

        return await handler(event, data)
