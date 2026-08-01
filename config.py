"""
=====================================================================
 НАСТРОЙКА БОТА — редактируйте значения в блоке ниже
=====================================================================

Это самый простой способ настроить бота: просто впишите свои значения
прямо в этот файл и запустите `python main.py`.

Если вам удобнее хранить секреты отдельно от кода (например, чтобы не
закоммитить токен в git) — можно вместо этого использовать файл `.env`
(см. `.env.example`). Любая переменная, заданная в `.env` или в
переменных окружения системы, ИМЕЕТ ПРИОРИТЕТ над значениями из этого
файла — то есть можно спокойно указать здесь свои значения "по
умолчанию" для локального/домашнего использования, а на сервере
переопределить их через `.env`, ничего не трогая в самом коде.

Для Termux/обычного локального запуска достаточно просто отредактировать
переменные ниже — ничего больше настраивать не нужно.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()  # подхватит .env, если он есть рядом с этим файлом


# =====================================================================
# 1. ТОКЕН БОТА
# =====================================================================
# Получить токен: напишите @BotFather в Telegram -> /newbot -> следуйте
# инструкциям -> скопируйте выданный токен сюда.
BOT_TOKEN: str = "8907090036:AAFXLjSuDi-mqDlK1olyw6OeaVX1XbVUX4M"


# =====================================================================
# 2. ВЛАДЕЛЕЦ БОТА (уровень доступа 2 — максимальный)
# =====================================================================
# Владелец — это ВЫ. Единственный уровень доступа, который нельзя выдать
# или отозвать через саму панель бота — задаётся только здесь, в коде.
# Владелец имеет абсолютно все права: управление API-ключами, назначение
# и снятие администраторов (уровень 1), тарифы, подписки, рассылка,
# бан/разблокировка, заморозка подписок — всё без ограничений.
#
# Как узнать свой Telegram ID: напишите боту @userinfobot, либо запустите
# этого бота и отправьте ему команду /id.
#
# Пример с несколькими владельцами: OWNER_IDS = [111111111, 222222222]
OWNER_IDS: list[int] = [
    8187018755,  # <- замените на свой Telegram ID
]


# =====================================================================
# 3. АДМИНИСТРАТОРЫ (уровень доступа 1)
# =====================================================================
# В отличие от владельца, администраторы НАЗНАЧАЮТСЯ И СНИМАЮТСЯ прямо в
# боте владельцем (👑 Админ-панель → Управление админами) — здесь просто
# начальный список (по желанию, можно оставить пустым и назначать только
# через бота). У администраторов есть доступ ко всему, КРОМЕ управления
# API-ключами и назначения/снятия других администраторов — это может
# только владелец.
#
# Пример: ADMIN_IDS = [111111111, 222222222]
ADMIN_IDS: list[int] = [
    # 8187018755,
]


# =====================================================================
# 4. ПАРАМЕТРЫ БАЗЫ ДАННЫХ И СИСТЕМНЫЕ НАСТРОЙКИ
# =====================================================================
# Путь к файлу базы данных SQLite (создастся автоматически при первом запуске)
DB_PATH: str = "data/bot.db"


# =====================================================================
# 5. ПАРАМЕТРЫ ГЕНЕРАЦИИ ПО УМОЛЧАНИЮ
# =====================================================================
# Эти значения используются, если пользователь не переопределил их
# лично для себя через меню "⚙️ Настройки" в самом боте.
DEFAULT_TEMPERATURE: float = 0.7      # 0.0 - 2.0, чем выше — тем "креативнее" ответы
DEFAULT_TOP_P: float = 0.95           # 0.0 - 1.0
DEFAULT_MAX_TOKENS: int = 1024        # максимальная длина ответа модели

# Сколько последних сообщений диалога отправлять модели как контекст
HISTORY_LIMIT: int = 20

# Как часто (в секундах) обновлять сообщение в Telegram во время
# потоковой (streaming) генерации ответа. Меньше — плавнее, но больше
# запросов к Telegram API (может словить rate-limit при частом обновлении).
STREAM_EDIT_INTERVAL: float = 0.6

# Таймаут ожидания ответа от провайдера (NVIDIA Cloud API / NIM), секунд
REQUEST_TIMEOUT: float = 180.0


# =====================================================================
# 6. РЕЖИМ АГЕНТА (выполнение кода в песочнице + анализ zip-архивов)
# =====================================================================
# Максимальное число шагов ReAct-цикла (модель -> действие -> наблюдение -> ...)
# за одно сообщение пользователя. Защита от зацикливания модели.
AGENT_MAX_STEPS: int = 6

# Таймаут выполнения одного блока Python-кода в песочнице, секунд.
SANDBOX_TIMEOUT_SECONDS: float = 15.0

# Лимит памяти (RLIMIT_AS) для процесса с кодом пользователя, в мегабайтах.
SANDBOX_MEMORY_LIMIT_MB: int = 256

# Лимит процессорного времени (RLIMIT_CPU) для процесса с кодом, секунд.
SANDBOX_CPU_LIMIT_SECONDS: int = 10

# Требовать ли подтверждение пользователя (кнопки "Выполнить"/"Отклонить")
# перед КАЖДЫМ запуском сгенерированного моделью кода в песочнице. Это
# значение по умолчанию для НОВЫХ пользователей — каждый может переключить
# его лично себе в «⚙️ Настройки → 🔒 Подтверждение кода». Рекомендуется
# держать включенным, чтобы явно видеть и одобрять любой код перед запуском.
AGENT_CONFIRM_CODE_EXECUTION_DEFAULT: bool = True

# Сколько секунд бот ждёт нажатия кнопки "Выполнить/Отклонить", прежде чем
# автоматически расценить это как отказ от выполнения кода.
AGENT_CONFIRMATION_TIMEOUT_SECONDS: float = 300.0


# =====================================================================
# 7. УРОВЕНЬ "МЫШЛЕНИЯ" (REASONING / THINKING) МОДЕЛИ ПО УМОЛЧАНИЮ
# =====================================================================
# Многие модели на NVIDIA (DeepSeek-R1/V3, Nemotron, Qwen3, GPT-OSS и т.д.)
# поддерживают управляемый режим "рассуждений" (chain-of-thought), который
# можно включать/выключать и регулировать по уровню усилия. Каждый
# пользователь может переопределить это лично себе в
# «⚙️ Настройки → 🧠 Уровень мышления». Допустимые значения:
#   "off"    — не запрашивать рассуждения (быстрее и дешевле по токенам);
#   "low"    — минимальные рассуждения;
#   "medium" — сбалансированный режим (хорошее качество/скорость);
#   "high"   — максимально подробные рассуждения (медленнее, больше токенов).
# Если конкретная модель не поддерживает управление reasoning — параметр
# просто не будет иметь эффекта (бот передаёт его во всех распространённых
# форматах, которые понимают модели NVIDIA, см. providers.py).
#
# ВАЖНО: по умолчанию стоит "off". У reasoning-моделей (DeepSeek-R1, Nemotron
# и т.п.) токены на "размышления" тратятся из ТОГО ЖЕ бюджета max_tokens, что
# и сам ответ — включённый reasoning заметно замедляет ответы и при небольшом
# max_tokens может привести к ПУСТОМУ ответу (модель тратит весь лимит на
# размышления и не успевает написать сам текст). Включайте "low"/"medium"/"high"
# осознанно — под конкретные сложные задачи (математика, код, логика), и по
# возможности одновременно увеличивайте DEFAULT_MAX_TOKENS.
DEFAULT_REASONING_EFFORT: str = "off"


# =====================================================================
# Ниже — техническая часть, редактировать обычно не требуется.
# Значения из .env / переменных окружения перекрывают значения выше.
# =====================================================================


def _parse_id_list(raw: str) -> list[int]:
    ids: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.isdigit() or (chunk.startswith("-") and chunk[1:].isdigit()):
            ids.append(int(chunk))
    return ids


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _env_id_list(name: str, default: list[int]) -> list[int]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return list(default)
    return _parse_id_list(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


@dataclass
class Config:
    bot_token: str = field(default_factory=lambda: _env_str("BOT_TOKEN", BOT_TOKEN))
    owner_ids: set[int] = field(
        default_factory=lambda: set(_env_id_list("OWNER_IDS", OWNER_IDS))
    )
    admin_ids: set[int] = field(
        default_factory=lambda: set(_env_id_list("ADMIN_IDS", ADMIN_IDS))
    )
    db_path: str = field(default_factory=lambda: _env_str("DB_PATH", DB_PATH))

    default_temperature: float = field(
        default_factory=lambda: _env_float("DEFAULT_TEMPERATURE", DEFAULT_TEMPERATURE)
    )
    default_max_tokens: int = field(
        default_factory=lambda: _env_int("DEFAULT_MAX_TOKENS", DEFAULT_MAX_TOKENS)
    )
    default_top_p: float = field(
        default_factory=lambda: _env_float("DEFAULT_TOP_P", DEFAULT_TOP_P)
    )
    history_limit: int = field(
        default_factory=lambda: _env_int("HISTORY_LIMIT", HISTORY_LIMIT)
    )
    stream_edit_interval: float = field(
        default_factory=lambda: _env_float("STREAM_EDIT_INTERVAL", STREAM_EDIT_INTERVAL)
    )
    request_timeout: float = field(
        default_factory=lambda: _env_float("REQUEST_TIMEOUT", REQUEST_TIMEOUT)
    )

    agent_max_steps: int = field(
        default_factory=lambda: _env_int("AGENT_MAX_STEPS", AGENT_MAX_STEPS)
    )
    sandbox_timeout_seconds: float = field(
        default_factory=lambda: _env_float("SANDBOX_TIMEOUT_SECONDS", SANDBOX_TIMEOUT_SECONDS)
    )
    sandbox_memory_limit_mb: int = field(
        default_factory=lambda: _env_int("SANDBOX_MEMORY_LIMIT_MB", SANDBOX_MEMORY_LIMIT_MB)
    )
    sandbox_cpu_limit_seconds: int = field(
        default_factory=lambda: _env_int("SANDBOX_CPU_LIMIT_SECONDS", SANDBOX_CPU_LIMIT_SECONDS)
    )
    agent_confirm_code_execution_default: bool = field(
        default_factory=lambda: _env_bool(
            "AGENT_CONFIRM_CODE_EXECUTION_DEFAULT", AGENT_CONFIRM_CODE_EXECUTION_DEFAULT
        )
    )
    agent_confirmation_timeout_seconds: float = field(
        default_factory=lambda: _env_float(
            "AGENT_CONFIRMATION_TIMEOUT_SECONDS", AGENT_CONFIRMATION_TIMEOUT_SECONDS
        )
    )
    default_reasoning_effort: str = field(
        default_factory=lambda: _env_str("DEFAULT_REASONING_EFFORT", DEFAULT_REASONING_EFFORT)
    )

    def validate(self) -> None:
        placeholder_tokens = {"", "ВСТАВЬТЕ_СЮДА_ТОКЕН_ОТ_BOTFATHER"}
        if self.bot_token.strip() in placeholder_tokens:
            raise RuntimeError(
                "❗ BOT_TOKEN не задан.\n"
                "Откройте config.py и впишите свой токен от @BotFather в переменную BOT_TOKEN,\n"
                "либо укажите его в файле .env (переменная BOT_TOKEN)."
            )
        if not self.owner_ids:
            raise RuntimeError(
                "❗ OWNER_IDS не задан.\n"
                "Откройте config.py и впишите свой Telegram ID в список OWNER_IDS — это ваш\n"
                "личный ID владельца бота (максимальные права). Узнать свой ID можно у @userinfobot."
            )
        if 123456789 in self.owner_ids:
            raise RuntimeError(
                "❗ В OWNER_IDS всё ещё стоит пример-заглушка 123456789.\n"
                "Замените его на свой настоящий Telegram ID в config.py."
            )

    def is_owner(self, telegram_id: int) -> bool:
        return telegram_id in self.owner_ids

    def is_static_admin(self, telegram_id: int) -> bool:
        """Админ, заданный статично в config.py (ADMIN_IDS) — в дополнение к тем,
        кого владелец назначил динамически через бота (хранятся в БД, см. database.UserProfile)."""
        return telegram_id in self.admin_ids


config = Config()
