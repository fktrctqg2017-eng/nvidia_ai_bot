"""Асинхронный слой работы с SQLite (aiosqlite).

Хранит:
- whitelist пользователей (кроме админов, у которых доступ всегда есть);
- API-ключи провайдеров (NVIDIA Cloud API / NVIDIA NIM) с флагом активности;
- персональные настройки пользователя (выбранный ключ, модель, температура и т.д.);
- историю переписки для контекста диалога.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS whitelist (
    telegram_id INTEGER PRIMARY KEY,
    added_by    INTEGER,
    added_at    INTEGER
);

CREATE TABLE IF NOT EXISTS api_keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    provider    TEXT NOT NULL,           -- 'nvidia_cloud' | 'nvidia_nim'
    api_key     TEXT NOT NULL,
    base_url    TEXT NOT NULL,
    model       TEXT,                    -- модель по умолчанию для ключа (можно менять из чата)
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_by  INTEGER,
    created_at  INTEGER,
    role        TEXT NOT NULL DEFAULT 'chat',  -- 'chat' | 'asr' | 'image_gen'
    model_mode  TEXT NOT NULL DEFAULT 'manual' -- 'manual' (одна фиксированная модель) | 'all_models' (пользователь выбирает из живого списка моделей провайдера)
);

CREATE TABLE IF NOT EXISTS user_settings (
    telegram_id             INTEGER PRIMARY KEY,
    active_key_id           INTEGER,
    model_override          TEXT,
    system_prompt           TEXT,
    temperature             REAL,
    top_p                   REAL,
    max_tokens              INTEGER,
    streaming               INTEGER NOT NULL DEFAULT 1,
    agent_mode              INTEGER NOT NULL DEFAULT 0,
    confirm_code_execution  INTEGER,   -- NULL = использовать значение по умолчанию из config.py
    reasoning_effort        TEXT,      -- NULL = использовать значение по умолчанию из config.py;
                                        -- 'off' | 'low' | 'medium' | 'high'
    active_asr_key_id       INTEGER,   -- ключ для распознавания голосовых сообщений (роль 'asr')
    active_image_key_id     INTEGER,   -- ключ для генерации изображений (роль 'image_gen')
    FOREIGN KEY (active_key_id) REFERENCES api_keys(id) ON DELETE SET NULL,
    FOREIGN KEY (active_asr_key_id) REFERENCES api_keys(id) ON DELETE SET NULL,
    FOREIGN KEY (active_image_key_id) REFERENCES api_keys(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_history_user ON history(telegram_id);

-- ---------------------------------------------------------------- подписки / тарифы

CREATE TABLE IF NOT EXISTS plans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,               -- название тарифа, например "Базовый"
    description     TEXT NOT NULL DEFAULT '',    -- описание, показывается пользователю в разделе "Подписки"
    price_per_month TEXT NOT NULL DEFAULT '',     -- цена в месяц как ТЕКСТ (например "199 руб." или "$5") — не участвует в расчётах
    allowed_key_ids TEXT NOT NULL DEFAULT '[]',   -- JSON-список ID разрешённых api_keys (какие модели доступны)
    rpm_limit       INTEGER,                      -- лимит запросов в минуту (NULL = без лимита)
    rph_limit       INTEGER,                      -- лимит запросов в час (NULL = без лимита)
    rpd_limit       INTEGER,                      -- лимит запросов в сутки (NULL = без лимита)
    is_active       INTEGER NOT NULL DEFAULT 1,   -- отключенный тариф не показывается в витрине и не может быть выдан
    created_at      INTEGER
);

CREATE TABLE IF NOT EXISTS subscriptions (
    telegram_id     INTEGER PRIMARY KEY,
    plan_id         INTEGER NOT NULL,
    granted_by      INTEGER,                      -- кто из админов выдал
    granted_at      INTEGER,
    expires_at      INTEGER,                       -- unix timestamp окончания подписки; NULL = бессрочно
    FOREIGN KEY (plan_id) REFERENCES plans(id) ON DELETE CASCADE
);

-- Счётчики запросов для лимитов rpm/rph/rpd. Храним отдельные окна, чтобы
-- можно было независимо проверять минутный/часовой/дневной лимит одним запросом.
CREATE TABLE IF NOT EXISTS request_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_request_log_user_time ON request_log(telegram_id, created_at);

-- Заявки на покупку подписки (кнопка "Хочу купить" в витрине тарифов)
CREATE TABLE IF NOT EXISTS purchase_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    plan_id     INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'handled'
    created_at  INTEGER,
    FOREIGN KEY (plan_id) REFERENCES plans(id) ON DELETE CASCADE
);

-- Настройки бота, редактируемые из админки (не из config.py) — например,
-- контакт для оплаты подписки (@username).
CREATE TABLE IF NOT EXISTS bot_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Точечные персональные переопределения доступа к конкретной модели для
-- ОТДЕЛЬНОГО пользователя — сверх того, что даёт его тариф. allowed=1 —
-- разрешить модель, даже если её нет в тарифе; allowed=0 — явно запретить,
-- даже если модель есть в тарифе.
CREATE TABLE IF NOT EXISTS user_key_overrides (
    telegram_id INTEGER NOT NULL,
    key_id      INTEGER NOT NULL,
    allowed     INTEGER NOT NULL,
    PRIMARY KEY (telegram_id, key_id)
);

-- Персональные переопределения лимитов запросов для отдельного пользователя,
-- поверх лимитов его тарифа. Наличие СТРОКИ означает, что переопределение
-- активно; NULL в конкретной колонке внутри существующей строки означает
-- "без ограничения по этому окну" (отличие от отсутствия строки вообще,
-- которое означает "использовать лимиты тарифа").
CREATE TABLE IF NOT EXISTS user_limit_overrides (
    telegram_id INTEGER PRIMARY KEY,
    rpm_limit   INTEGER,
    rph_limit   INTEGER,
    rpd_limit   INTEGER
);

-- Профиль пользователя: дата регистрации (первый /start), роль (админ
-- назначается владельцем через бота; владелец задаётся только в config.py
-- и в БД не хранится), блокировка, заморозка подписки.
CREATE TABLE IF NOT EXISTS user_profiles (
    telegram_id     INTEGER PRIMARY KEY,
    registered_at   INTEGER NOT NULL,
    is_bot_admin    INTEGER NOT NULL DEFAULT 0,  -- назначен ли администратором (уровень 1) владельцем
    is_banned       INTEGER NOT NULL DEFAULT 0,
    banned_reason   TEXT,
    is_frozen       INTEGER NOT NULL DEFAULT 0,  -- заморозка подписки: срок не идёт, доступ закрыт
    frozen_at       INTEGER
);

-- Кастомные (не связанные с общим тарифом) подписки, выданные конкретному
-- пользователю персонально администратором/владельцем — свой набор моделей
-- и лимитов, не относящийся ни к одному общему plans-тарифу.
CREATE TABLE IF NOT EXISTS custom_subscriptions (
    telegram_id     INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,               -- отображаемое название, например "VIP индивидуальный"
    allowed_key_ids TEXT NOT NULL DEFAULT '[]',
    rpm_limit       INTEGER,
    rph_limit       INTEGER,
    rpd_limit       INTEGER,
    granted_by      INTEGER,
    granted_at      INTEGER,
    expires_at      INTEGER
);

-- Рассылка: журнал запусков (для истории/статистики в админке).
CREATE TABLE IF NOT EXISTS broadcast_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_by       INTEGER,
    text          TEXT NOT NULL,
    total_targets INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER
);

-- Промокоды. Создавать/включать/отключать/удалять/редактировать может
-- ТОЛЬКО владелец. Администраторы и владелец сами промокоды использовать
-- не могут (см. promo_logic.redeem_promo_code). reward_type='plan' выдаёт
-- существующий тариф из таблицы plans; reward_type='custom' выдаёт
-- персональный набор моделей/лимитов (аналогично custom_subscriptions).
CREATE TABLE IF NOT EXISTS promo_codes (
    code                    TEXT PRIMARY KEY,             -- всегда в ВЕРХНЕМ регистре
    reward_type             TEXT NOT NULL,                 -- 'plan' | 'custom'
    plan_id                 INTEGER,                       -- для reward_type='plan'
    custom_name             TEXT,                          -- для reward_type='custom'
    custom_allowed_key_ids  TEXT NOT NULL DEFAULT '[]',
    custom_rpm_limit        INTEGER,
    custom_rph_limit        INTEGER,
    custom_rpd_limit        INTEGER,
    duration_days           INTEGER,                       -- срок ВЫДАВАЕМОЙ подписки в днях; NULL = бессрочно
    max_activations         INTEGER,                       -- сколько РАЗНЫХ пользователей могут активировать; NULL = без ограничения
    used_count              INTEGER NOT NULL DEFAULT 0,
    target_audience         TEXT NOT NULL DEFAULT 'all',   -- 'all' | 'subscribers_only'
    valid_from              INTEGER,                       -- с какого момента код действует; NULL = сразу
    valid_until             INTEGER,                       -- до какого момента код действует; NULL = бессрочно
    is_active               INTEGER NOT NULL DEFAULT 1,    -- ручное вкл/выкл владельцем
    created_by              INTEGER,
    created_at              INTEGER,
    FOREIGN KEY (plan_id) REFERENCES plans(id) ON DELETE SET NULL
);

-- Кто уже активировал какой промокод — защищает от повторной активации
-- ОДНИМ И ТЕМ ЖЕ пользователем (UNIQUE на пару код+пользователь).
CREATE TABLE IF NOT EXISTS promo_code_activations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT NOT NULL,
    telegram_id   INTEGER NOT NULL,
    activated_at  INTEGER,
    UNIQUE(code, telegram_id)
);

CREATE INDEX IF NOT EXISTS idx_promo_activations_code ON promo_code_activations(code);

-- Точечное ограничение моделей ВНУТРИ ключа с режимом 'all_models' (у ключа
-- может быть подключено сразу несколько моделей у провайдера) — отдельно
-- для тарифов и для кастомных подписок. Если для пары (owner_type, owner_id,
-- key_id) есть хотя бы одна строка — значит для этого ключа разрешены ТОЛЬКО
-- перечисленные модели, а не все модели ключа. Если строк для этой пары нет
-- вообще — действует прежнее поведение (весь ключ разрешён целиком, все его
-- модели). owner_type: 'plan' | 'custom' (custom использует telegram_id
-- пользователя как owner_id, т.к. custom_subscriptions однa на пользователя).
CREATE TABLE IF NOT EXISTS plan_key_model_restrictions (
    owner_type TEXT NOT NULL,     -- 'plan' | 'custom'
    owner_id   INTEGER NOT NULL,  -- plans.id ЛИБО telegram_id (для custom)
    key_id     INTEGER NOT NULL,
    model_name TEXT NOT NULL,     -- конкретная разрешённая модель этого ключа
    PRIMARY KEY (owner_type, owner_id, key_id, model_name)
);

CREATE INDEX IF NOT EXISTS idx_plan_key_model_restrictions_owner
    ON plan_key_model_restrictions(owner_type, owner_id, key_id);
"""


KEY_ROLE_CHAT = "chat"
KEY_ROLE_ASR = "asr"
KEY_ROLE_IMAGE_GEN = "image_gen"

# Режим выбора модели для ключа:
#   'manual'     — у ключа зафиксирована ОДНА модель (задаётся при добавлении/
#                  редактировании), она же используется по умолчанию;
#   'all_models' — при выборе этого ключа пользователю показывается ЖИВОЙ
#                  список моделей, реально подключённых к ключу у провайдера
#                  (через provider.list_models), и можно выбрать любую из них.
KEY_MODEL_MODE_MANUAL = "manual"
KEY_MODEL_MODE_ALL = "all_models"


@dataclass
class ApiKey:
    id: int
    name: str
    provider: str
    api_key: str
    base_url: str
    model: Optional[str]
    is_active: bool
    created_by: Optional[int]
    created_at: int
    role: str = KEY_ROLE_CHAT
    model_mode: str = KEY_MODEL_MODE_MANUAL


@dataclass
class UserSettings:
    telegram_id: int
    active_key_id: Optional[int]
    model_override: Optional[str]
    system_prompt: Optional[str]
    temperature: Optional[float]
    top_p: Optional[float]
    max_tokens: Optional[int]
    streaming: bool
    agent_mode: bool
    confirm_code_execution: Optional[bool]  # None = использовать значение по умолчанию из config.py
    reasoning_effort: Optional[str]  # None = использовать значение по умолчанию из config.py
    active_asr_key_id: Optional[int] = None
    active_image_key_id: Optional[int] = None


PURCHASE_STATUS_PENDING = "pending"
PURCHASE_STATUS_HANDLED = "handled"

# Ключ в bot_settings, под которым хранится контакт для оплаты подписки (@username)
SETTING_PAYMENT_CONTACT = "payment_contact"


@dataclass
class Plan:
    id: int
    name: str
    description: str
    price_per_month: str
    allowed_key_ids: list[int]
    rpm_limit: Optional[int]
    rph_limit: Optional[int]
    rpd_limit: Optional[int]
    is_active: bool
    created_at: int


@dataclass
class Subscription:
    telegram_id: int
    plan_id: int
    granted_by: Optional[int]
    granted_at: int
    expires_at: Optional[int]  # None = бессрочная подписка

    def is_expired(self, now: Optional[int] = None) -> bool:
        if self.expires_at is None:
            return False
        now = now if now is not None else int(time.time())
        return now >= self.expires_at


@dataclass
class PurchaseRequest:
    id: int
    telegram_id: int
    plan_id: int
    status: str
    created_at: int


@dataclass
class UserProfile:
    telegram_id: int
    registered_at: int
    is_bot_admin: bool
    is_banned: bool
    banned_reason: Optional[str]
    is_frozen: bool
    frozen_at: Optional[int]


@dataclass
class CustomSubscription:
    telegram_id: int
    name: str
    allowed_key_ids: list[int]
    rpm_limit: Optional[int]
    rph_limit: Optional[int]
    rpd_limit: Optional[int]
    granted_by: Optional[int]
    granted_at: int
    expires_at: Optional[int]

    def is_expired(self, now: Optional[int] = None) -> bool:
        if self.expires_at is None:
            return False
        now = now if now is not None else int(time.time())
        return now >= self.expires_at


@dataclass
class PromoCode:
    code: str
    reward_type: str  # 'plan' | 'custom'
    plan_id: Optional[int]
    custom_name: Optional[str]
    custom_allowed_key_ids: list[int]
    custom_rpm_limit: Optional[int]
    custom_rph_limit: Optional[int]
    custom_rpd_limit: Optional[int]
    duration_days: Optional[int]
    max_activations: Optional[int]
    used_count: int
    target_audience: str  # 'all' | 'subscribers_only'
    valid_from: Optional[int]
    valid_until: Optional[int]
    is_active: bool
    created_by: Optional[int]
    created_at: int

    def is_time_valid(self, now: Optional[int] = None) -> bool:
        now = now if now is not None else int(time.time())
        if self.valid_from is not None and now < self.valid_from:
            return False
        if self.valid_until is not None and now >= self.valid_until:
            return False
        return True

    def has_activations_left(self) -> bool:
        if self.max_activations is None:
            return True
        return self.used_count < self.max_activations


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        await self._run_migrations()

    async def _run_migrations(self) -> None:
        """Лёгкие миграции для баз данных, созданных более старой версией бота
        (например, добавление новых колонок в уже существующие таблицы)."""
        cur = await self.conn.execute("PRAGMA table_info(user_settings)")
        columns = {row["name"] for row in await cur.fetchall()}
        if "agent_mode" not in columns:
            await self.conn.execute(
                "ALTER TABLE user_settings ADD COLUMN agent_mode INTEGER NOT NULL DEFAULT 0"
            )
            await self.conn.commit()
        if "confirm_code_execution" not in columns:
            await self.conn.execute(
                "ALTER TABLE user_settings ADD COLUMN confirm_code_execution INTEGER"
            )
            await self.conn.commit()
        if "reasoning_effort" not in columns:
            await self.conn.execute(
                "ALTER TABLE user_settings ADD COLUMN reasoning_effort TEXT"
            )
            await self.conn.commit()
        if "active_asr_key_id" not in columns:
            await self.conn.execute(
                "ALTER TABLE user_settings ADD COLUMN active_asr_key_id INTEGER"
            )
            await self.conn.commit()
        if "active_image_key_id" not in columns:
            await self.conn.execute(
                "ALTER TABLE user_settings ADD COLUMN active_image_key_id INTEGER"
            )
            await self.conn.commit()

        cur = await self.conn.execute("PRAGMA table_info(api_keys)")
        key_columns = {row["name"] for row in await cur.fetchall()}
        if "role" not in key_columns:
            # Все уже существующие ключи (в т.ч. ваши ранее добавленные) автоматически
            # получают роль 'chat' — их поведение не меняется, они просто продолжают
            # использоваться в обычном чате как и раньше.
            await self.conn.execute(
                f"ALTER TABLE api_keys ADD COLUMN role TEXT NOT NULL DEFAULT '{KEY_ROLE_CHAT}'"
            )
            await self.conn.commit()
        if "model_mode" not in key_columns:
            # Все уже существующие ключи автоматически получают режим 'manual'
            # (одна фиксированная модель — та, что уже была указана в поле
            # model) — их поведение не меняется ни на йоту.
            await self.conn.execute(
                f"ALTER TABLE api_keys ADD COLUMN model_mode TEXT NOT NULL DEFAULT '{KEY_MODEL_MODE_MANUAL}'"
            )
            await self.conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "База данных не инициализирована"
        return self._conn

    # ---------------------------------------------------------------- whitelist

    async def add_to_whitelist(self, telegram_id: int, added_by: int) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO whitelist (telegram_id, added_by, added_at) VALUES (?, ?, ?)",
            (telegram_id, added_by, int(time.time())),
        )
        await self.conn.commit()

    async def remove_from_whitelist(self, telegram_id: int) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM whitelist WHERE telegram_id = ?", (telegram_id,)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def is_whitelisted(self, telegram_id: int) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM whitelist WHERE telegram_id = ?", (telegram_id,)
        )
        return await cur.fetchone() is not None

    async def list_whitelist(self) -> list[int]:
        cur = await self.conn.execute(
            "SELECT telegram_id FROM whitelist ORDER BY added_at"
        )
        rows = await cur.fetchall()
        return [r["telegram_id"] for r in rows]

    # ---------------------------------------------------------------- api keys

    async def add_api_key(
        self,
        name: str,
        provider: str,
        api_key: str,
        base_url: str,
        model: Optional[str],
        created_by: int,
        role: str = KEY_ROLE_CHAT,
        model_mode: str = KEY_MODEL_MODE_MANUAL,
    ) -> int:
        cur = await self.conn.execute(
            """INSERT INTO api_keys (name, provider, api_key, base_url, model, is_active, created_by, created_at, role, model_mode)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
            (name, provider, api_key, base_url, model, created_by, int(time.time()), role, model_mode),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def set_api_key_role(self, key_id: int, role: str) -> bool:
        cur = await self.conn.execute(
            "UPDATE api_keys SET role = ? WHERE id = ?", (role, key_id)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def set_api_key_model_mode(self, key_id: int, model_mode: str) -> bool:
        """Переключает режим модели ключа: 'manual' (одна фиксированная модель,
        заданная в поле model) или 'all_models' (пользователь каждый раз
        выбирает из живого списка моделей, которые реально подключены к
        ключу у провайдера — см. providers.list_models)."""
        cur = await self.conn.execute(
            "UPDATE api_keys SET model_mode = ? WHERE id = ?", (model_mode, key_id)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def set_api_key_model(self, key_id: int, model: Optional[str]) -> bool:
        """Меняет модель по умолчанию у уже существующего ключа (используется
        и в ручном режиме, и для запоминания последней выбранной модели ключа
        в режиме 'все модели')."""
        cur = await self.conn.execute(
            "UPDATE api_keys SET model = ? WHERE id = ?", (model, key_id)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def delete_api_key(self, key_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
        await self.conn.execute(
            "UPDATE user_settings SET active_key_id = NULL WHERE active_key_id = ?",
            (key_id,),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def set_api_key_active(self, key_id: int, active: bool) -> bool:
        cur = await self.conn.execute(
            "UPDATE api_keys SET is_active = ? WHERE id = ?", (int(active), key_id)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_api_key(self, key_id: int) -> Optional[ApiKey]:
        cur = await self.conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,))
        row = await cur.fetchone()
        return self._row_to_apikey(row) if row else None

    async def list_api_keys(self, only_active: bool = False, role: Optional[str] = None) -> list[ApiKey]:
        query = "SELECT * FROM api_keys"
        conditions = []
        params: list = []
        if only_active:
            conditions.append("is_active = 1")
        if role is not None:
            conditions.append("role = ?")
            params.append(role)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY id"
        cur = await self.conn.execute(query, params)
        rows = await cur.fetchall()
        return [self._row_to_apikey(r) for r in rows]

    @staticmethod
    def _row_to_apikey(row: aiosqlite.Row) -> ApiKey:
        return ApiKey(
            id=row["id"],
            name=row["name"],
            provider=row["provider"],
            api_key=row["api_key"],
            base_url=row["base_url"],
            model=row["model"],
            is_active=bool(row["is_active"]),
            created_by=row["created_by"],
            created_at=row["created_at"],
            role=row["role"] if "role" in row.keys() else KEY_ROLE_CHAT,
            model_mode=row["model_mode"] if "model_mode" in row.keys() else KEY_MODEL_MODE_MANUAL,
        )

    # ---------------------------------------------------------------- settings

    async def get_user_settings(self, telegram_id: int) -> UserSettings:
        cur = await self.conn.execute(
            "SELECT * FROM user_settings WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cur.fetchone()
        if row is None:
            await self.conn.execute(
                "INSERT INTO user_settings (telegram_id, streaming, agent_mode) VALUES (?, 1, 0)",
                (telegram_id,),
            )
            await self.conn.commit()
            return UserSettings(
                telegram_id=telegram_id,
                active_key_id=None,
                model_override=None,
                system_prompt=None,
                temperature=None,
                top_p=None,
                max_tokens=None,
                streaming=True,
                agent_mode=False,
                confirm_code_execution=None,
                reasoning_effort=None,
                active_asr_key_id=None,
                active_image_key_id=None,
            )
        raw_confirm = row["confirm_code_execution"]
        row_keys = row.keys()
        return UserSettings(
            telegram_id=row["telegram_id"],
            active_key_id=row["active_key_id"],
            model_override=row["model_override"],
            system_prompt=row["system_prompt"],
            temperature=row["temperature"],
            top_p=row["top_p"],
            max_tokens=row["max_tokens"],
            streaming=bool(row["streaming"]),
            agent_mode=bool(row["agent_mode"]),
            confirm_code_execution=None if raw_confirm is None else bool(raw_confirm),
            active_asr_key_id=row["active_asr_key_id"] if "active_asr_key_id" in row_keys else None,
            active_image_key_id=row["active_image_key_id"] if "active_image_key_id" in row_keys else None,
            reasoning_effort=row["reasoning_effort"],
        )

    async def update_user_settings(self, telegram_id: int, **fields) -> None:
        await self.get_user_settings(telegram_id)  # гарантируем наличие строки
        if not fields:
            return
        columns = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [telegram_id]
        await self.conn.execute(
            f"UPDATE user_settings SET {columns} WHERE telegram_id = ?", values
        )
        await self.conn.commit()

    # ---------------------------------------------------------------- history

    async def add_history_message(self, telegram_id: int, role: str, content: str) -> None:
        await self.conn.execute(
            "INSERT INTO history (telegram_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (telegram_id, role, content, int(time.time())),
        )
        await self.conn.commit()

    async def get_history(self, telegram_id: int, limit: int) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT role, content FROM history WHERE telegram_id = ? ORDER BY id DESC LIMIT ?",
            (telegram_id, limit),
        )
        rows = await cur.fetchall()
        rows.reverse()
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    async def clear_history(self, telegram_id: int) -> None:
        await self.conn.execute("DELETE FROM history WHERE telegram_id = ?", (telegram_id,))
        await self.conn.commit()

    # ---------------------------------------------------------------- planы (тарифы)

    async def create_plan(
        self,
        name: str,
        description: str,
        price_per_month: str,
        allowed_key_ids: list[int],
        rpm_limit: Optional[int],
        rph_limit: Optional[int],
        rpd_limit: Optional[int],
    ) -> int:
        cur = await self.conn.execute(
            """INSERT INTO plans (name, description, price_per_month, allowed_key_ids,
                                   rpm_limit, rph_limit, rpd_limit, is_active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (
                name, description, price_per_month, json.dumps(allowed_key_ids),
                rpm_limit, rph_limit, rpd_limit, int(time.time()),
            ),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def update_plan(self, plan_id: int, **fields) -> bool:
        if not fields:
            return False
        if "allowed_key_ids" in fields:
            fields["allowed_key_ids"] = json.dumps(fields["allowed_key_ids"])
        columns = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [plan_id]
        cur = await self.conn.execute(f"UPDATE plans SET {columns} WHERE id = ?", values)
        await self.conn.commit()
        return cur.rowcount > 0

    async def delete_plan(self, plan_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
        await self.conn.execute("DELETE FROM subscriptions WHERE plan_id = ?", (plan_id,))
        await self.conn.execute(
            "DELETE FROM plan_key_model_restrictions WHERE owner_type = 'plan' AND owner_id = ?", (plan_id,)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_plan(self, plan_id: int) -> Optional[Plan]:
        cur = await self.conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,))
        row = await cur.fetchone()
        return self._row_to_plan(row) if row else None

    async def list_plans(self, only_active: bool = False) -> list[Plan]:
        query = "SELECT * FROM plans"
        if only_active:
            query += " WHERE is_active = 1"
        query += " ORDER BY id"
        cur = await self.conn.execute(query)
        rows = await cur.fetchall()
        return [self._row_to_plan(r) for r in rows]

    async def set_plan_active(self, plan_id: int, active: bool) -> bool:
        cur = await self.conn.execute(
            "UPDATE plans SET is_active = ? WHERE id = ?", (int(active), plan_id)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    @staticmethod
    def _row_to_plan(row: aiosqlite.Row) -> Plan:
        try:
            allowed = json.loads(row["allowed_key_ids"] or "[]")
        except (json.JSONDecodeError, TypeError):
            allowed = []
        return Plan(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            price_per_month=row["price_per_month"],
            allowed_key_ids=allowed,
            rpm_limit=row["rpm_limit"],
            rph_limit=row["rph_limit"],
            rpd_limit=row["rpd_limit"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
        )

    # ---------------------------------------------------------------- подписки

    async def grant_subscription(
        self, telegram_id: int, plan_id: int, granted_by: int, duration_days: Optional[int]
    ) -> None:
        """Выдаёт (или продлевает/меняет) подписку пользователю.
        duration_days=None -> бессрочная подписка."""
        now = int(time.time())
        expires_at = None if duration_days is None else now + duration_days * 86400
        # Обычная (plans) подписка и кастомная — взаимоисключающие: выдача
        # обычной ОБЯЗАНА отозвать кастомную (симметрично тому, как
        # grant_custom_subscription отзывает обычную) — иначе у пользователя
        # оказываются одновременно активны ДВЕ подписки разных типов, что
        # приводит к рассинхронизации данных (например, в профиле и в
        # эффективном тарифе будут видны разные, противоречащие друг другу
        # подписки).
        await self.revoke_custom_subscription(telegram_id)
        await self.conn.execute(
            """INSERT INTO subscriptions (telegram_id, plan_id, granted_by, granted_at, expires_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                   plan_id = excluded.plan_id,
                   granted_by = excluded.granted_by,
                   granted_at = excluded.granted_at,
                   expires_at = excluded.expires_at""",
            (telegram_id, plan_id, granted_by, now, expires_at),
        )
        await self.conn.commit()
        # Если пользователь в данный момент заморожен — новая подписка не должна
        # "тикать" в фоне (см. _resync_freeze_state — иначе после разморозки
        # восстановится устаревший остаток от предыдущей подписки).
        await self._resync_freeze_state(telegram_id)

    async def revoke_subscription(self, telegram_id: int) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM subscriptions WHERE telegram_id = ?", (telegram_id,)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def revoke_any_subscription(self, telegram_id: int) -> str:
        """Забирает у пользователя ЛЮБУЮ активную подписку — обычную (тариф)
        ИЛИ кастомную, какая бы из них ни была выдана. Раньше кнопка «Забрать
        подписку» вызывала только `revoke_subscription`, которая трогает лишь
        таблицу обычных тарифов — из-за этого кастомные подписки нельзя было
        отозвать вообще (баг). Дополнительно сбрасывает состояние заморозки:
        если подписки больше нет, замороженный "остаток срока" тоже не имеет
        смысла хранить — иначе он "утечёт" и исказит следующую выданную
        подписку (см. также freeze_subscription/unfreeze_subscription).
        Возвращает 'plan' | 'custom' | 'none' — что именно было отозвано."""
        revoked_kind = "none"
        if await self.revoke_subscription(telegram_id):
            revoked_kind = "plan"
        if await self.revoke_custom_subscription(telegram_id):
            revoked_kind = "custom" if revoked_kind == "none" else revoked_kind
        await self.conn.execute(
            "DELETE FROM bot_settings WHERE key = ?", (f"frozen_remaining_{telegram_id}",)
        )
        await self.set_user_frozen(telegram_id, False)
        await self.conn.commit()
        return revoked_kind

    async def get_subscription(self, telegram_id: int) -> Optional[Subscription]:
        cur = await self.conn.execute(
            "SELECT * FROM subscriptions WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cur.fetchone()
        return self._row_to_subscription(row) if row else None

    async def get_active_subscription(self, telegram_id: int) -> Optional[Subscription]:
        """Возвращает подписку, только если она ещё не истекла. Автоматически
        удаляет запись из БД, если срок действия истёк (самоочистка по обращению)."""
        sub = await self.get_subscription(telegram_id)
        if sub is None:
            return None
        if sub.is_expired():
            await self.revoke_subscription(telegram_id)
            return None
        return sub

    async def list_subscriptions(self) -> list[Subscription]:
        cur = await self.conn.execute("SELECT * FROM subscriptions ORDER BY telegram_id")
        rows = await cur.fetchall()
        return [self._row_to_subscription(r) for r in rows]

    async def purge_expired_subscriptions(self) -> int:
        """Удаляет все истёкшие подписки разом (полезно вызывать периодически)."""
        now = int(time.time())
        cur = await self.conn.execute(
            "DELETE FROM subscriptions WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)
        )
        await self.conn.commit()
        return cur.rowcount

    @staticmethod
    def _row_to_subscription(row: aiosqlite.Row) -> Subscription:
        return Subscription(
            telegram_id=row["telegram_id"],
            plan_id=row["plan_id"],
            granted_by=row["granted_by"],
            granted_at=row["granted_at"],
            expires_at=row["expires_at"],
        )

    # ---------------------------------------------------------------- лимиты запросов (rpm/rph/rpd)

    async def record_request(self, telegram_id: int) -> None:
        """Фиксирует факт одного запроса пользователя к модели (для подсчёта лимитов)."""
        await self.conn.execute(
            "INSERT INTO request_log (telegram_id, created_at) VALUES (?, ?)",
            (telegram_id, int(time.time())),
        )
        await self.conn.commit()

    async def count_requests_since(self, telegram_id: int, since_ts: int) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM request_log WHERE telegram_id = ? AND created_at >= ?",
            (telegram_id, since_ts),
        )
        row = await cur.fetchone()
        return row["cnt"] if row else 0

    async def cleanup_old_request_logs(self, older_than_seconds: int = 90000) -> int:
        """Удаляет старые записи журнала запросов, которые уже не нужны ни для
        одного из окон (минута/час/сутки) — вызывать периодически, чтобы таблица
        не росла бесконечно. По умолчанию хранит немного больше суток (25 часов)."""
        cutoff = int(time.time()) - older_than_seconds
        cur = await self.conn.execute("DELETE FROM request_log WHERE created_at < ?", (cutoff,))
        await self.conn.commit()
        return cur.rowcount

    # ---------------------------------------------------------------- заявки на покупку подписки

    async def create_purchase_request(self, telegram_id: int, plan_id: int) -> int:
        cur = await self.conn.execute(
            "INSERT INTO purchase_requests (telegram_id, plan_id, status, created_at) VALUES (?, ?, 'pending', ?)",
            (telegram_id, plan_id, int(time.time())),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def list_purchase_requests(self, only_pending: bool = True) -> list[PurchaseRequest]:
        query = "SELECT * FROM purchase_requests"
        if only_pending:
            query += " WHERE status = 'pending'"
        query += " ORDER BY created_at"
        cur = await self.conn.execute(query)
        rows = await cur.fetchall()
        return [
            PurchaseRequest(
                id=r["id"], telegram_id=r["telegram_id"], plan_id=r["plan_id"],
                status=r["status"], created_at=r["created_at"],
            )
            for r in rows
        ]

    async def mark_purchase_request_handled(self, request_id: int) -> bool:
        cur = await self.conn.execute(
            "UPDATE purchase_requests SET status = 'handled' WHERE id = ?", (request_id,)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    # ---------------------------------------------------------------- общие настройки бота (bot_settings)

    async def get_bot_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        cur = await self.conn.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else default

    async def set_bot_setting(self, key: str, value: str) -> None:
        await self.conn.execute(
            """INSERT INTO bot_settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )
        await self.conn.commit()

    # ---------------------------------------------------------------- персональные переопределения доступа к моделям

    async def set_user_key_override(self, telegram_id: int, key_id: int, allowed: bool) -> None:
        await self.conn.execute(
            """INSERT INTO user_key_overrides (telegram_id, key_id, allowed) VALUES (?, ?, ?)
               ON CONFLICT(telegram_id, key_id) DO UPDATE SET allowed = excluded.allowed""",
            (telegram_id, key_id, int(allowed)),
        )
        await self.conn.commit()

    async def clear_user_key_override(self, telegram_id: int, key_id: int) -> bool:
        """Убирает персональное переопределение — доступ снова определяется только тарифом."""
        cur = await self.conn.execute(
            "DELETE FROM user_key_overrides WHERE telegram_id = ? AND key_id = ?",
            (telegram_id, key_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_user_key_overrides(self, telegram_id: int) -> dict[int, bool]:
        cur = await self.conn.execute(
            "SELECT key_id, allowed FROM user_key_overrides WHERE telegram_id = ?", (telegram_id,)
        )
        rows = await cur.fetchall()
        return {r["key_id"]: bool(r["allowed"]) for r in rows}

    # ---------------------------------------------------------------- персональные переопределения лимитов

    async def set_user_limit_override(
        self,
        telegram_id: int,
        rpm_limit: Optional[int] = None,
        rph_limit: Optional[int] = None,
        rpd_limit: Optional[int] = None,
    ) -> None:
        await self.conn.execute(
            """INSERT INTO user_limit_overrides (telegram_id, rpm_limit, rph_limit, rpd_limit)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                   rpm_limit = excluded.rpm_limit,
                   rph_limit = excluded.rph_limit,
                   rpd_limit = excluded.rpd_limit""",
            (telegram_id, rpm_limit, rph_limit, rpd_limit),
        )
        await self.conn.commit()

    async def clear_user_limit_override(self, telegram_id: int) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM user_limit_overrides WHERE telegram_id = ?", (telegram_id,)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_user_limit_override(self, telegram_id: int) -> Optional[dict]:
        cur = await self.conn.execute(
            "SELECT rpm_limit, rph_limit, rpd_limit FROM user_limit_overrides WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {"rpm_limit": row["rpm_limit"], "rph_limit": row["rph_limit"], "rpd_limit": row["rpd_limit"]}

    # ---------------------------------------------------------------- профиль пользователя (регистрация, роль, бан, заморозка)

    async def ensure_user_profile(self, telegram_id: int) -> UserProfile:
        """Создаёт профиль при первом обращении (регистрирует дату первого /start),
        либо возвращает уже существующий без изменений."""
        cur = await self.conn.execute(
            "SELECT * FROM user_profiles WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cur.fetchone()
        if row is None:
            now = int(time.time())
            await self.conn.execute(
                "INSERT INTO user_profiles (telegram_id, registered_at) VALUES (?, ?)",
                (telegram_id, now),
            )
            await self.conn.commit()
            return UserProfile(
                telegram_id=telegram_id, registered_at=now, is_bot_admin=False,
                is_banned=False, banned_reason=None, is_frozen=False, frozen_at=None,
            )
        return self._row_to_profile(row)

    async def get_user_profile(self, telegram_id: int) -> Optional[UserProfile]:
        cur = await self.conn.execute(
            "SELECT * FROM user_profiles WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cur.fetchone()
        return self._row_to_profile(row) if row else None

    async def list_all_user_ids(self) -> list[int]:
        """Все ID пользователей, которые хоть раз запускали /start — основа для рассылки."""
        cur = await self.conn.execute("SELECT telegram_id FROM user_profiles ORDER BY registered_at")
        rows = await cur.fetchall()
        return [r["telegram_id"] for r in rows]

    async def list_bot_admins(self) -> list[int]:
        cur = await self.conn.execute(
            "SELECT telegram_id FROM user_profiles WHERE is_bot_admin = 1 ORDER BY telegram_id"
        )
        rows = await cur.fetchall()
        return [r["telegram_id"] for r in rows]

    async def set_bot_admin(self, telegram_id: int, is_admin: bool) -> None:
        await self.ensure_user_profile(telegram_id)
        await self.conn.execute(
            "UPDATE user_profiles SET is_bot_admin = ? WHERE telegram_id = ?",
            (int(is_admin), telegram_id),
        )
        await self.conn.commit()

    async def set_user_banned(self, telegram_id: int, banned: bool, reason: Optional[str] = None) -> None:
        await self.ensure_user_profile(telegram_id)
        await self.conn.execute(
            "UPDATE user_profiles SET is_banned = ?, banned_reason = ? WHERE telegram_id = ?",
            (int(banned), reason if banned else None, telegram_id),
        )
        await self.conn.commit()

    async def set_user_frozen(self, telegram_id: int, frozen: bool) -> None:
        """Замораживает/размораживает подписку: пока заморожена, срок действия
        НЕ уменьшается (expires_at не трогаем), но доступ проверяется отдельно
        через is_frozen в subscriptions.py."""
        await self.ensure_user_profile(telegram_id)
        await self.conn.execute(
            "UPDATE user_profiles SET is_frozen = ?, frozen_at = ? WHERE telegram_id = ?",
            (int(frozen), int(time.time()) if frozen else None, telegram_id),
        )
        await self.conn.commit()

    @staticmethod
    def _row_to_profile(row: aiosqlite.Row) -> UserProfile:
        return UserProfile(
            telegram_id=row["telegram_id"],
            registered_at=row["registered_at"],
            is_bot_admin=bool(row["is_bot_admin"]),
            is_banned=bool(row["is_banned"]),
            banned_reason=row["banned_reason"],
            is_frozen=bool(row["is_frozen"]),
            frozen_at=row["frozen_at"],
        )

    # ---------------------------------------------------------------- заморозка подписки: "заморозить" срок действия

    async def freeze_subscription(self, telegram_id: int) -> bool:
        """Замораживает срок как обычной (plans), так и кастомной подписки:
        запоминает, сколько секунд оставалось до истечения на момент заморозки,
        и убирает expires_at (делает подписку временно "бессрочной" по хранению,
        но недоступной из-за is_frozen), чтобы при разморозке восстановить остаток."""
        sub = await self.get_subscription(telegram_id)
        custom = await self.get_custom_subscription(telegram_id)
        now = int(time.time())
        remaining = None
        if sub and sub.expires_at is not None:
            remaining = max(0, sub.expires_at - now)
            await self.conn.execute(
                "UPDATE subscriptions SET expires_at = NULL WHERE telegram_id = ?", (telegram_id,)
            )
        elif custom and custom.expires_at is not None:
            remaining = max(0, custom.expires_at - now)
            await self.conn.execute(
                "UPDATE custom_subscriptions SET expires_at = NULL WHERE telegram_id = ?", (telegram_id,)
            )
        if remaining is not None:
            await self.set_bot_setting(f"frozen_remaining_{telegram_id}", str(remaining))
        await self.set_user_frozen(telegram_id, True)
        await self.conn.commit()
        return True

    async def _resync_freeze_state(self, telegram_id: int) -> None:
        """Поддерживает согласованность заморозки при ЛЮБОМ изменении подписки
        (выдача новой/другой подписки, отзыв) для уже замороженного пользователя.

        Раньше при выдаче НОВОЙ подписки пользователю, который в этот момент
        был заморожен, новая подписка продолжала "тикать" (её expires_at не
        трогали), хотя пользователь оставался помечен как is_frozen. При
        последующей разморозке восстанавливался СТАРЫЙ сохранённый остаток от
        прошлой подписки (действовавшей на момент заморозки), а не от только
        что выданной — из-за этого срок "расходился" (например, пользователю
        только что выдали подписку на 5 часов, а после разморозки внезапно
        показывался срок от совсем другой, уже неактуальной подписки).

        Эта функция вызывается после КАЖДОГО grant_subscription /
        grant_custom_subscription / revoke_any_subscription и:
          - если подписки больше нет вообще — снимает заморозку (замораживать
            нечего) и удаляет устаревший сохранённый остаток;
          - если у активной подписки expires_at ещё "тикает" (то есть кто-то
            выдал/поменял её, не учтя, что пользователь заморожен) — немедленно
            ставит её на паузу (как freeze_subscription) с АКТУАЛЬНЫМ остатком,
            заменяя устаревшее сохранённое значение;
          - если новая подписка бессрочная (expires_at IS NULL) — сохранённый
            остаток больше не нужен, удаляет его (при разморозке бессрочная
            подписка так и останется бессрочной, восстанавливать нечего).
        """
        profile = await self.get_user_profile(telegram_id)
        if profile is None or not profile.is_frozen:
            return

        now = int(time.time())
        sub = await self.get_subscription(telegram_id)
        custom = await self.get_custom_subscription(telegram_id)

        if sub is None and custom is None:
            # Подписки не осталось вообще — заморозка более неактуальна.
            await self.conn.execute(
                "DELETE FROM bot_settings WHERE key = ?", (f"frozen_remaining_{telegram_id}",)
            )
            await self.set_user_frozen(telegram_id, False)
            await self.conn.commit()
            return

        active_expires_at = sub.expires_at if sub is not None else custom.expires_at
        if active_expires_at is None:
            # Новая подписка бессрочная — раньше сохранённый остаток не имеет смысла.
            await self.conn.execute(
                "DELETE FROM bot_settings WHERE key = ?", (f"frozen_remaining_{telegram_id}",)
            )
            await self.conn.commit()
            return

        # У активной подписки expires_at всё ещё "тикает" — ставим её на паузу
        # немедленно, с АКТУАЛЬНЫМ остатком (перезаписывая устаревшее значение).
        remaining = max(0, active_expires_at - now)
        if sub is not None:
            await self.conn.execute(
                "UPDATE subscriptions SET expires_at = NULL WHERE telegram_id = ?", (telegram_id,)
            )
        else:
            await self.conn.execute(
                "UPDATE custom_subscriptions SET expires_at = NULL WHERE telegram_id = ?", (telegram_id,)
            )
        await self.set_bot_setting(f"frozen_remaining_{telegram_id}", str(remaining))
        await self.conn.commit()

    async def unfreeze_subscription(self, telegram_id: int) -> bool:
        remaining_raw = await self.get_bot_setting(f"frozen_remaining_{telegram_id}")
        now = int(time.time())
        if remaining_raw is not None:
            remaining = int(remaining_raw)
            new_expiry = now + remaining
            sub = await self.get_subscription(telegram_id)
            custom = await self.get_custom_subscription(telegram_id)
            if sub:
                await self.conn.execute(
                    "UPDATE subscriptions SET expires_at = ? WHERE telegram_id = ?", (new_expiry, telegram_id)
                )
            elif custom:
                await self.conn.execute(
                    "UPDATE custom_subscriptions SET expires_at = ? WHERE telegram_id = ?", (new_expiry, telegram_id)
                )
            await self.conn.execute("DELETE FROM bot_settings WHERE key = ?", (f"frozen_remaining_{telegram_id}",))
        await self.set_user_frozen(telegram_id, False)
        await self.conn.commit()
        return True

    # ---------------------------------------------------------------- кастомные подписки

    async def grant_custom_subscription(
        self,
        telegram_id: int,
        name: str,
        allowed_key_ids: list[int],
        rpm_limit: Optional[int],
        rph_limit: Optional[int],
        rpd_limit: Optional[int],
        granted_by: int,
        duration_days: Optional[int],
    ) -> None:
        now = int(time.time())
        expires_at = None if duration_days is None else now + duration_days * 86400
        # Кастомная подписка исключает обычную (plans) и наоборот — один активный тип за раз.
        await self.revoke_subscription(telegram_id)
        # При ПОВТОРНОЙ выдаче кастомной подписки тому же пользователю старые
        # точечные ограничения моделей (см. plan_key_model_restrictions) могли
        # относиться к ключам, которых уже нет в новом allowed_key_ids —
        # чистим их, чтобы не копить "мусор" от прошлых выдач.
        await self.clear_all_key_model_restrictions_for_owner("custom", telegram_id)
        await self.conn.execute(
            """INSERT INTO custom_subscriptions
                   (telegram_id, name, allowed_key_ids, rpm_limit, rph_limit, rpd_limit, granted_by, granted_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                   name = excluded.name,
                   allowed_key_ids = excluded.allowed_key_ids,
                   rpm_limit = excluded.rpm_limit,
                   rph_limit = excluded.rph_limit,
                   rpd_limit = excluded.rpd_limit,
                   granted_by = excluded.granted_by,
                   granted_at = excluded.granted_at,
                   expires_at = excluded.expires_at""",
            (telegram_id, name, json.dumps(allowed_key_ids), rpm_limit, rph_limit, rpd_limit,
             granted_by, now, expires_at),
        )
        await self.conn.commit()
        # См. комментарий в grant_subscription — та же защита от "утечки"
        # устаревшего остатка при выдаче кастомной подписки замороженному пользователю.
        await self._resync_freeze_state(telegram_id)

    async def revoke_custom_subscription(self, telegram_id: int) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM custom_subscriptions WHERE telegram_id = ?", (telegram_id,)
        )
        await self.conn.execute(
            "DELETE FROM plan_key_model_restrictions WHERE owner_type = 'custom' AND owner_id = ?", (telegram_id,)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def update_custom_subscription(self, telegram_id: int, **fields) -> bool:
        """Точечное редактирование уже выданной кастомной подписки (название,
        набор моделей, лимиты) БЕЗ пересоздания записи — в отличие от повторного
        вызова grant_custom_subscription, здесь НЕ трогается expires_at/granted_at,
        поэтому изменение названия/моделей/лимитов не влияет на оставшийся срок
        действия подписки."""
        if not fields:
            return False
        if "allowed_key_ids" in fields:
            fields["allowed_key_ids"] = json.dumps(fields["allowed_key_ids"])
        columns = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [telegram_id]
        cur = await self.conn.execute(
            f"UPDATE custom_subscriptions SET {columns} WHERE telegram_id = ?", values
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def set_custom_subscription_duration(self, telegram_id: int, duration_days: Optional[int]) -> bool:
        """Меняет СРОК действия уже выданной кастомной подписки, отсчитывая
        заново от текущего момента (аналогично повторной выдаче через
        `grant_custom_subscription`, но не требует заново указывать
        название/модели/лимиты). Корректно учитывает заморозку: если
        подписка сейчас заморожена — новый срок сразу же ставится на паузу
        через `_resync_freeze_state`, чтобы при разморозке восстановился
        именно этот новый срок, а не устаревший."""
        now = int(time.time())
        expires_at = None if duration_days is None else now + duration_days * 86400
        cur = await self.conn.execute(
            "UPDATE custom_subscriptions SET expires_at = ? WHERE telegram_id = ?",
            (expires_at, telegram_id),
        )
        await self.conn.commit()
        if cur.rowcount > 0:
            await self._resync_freeze_state(telegram_id)
        return cur.rowcount > 0

    async def set_subscription_duration(self, telegram_id: int, duration_days: Optional[int]) -> bool:
        """Аналог `set_custom_subscription_duration`, но для ОБЫЧНОЙ (plans)
        подписки — меняет срок действия без изменения самого тарифа."""
        now = int(time.time())
        expires_at = None if duration_days is None else now + duration_days * 86400
        cur = await self.conn.execute(
            "UPDATE subscriptions SET expires_at = ? WHERE telegram_id = ?",
            (expires_at, telegram_id),
        )
        await self.conn.commit()
        if cur.rowcount > 0:
            await self._resync_freeze_state(telegram_id)
        return cur.rowcount > 0

    async def get_custom_subscription(self, telegram_id: int) -> Optional[CustomSubscription]:
        cur = await self.conn.execute(
            "SELECT * FROM custom_subscriptions WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cur.fetchone()
        return self._row_to_custom_subscription(row) if row else None

    async def get_active_custom_subscription(self, telegram_id: int) -> Optional[CustomSubscription]:
        sub = await self.get_custom_subscription(telegram_id)
        if sub is None:
            return None
        if sub.is_expired():
            await self.revoke_custom_subscription(telegram_id)
            return None
        return sub

    @staticmethod
    def _row_to_custom_subscription(row: aiosqlite.Row) -> CustomSubscription:
        try:
            allowed = json.loads(row["allowed_key_ids"] or "[]")
        except (json.JSONDecodeError, TypeError):
            allowed = []
        return CustomSubscription(
            telegram_id=row["telegram_id"],
            name=row["name"],
            allowed_key_ids=allowed,
            rpm_limit=row["rpm_limit"],
            rph_limit=row["rph_limit"],
            rpd_limit=row["rpd_limit"],
            granted_by=row["granted_by"],
            granted_at=row["granted_at"],
            expires_at=row["expires_at"],
        )

    # ---------------------------------------------------------------- ручной сброс лимитов

    async def reset_user_rate_limits(self, telegram_id: int) -> int:
        """Полностью очищает журнал запросов пользователя — все счётчики rpm/rph/rpd
        обнуляются немедленно (ручной сброс лимитов администратором)."""
        cur = await self.conn.execute("DELETE FROM request_log WHERE telegram_id = ?", (telegram_id,))
        await self.conn.commit()
        return cur.rowcount

    # ---------------------------------------------------------------- рассылка

    async def log_broadcast(
        self, sent_by: int, text: str, total_targets: int, success_count: int, fail_count: int
    ) -> int:
        cur = await self.conn.execute(
            """INSERT INTO broadcast_log (sent_by, text, total_targets, success_count, fail_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (sent_by, text, total_targets, success_count, fail_count, int(time.time())),
        )
        await self.conn.commit()
        return cur.lastrowid

    # ---------------------------------------------------------------- промокоды

    @staticmethod
    def _row_to_promo_code(row: aiosqlite.Row) -> "PromoCode":
        try:
            allowed = json.loads(row["custom_allowed_key_ids"] or "[]")
        except (json.JSONDecodeError, TypeError):
            allowed = []
        return PromoCode(
            code=row["code"],
            reward_type=row["reward_type"],
            plan_id=row["plan_id"],
            custom_name=row["custom_name"],
            custom_allowed_key_ids=allowed,
            custom_rpm_limit=row["custom_rpm_limit"],
            custom_rph_limit=row["custom_rph_limit"],
            custom_rpd_limit=row["custom_rpd_limit"],
            duration_days=row["duration_days"],
            max_activations=row["max_activations"],
            used_count=row["used_count"],
            target_audience=row["target_audience"],
            valid_from=row["valid_from"],
            valid_until=row["valid_until"],
            is_active=bool(row["is_active"]),
            created_by=row["created_by"],
            created_at=row["created_at"],
        )

    async def create_promo_code(
        self,
        code: str,
        reward_type: str,
        created_by: int,
        plan_id: Optional[int] = None,
        custom_name: Optional[str] = None,
        custom_allowed_key_ids: Optional[list[int]] = None,
        custom_rpm_limit: Optional[int] = None,
        custom_rph_limit: Optional[int] = None,
        custom_rpd_limit: Optional[int] = None,
        duration_days: Optional[int] = None,
        max_activations: Optional[int] = None,
        target_audience: str = "all",
        valid_from: Optional[int] = None,
        valid_until: Optional[int] = None,
    ) -> None:
        """Создаёт новый промокод. code нормализуется в ВЕРХНИЙ регистр.
        Бросает aiosqlite.IntegrityError, если код с таким названием уже есть
        (обрабатывается в хендлере — просим ввести другое название)."""
        code = code.strip().upper()
        await self.conn.execute(
            """INSERT INTO promo_codes (
                   code, reward_type, plan_id, custom_name, custom_allowed_key_ids,
                   custom_rpm_limit, custom_rph_limit, custom_rpd_limit,
                   duration_days, max_activations, used_count, target_audience,
                   valid_from, valid_until, is_active, created_by, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 1, ?, ?)""",
            (
                code, reward_type, plan_id, custom_name,
                json.dumps(custom_allowed_key_ids or []),
                custom_rpm_limit, custom_rph_limit, custom_rpd_limit,
                duration_days, max_activations, target_audience,
                valid_from, valid_until, created_by, int(time.time()),
            ),
        )
        await self.conn.commit()

    async def get_promo_code(self, code: str) -> Optional["PromoCode"]:
        cur = await self.conn.execute(
            "SELECT * FROM promo_codes WHERE code = ?", (code.strip().upper(),)
        )
        row = await cur.fetchone()
        return self._row_to_promo_code(row) if row else None

    async def list_promo_codes(self) -> list["PromoCode"]:
        cur = await self.conn.execute("SELECT * FROM promo_codes ORDER BY created_at DESC")
        rows = await cur.fetchall()
        return [self._row_to_promo_code(r) for r in rows]

    async def set_promo_code_active(self, code: str, is_active: bool) -> None:
        await self.conn.execute(
            "UPDATE promo_codes SET is_active = ? WHERE code = ?", (int(is_active), code.strip().upper())
        )
        await self.conn.commit()

    async def delete_promo_code(self, code: str) -> bool:
        code = code.strip().upper()
        cur = await self.conn.execute("DELETE FROM promo_codes WHERE code = ?", (code,))
        await self.conn.execute("DELETE FROM promo_code_activations WHERE code = ?", (code,))
        await self.conn.commit()
        return cur.rowcount > 0

    async def update_promo_code(self, code: str, **fields) -> None:
        """Точечное обновление отдельных полей промокода (используется в редактировании)."""
        if not fields:
            return
        code = code.strip().upper()
        columns = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [code]
        await self.conn.execute(f"UPDATE promo_codes SET {columns} WHERE code = ?", values)
        await self.conn.commit()

    async def has_user_activated_promo(self, code: str, telegram_id: int) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM promo_code_activations WHERE code = ? AND telegram_id = ?",
            (code.strip().upper(), telegram_id),
        )
        row = await cur.fetchone()
        return row is not None

    async def record_promo_activation(self, code: str, telegram_id: int) -> None:
        """Отмечает активацию промокода конкретным пользователем и увеличивает
        общий счётчик использований. UNIQUE(code, telegram_id) защищает от
        повторной активации одним и тем же пользователем на уровне БД."""
        code = code.strip().upper()
        await self.conn.execute(
            "INSERT INTO promo_code_activations (code, telegram_id, activated_at) VALUES (?, ?, ?)",
            (code, telegram_id, int(time.time())),
        )
        await self.conn.execute(
            "UPDATE promo_codes SET used_count = used_count + 1 WHERE code = ?", (code,)
        )
        await self.conn.commit()

    async def count_promo_activations(self, code: str) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM promo_code_activations WHERE code = ?", (code.strip().upper(),)
        )
        row = await cur.fetchone()
        return row["cnt"] if row else 0

    # ---------------------------------------------------------------- точечное ограничение моделей внутри ключа (all_models)

    async def set_key_model_restrictions(
        self, owner_type: str, owner_id: int, key_id: int, model_names: list[str]
    ) -> None:
        """Задаёт СПИСОК конкретных моделей ключа (режим 'all_models'),
        разрешённых для этого тарифа/кастомной подписки. Пустой список
        `model_names` означает то же самое, что и полное отсутствие
        ограничения (удаляет строки) — то есть "весь ключ целиком", а не
        "ни одной модели" (иначе было бы невозможно вручную снять точечное
        ограничение обратно на "все модели ключа" через UI)."""
        await self.conn.execute(
            "DELETE FROM plan_key_model_restrictions WHERE owner_type = ? AND owner_id = ? AND key_id = ?",
            (owner_type, owner_id, key_id),
        )
        for model_name in model_names:
            await self.conn.execute(
                """INSERT OR IGNORE INTO plan_key_model_restrictions (owner_type, owner_id, key_id, model_name)
                   VALUES (?, ?, ?, ?)""",
                (owner_type, owner_id, key_id, model_name),
            )
        await self.conn.commit()

    async def get_key_model_restrictions(self, owner_type: str, owner_id: int, key_id: int) -> list[str]:
        """Возвращает список конкретно разрешённых моделей этого ключа для
        данного тарифа/кастомной подписки. Пустой список означает, что
        точечного ограничения НЕТ — разрешён весь ключ целиком (все его модели)."""
        cur = await self.conn.execute(
            "SELECT model_name FROM plan_key_model_restrictions WHERE owner_type = ? AND owner_id = ? AND key_id = ?",
            (owner_type, owner_id, key_id),
        )
        rows = await cur.fetchall()
        return [r["model_name"] for r in rows]

    async def get_all_key_model_restrictions(self, owner_type: str, owner_id: int) -> dict[int, list[str]]:
        """Возвращает ВСЕ точечные ограничения моделей для данного владельца
        (тарифа/кастомной подписки) сразу, сгруппированные по key_id — удобно
        для построения UI и для проверки доступа без N+1 запросов."""
        cur = await self.conn.execute(
            "SELECT key_id, model_name FROM plan_key_model_restrictions WHERE owner_type = ? AND owner_id = ?",
            (owner_type, owner_id),
        )
        rows = await cur.fetchall()
        result: dict[int, list[str]] = {}
        for r in rows:
            result.setdefault(r["key_id"], []).append(r["model_name"])
        return result

    async def clear_key_model_restrictions(self, owner_type: str, owner_id: int, key_id: int) -> None:
        """Полностью снимает точечное ограничение — ключ снова разрешает ВСЕ
        свои модели (для режима 'all_models')."""
        await self.conn.execute(
            "DELETE FROM plan_key_model_restrictions WHERE owner_type = ? AND owner_id = ? AND key_id = ?",
            (owner_type, owner_id, key_id),
        )
        await self.conn.commit()

    async def clear_all_key_model_restrictions_for_owner(self, owner_type: str, owner_id: int) -> None:
        """Удаляет ВСЕ точечные ограничения владельца сразу — используется при
        удалении тарифа/отзыве кастомной подписки, чтобы не оставлять "хвосты"
        в этой служебной таблице."""
        await self.conn.execute(
            "DELETE FROM plan_key_model_restrictions WHERE owner_type = ? AND owner_id = ?",
            (owner_type, owner_id),
        )
        await self.conn.commit()
