"""Бизнес-логика подписок: проверка доступа, лимитов запросов, форматирование
для UI. Вынесена отдельно от database.py (чистый доступ к данным) и от
handlers/*.py (представление), чтобы правила были в одном месте.

Модель доступа:
    - Whitelist полностью упразднён. Общаться с моделью и видеть персональные
      разделы (Мои модели, Настройки, Файлы) может только тот, у кого есть
      АКТИВНАЯ подписка — обычная (plans), кастомная (custom_subscriptions)
      или "виртуальная" admin-подписка (см. ниже).
    - Администраторы и владелец получают ОСОБУЮ подписку с id=ADMIN_PLAN_ID
      и названием "admin" — она НЕ хранится в БД, а вычисляется на лету по
      факту наличия прав администратора/владельца (roles.py). Бессрочная,
      без лимитов, доступны ВСЕ активные модели. Как только пользователь
      перестаёт быть админом — эта подписка мгновенно исчезает (следующий
      же вызов get_user_plan её больше не увидит), а обычная/кастомная
      подписка (если была отдельно выдана) продолжает действовать как обычно.
    - Забаненный пользователь не имеет доступа НИ К ЧЕМУ, что требует
      подписки, независимо от того, какая у него подписка.
    - Замороженная подписка сохраняет свой срок (expires_at ставится в
      паузу, см. database.freeze_subscription), но временно не даёт доступа.
    - Кастомная подписка и обычная (plans) — взаимоисключающие: выдача одной
      автоматически отзывает другую (см. database.grant_custom_subscription).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from database import ApiKey, CustomSubscription, Database, Plan, Subscription

NO_SUBSCRIPTION_TEXT = (
    "⛔ У вас нет активной подписки, поэтому доступ к диалогу с моделью закрыт.\n"
    "Откройте «💳 Подписки», чтобы выбрать и приобрести тариф."
)

# Окна для лимитов, в секундах (используются для проверки лимита "по скользящему
# окну"); для отображения точного времени сброса в профиле используются
# КАЛЕНДАРНЫЕ окна — см. calendar_window_bounds().
WINDOW_MINUTE = 60
WINDOW_HOUR = 3600
WINDOW_DAY = 86400

# Специальный ID виртуального admin-тарифа (никогда не существует в таблице plans).
ADMIN_PLAN_ID = -1
ADMIN_PLAN_NAME = "admin"


@dataclass
class EffectivePlan:
    """Единое представление "эффективного тарифа" пользователя — либо
    обычный Plan, либо CustomSubscription, либо виртуальный admin-план.
    Используется везде, где раньше использовался просто Plan.

    `key_model_restrictions` — точечное ограничение МОДЕЛЕЙ внутри ключа с
    режимом 'all_models' (у которого может быть подключено сразу несколько
    моделей у провайдера): словарь key_id -> список разрешённых ИМЁН моделей
    ИМЕННО для этого тарифа/подписки. Если для key_id нет записи в этом
    словаре — ограничения нет, разрешены ВСЕ модели ключа (прежнее поведение).
    `owner_type`/`owner_id` — откуда именно взят этот эффективный план,
    нужны, чтобы администратор мог редактировать точечные ограничения через
    admin-панель (см. handlers/admin.py)."""

    id: int
    name: str
    allowed_key_ids: list[int]
    rpm_limit: Optional[int]
    rph_limit: Optional[int]
    rpd_limit: Optional[int]
    is_admin_plan: bool = False
    is_custom: bool = False
    key_model_restrictions: dict[int, list[str]] = field(default_factory=dict)
    owner_type: Optional[str] = None
    owner_id: Optional[int] = None


@dataclass
class AccessCheckResult:
    allowed: bool
    reason: str = ""  # человекочитаемая причина отказа (для показа пользователю)


@dataclass
class RateLimitStatus:
    allowed: bool
    limit_hit: Optional[str] = None  # "минуту" | "час" | "сутки"
    retry_after_seconds: Optional[int] = None


async def _plan_to_effective(db: Database, plan: Plan) -> EffectivePlan:
    restrictions = await db.get_all_key_model_restrictions("plan", plan.id)
    return EffectivePlan(
        id=plan.id, name=plan.name, allowed_key_ids=plan.allowed_key_ids,
        rpm_limit=plan.rpm_limit, rph_limit=plan.rph_limit, rpd_limit=plan.rpd_limit,
        key_model_restrictions=restrictions, owner_type="plan", owner_id=plan.id,
    )


async def _custom_to_effective(db: Database, custom: CustomSubscription) -> EffectivePlan:
    restrictions = await db.get_all_key_model_restrictions("custom", custom.telegram_id)
    return EffectivePlan(
        id=0, name=custom.name, allowed_key_ids=custom.allowed_key_ids,
        rpm_limit=custom.rpm_limit, rph_limit=custom.rph_limit, rpd_limit=custom.rpd_limit,
        is_custom=True, key_model_restrictions=restrictions,
        owner_type="custom", owner_id=custom.telegram_id,
    )


async def _admin_effective_plan(db: Database) -> EffectivePlan:
    """Виртуальный безлимитный тариф для админов/владельца: доступны ВСЕ
    активные ключи, лимитов нет. Вычисляется на лету, ничего не хранится."""
    all_keys = await db.list_api_keys(only_active=True)
    return EffectivePlan(
        id=ADMIN_PLAN_ID, name=ADMIN_PLAN_NAME,
        allowed_key_ids=[k.id for k in all_keys],
        rpm_limit=None, rph_limit=None, rpd_limit=None,
        is_admin_plan=True,
    )


async def get_effective_subscription(db: Database, telegram_id: int) -> Optional[Subscription]:
    """Возвращает ОБЫЧНУЮ (plans) подписку пользователя, если она активна.
    Не учитывает кастомные/admin — для полной картины используйте get_user_plan."""
    return await db.get_active_subscription(telegram_id)


async def get_user_plan(
    db: Database, telegram_id: int, is_admin: bool = False
) -> Optional[EffectivePlan]:
    """Возвращает эффективный тариф пользователя с учётом приоритета:
    1) is_admin=True -> виртуальный admin-план (всегда, если не забанен/не заморожен — это
       проверяется отдельно в check_chat_access, здесь просто отдаём план);
    2) кастомная подписка (custom_subscriptions), если активна;
    3) обычная подписка на один из тарифов (plans), если активна;
    4) иначе — None (доступа нет).
    """
    if is_admin:
        return await _admin_effective_plan(db)

    custom = await db.get_active_custom_subscription(telegram_id)
    if custom is not None:
        return await _custom_to_effective(db, custom)

    sub = await db.get_active_subscription(telegram_id)
    if sub is None:
        return None
    plan = await db.get_plan(sub.plan_id)
    if plan is None or not plan.is_active:
        return None
    return await _plan_to_effective(db, plan)


async def get_subscription_kind_and_expiry(
    db: Database, telegram_id: int, is_admin: bool
) -> tuple[str, Optional[int]]:
    """Для профиля: возвращает (тип подписки как строка, expires_at | None).
    Тип — 'admin' | 'custom:<название>' | 'plan:<название>' | 'none'."""
    if is_admin:
        return "admin", None

    custom = await db.get_active_custom_subscription(telegram_id)
    if custom is not None:
        return f"custom:{custom.name}", custom.expires_at

    sub = await db.get_active_subscription(telegram_id)
    if sub is not None:
        plan = await db.get_plan(sub.plan_id)
        name = plan.name if plan else "?"
        return f"plan:{name}", sub.expires_at

    return "none", None


def format_time_left(expires_at: Optional[int]) -> str:
    if expires_at is None:
        return "бессрочно"
    now = int(time.time())
    remaining = expires_at - now
    if remaining <= 0:
        return "истекла"
    days = remaining // 86400
    hours = (remaining % 86400) // 3600
    minutes = (remaining % 3600) // 60
    parts = []
    if days:
        parts.append(f"{days} дн.")
    if hours:
        parts.append(f"{hours} ч.")
    if not days and minutes:
        parts.append(f"{minutes} мин.")
    return " ".join(parts) if parts else "меньше минуты"


async def check_model_access(
    db: Database, telegram_id: int, key: ApiKey, is_admin: bool = False, model: Optional[str] = None
) -> AccessCheckResult:
    """Проверяет, разрешено ли пользователю пользоваться конкретным api_key
    (моделью). Порядок проверки (от самого сильного правила к самому слабому):
      0. is_admin=True -> доступ ко всем активным моделям всегда разрешён;
      1. Персональное переопределение (allow/deny) — если задано, решает всё;
      2. Иначе — есть ли модель в эффективном тарифе пользователя
         (обычный/кастомный, см. get_user_plan);
      3. Если ключ разрешён (шаг 2 пройден) и у него режим 'all_models' —
         дополнительно проверяется ТОЧЕЧНОЕ ограничение конкретных моделей
         внутри ключа (plan.key_model_restrictions): если для этого key_id
         задан непустой список разрешённых имён моделей — реально
         используемая `model` ДОЛЖНА входить в этот список, иначе доступ
         запрещён, даже если сам ключ в тарифе разрешён. Если список пуст
         (ограничения нет) — разрешены все модели ключа, как и раньше.
    """
    if is_admin:
        return AccessCheckResult(key.is_active, "" if key.is_active else "Модель отключена администратором.")

    overrides = await db.get_user_key_overrides(telegram_id)
    if key.id in overrides:
        if overrides[key.id]:
            return AccessCheckResult(True)
        return AccessCheckResult(
            False, f"Администратор явно запретил вам доступ к модели «{key.name}»."
        )

    plan = await get_user_plan(db, telegram_id)
    if plan is None:
        return AccessCheckResult(False, "У вас нет активной подписки.")
    if key.id not in plan.allowed_key_ids:
        return AccessCheckResult(
            False,
            f"Модель «{key.name}» не входит в ваш тариф «{plan.name}». "
            f"Посмотрите доступные модели в «🧠 Мои модели» или обновите тариф в «💳 Подписки».",
        )

    restricted_models = plan.key_model_restrictions.get(key.id)
    if restricted_models and model is not None and model not in restricted_models:
        allowed_str = ", ".join(restricted_models)
        return AccessCheckResult(
            False,
            f"В вашем тарифе «{plan.name}» для ключа «{key.name}» разрешены только "
            f"следующие модели: {allowed_str}. Модель «{model}» недоступна — выберите "
            f"разрешённую модель в «🧠 Мои модели».",
        )
    return AccessCheckResult(True)


async def get_effective_allowed_key_ids(
    db: Database, telegram_id: int, is_admin: bool = False
) -> set[int]:
    """Возвращает итоговый набор ID моделей, доступных пользователю — тариф
    плюс персональные разрешения минус персональные запреты. Используется для
    построения меню «🧠 Мои модели» и профиля."""
    if is_admin:
        plan = await _admin_effective_plan(db)
        return set(plan.allowed_key_ids)

    plan = await get_user_plan(db, telegram_id)
    allowed: set[int] = set(plan.allowed_key_ids) if plan else set()

    overrides = await db.get_user_key_overrides(telegram_id)
    for key_id, is_allowed in overrides.items():
        if is_allowed:
            allowed.add(key_id)
        else:
            allowed.discard(key_id)
    return allowed


async def get_restricted_models_for_key(
    db: Database, telegram_id: int, key_id: int, is_admin: bool = False
) -> Optional[list[str]]:
    """Возвращает список ИМЁН моделей, на которые точечно ограничен доступ
    пользователя внутри конкретного ключа (актуально для ключей с режимом
    'all_models', у которых подключено сразу несколько моделей), либо None,
    если ограничения нет (разрешены все модели ключа). Персональные
    переопределения (user_key_overrides) сюда не относятся — они работают
    на уровне ключа целиком, а не отдельных моделей внутри него."""
    if is_admin:
        return None
    plan = await get_user_plan(db, telegram_id)
    if plan is None:
        return None
    restricted = plan.key_model_restrictions.get(key_id)
    return restricted if restricted else None


async def get_effective_limits(
    db: Database, telegram_id: int, plan: Optional[EffectivePlan]
) -> dict:
    """Возвращает итоговые лимиты rpm/rph/rpd с учётом персонального
    переопределения (если оно есть — оно полностью замещает лимиты тарифа
    по КАЖДОМУ окну независимо, а не объединяется с ними)."""
    if plan is not None and plan.is_admin_plan:
        return {"rpm_limit": None, "rph_limit": None, "rpd_limit": None}

    override = await db.get_user_limit_override(telegram_id)
    if override is not None:
        return override
    if plan is None:
        return {"rpm_limit": None, "rph_limit": None, "rpd_limit": None}
    return {"rpm_limit": plan.rpm_limit, "rph_limit": plan.rph_limit, "rpd_limit": plan.rpd_limit}


def calendar_window_bounds(window_seconds: int, now: Optional[int] = None) -> tuple[int, int]:
    """Возвращает (начало, конец) ТЕКУЩЕГО календарного окна для лимита:
    минута — с начала текущей минуты до начала следующей; час — с начала
    текущего часа; сутки — с 00:00 UTC текущих суток до 00:00 UTC следующих.
    Используется для точного отображения "когда сброс" в профиле (в отличие
    от скользящего окна, которое используется для самой проверки лимита)."""
    now = now if now is not None else int(time.time())
    if window_seconds == WINDOW_MINUTE:
        start = now - (now % 60)
        end = start + 60
    elif window_seconds == WINDOW_HOUR:
        start = now - (now % 3600)
        end = start + 3600
    elif window_seconds == WINDOW_DAY:
        start = now - (now % 86400)
        end = start + 86400
    else:
        start = now - window_seconds
        end = now
    return start, end


async def check_and_record_rate_limit(
    db: Database, telegram_id: int, plan: Optional[EffectivePlan]
) -> RateLimitStatus:
    """Проверяет лимиты rpm/rph/rpd (тарифа, либо персонального переопределения,
    если оно задано администратором — см. get_effective_limits). Лимиты
    считаются по СКОЛЬЗЯЩЕМУ окну (последние N секунд) — это самый надёжный
    способ не дать превысить лимит между календарными окнами. Если лимит не
    превышен — СРАЗУ регистрирует этот запрос (record_request) и возвращает
    allowed=True. Если превышен — запрос НЕ регистрируется, allowed=False.
    """
    now = int(time.time())
    limits = await get_effective_limits(db, telegram_id, plan)

    checks = (
        (limits["rpm_limit"], WINDOW_MINUTE, "минуту"),
        (limits["rph_limit"], WINDOW_HOUR, "час"),
        (limits["rpd_limit"], WINDOW_DAY, "сутки"),
    )
    for limit, window, label in checks:
        if limit is None:
            continue
        count = await db.count_requests_since(telegram_id, now - window)
        if count >= limit:
            return RateLimitStatus(allowed=False, limit_hit=label, retry_after_seconds=window)

    await db.record_request(telegram_id)
    return RateLimitStatus(allowed=True)


@dataclass
class QuotaInfo:
    """Информация об оставшейся квоте для отображения в профиле — берётся
    самое строгое (ближайшее к исчерпанию) из настроенных окон."""
    has_limits: bool
    remaining: Optional[int] = None
    limit: Optional[int] = None
    window_label: str = ""
    reset_at: Optional[int] = None


async def get_quota_info(
    db: Database, telegram_id: int, plan: Optional[EffectivePlan]
) -> QuotaInfo:
    """Вычисляет, сколько запросов ещё осталось до ближайшего лимита и когда
    будет сброс (по календарному окну — см. calendar_window_bounds)."""
    limits = await get_effective_limits(db, telegram_id, plan)
    now = int(time.time())

    windows = (
        (limits["rpm_limit"], WINDOW_MINUTE, "минуту"),
        (limits["rph_limit"], WINDOW_HOUR, "час"),
        (limits["rpd_limit"], WINDOW_DAY, "сутки"),
    )
    candidates = []
    for limit, window, label in windows:
        if limit is None:
            continue
        used = await db.count_requests_since(telegram_id, now - window)
        remaining = max(0, limit - used)
        _, window_end = calendar_window_bounds(window, now)
        candidates.append(QuotaInfo(True, remaining, limit, label, window_end))

    if not candidates:
        return QuotaInfo(has_limits=False)

    # Показываем самое "тесное" ограничение (наименьший остаток)
    candidates.sort(key=lambda q: q.remaining)
    return candidates[0]


@dataclass
class ChatAccessResult:
    allowed: bool
    reason: str = ""
    retry_after_seconds: Optional[int] = None


async def check_chat_access(
    db: Database, telegram_id: int, key: ApiKey, is_admin: bool, model: Optional[str] = None
) -> ChatAccessResult:
    """Единая точка проверки перед КАЖДЫМ обращением к модели (обычный чат,
    архивы, голос, фото — всё, что тратит один "запрос" в рамках лимитов).

    Порядок проверки: бан -> заморозка -> доступ к конкретной модели ->
    лимит запросов. Администраторы освобождены от лимитов, но НЕ от
    бана/заморозки (на случай, если владелец решит забанить бывшего админа).
    `model` — реально используемое имя модели (settings.model_override или
    key.model) — нужно, чтобы проверить точечное ограничение моделей внутри
    ключа с режимом 'all_models' (см. check_model_access); если не передано,
    точечное ограничение не проверяется (только доступ к ключу целиком).
    Если всё ок — запрос СРАЗУ регистрируется (record_request) для не-админов,
    поэтому эту функцию нужно вызывать РОВНО ОДИН РАЗ на каждое обращение к модели.
    """
    profile = await db.get_user_profile(telegram_id)
    if profile is not None and profile.is_banned:
        reason = "🚫 Вы заблокированы в этом боте."
        if profile.banned_reason:
            reason += f" Причина: {profile.banned_reason}"
        return ChatAccessResult(False, reason)

    if profile is not None and profile.is_frozen:
        return ChatAccessResult(
            False, "⏸ Ваша подписка временно заморожена администратором. Обратитесь к администратору."
        )

    if is_admin:
        model_access = await check_model_access(db, telegram_id, key, is_admin=True, model=model)
        if not model_access.allowed:
            return ChatAccessResult(False, model_access.reason)
        return ChatAccessResult(True)

    model_access = await check_model_access(db, telegram_id, key, model=model)
    if not model_access.allowed:
        return ChatAccessResult(False, model_access.reason)

    plan = await get_user_plan(db, telegram_id)
    rate_status = await check_and_record_rate_limit(db, telegram_id, plan)
    if not rate_status.allowed:
        return ChatAccessResult(
            False,
            f"⏳ Превышен лимит запросов ({rate_status.limit_hit}). Попробуйте позже.",
            rate_status.retry_after_seconds,
        )
    return ChatAccessResult(True)


def plan_summary_text(plan: Plan, key_names: dict[int, str]) -> str:
    """Формирует текстовое описание тарифа для витрины подписок."""
    lines = [f"💳 <b>{plan.name}</b>", f"Цена: {plan.price_per_month}/мес."]
    if plan.description:
        lines.append(plan.description)

    limits = []
    if plan.rpm_limit:
        limits.append(f"{plan.rpm_limit}/мин")
    if plan.rph_limit:
        limits.append(f"{plan.rph_limit}/час")
    if plan.rpd_limit:
        limits.append(f"{plan.rpd_limit}/сутки")
    lines.append("Лимиты запросов: " + (", ".join(limits) if limits else "без ограничений"))

    if plan.allowed_key_ids:
        names = [key_names.get(kid, f"#{kid}") for kid in plan.allowed_key_ids]
        lines.append("Доступные модели: " + ", ".join(names))
    else:
        lines.append("Доступные модели: не назначены")

    return "\n".join(lines)
