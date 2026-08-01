"""Единая точка определения уровня доступа пользователя.

Три уровня:
    0 — обычный пользователь;
    1 — администратор (назначается владельцем через бота, хранится в БД
        user_profiles.is_bot_admin, ЛИБО статично прописан в config.py -> ADMIN_IDS);
    2 — владелец (только config.py -> OWNER_IDS, никогда не хранится в БД,
        никогда не назначается/не снимается через саму панель бота).

ROLE_USER / ROLE_ADMIN / ROLE_OWNER — константы уровня для сравнений в коде.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import Config
from database import Database

ROLE_USER = 0
ROLE_ADMIN = 1
ROLE_OWNER = 2

ROLE_LABELS = {
    ROLE_USER: "Пользователь",
    ROLE_ADMIN: "Администратор",
    ROLE_OWNER: "Владелец",
}


@dataclass
class UserRole:
    level: int
    is_owner: bool
    is_admin: bool  # True для admin И owner (owner тоже проходит все admin-проверки)

    @property
    def label(self) -> str:
        return ROLE_LABELS.get(self.level, "Пользователь")


async def get_user_role(db: Database, config: Config, telegram_id: int) -> UserRole:
    """Вычисляет актуальный уровень доступа пользователя на КАЖДЫЙ вызов —
    ничего не кешируется, поэтому снятие прав администратора (или владельца
    из config.py при следующем перезапуске бота) мгновенно отражается
    на реальных правах, включая автоматическое исчезновение admin-подписки
    (см. subscriptions.get_admin_plan_virtual)."""
    if config.is_owner(telegram_id):
        return UserRole(level=ROLE_OWNER, is_owner=True, is_admin=True)

    if config.is_static_admin(telegram_id):
        return UserRole(level=ROLE_ADMIN, is_owner=False, is_admin=True)

    profile = await db.get_user_profile(telegram_id)
    if profile is not None and profile.is_bot_admin:
        return UserRole(level=ROLE_ADMIN, is_owner=False, is_admin=True)

    return UserRole(level=ROLE_USER, is_owner=False, is_admin=False)
