from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import ApiKey
from providers import (
    PROVIDER_LABELS,
    PROVIDER_NVIDIA_CLOUD,
    PROVIDER_NVIDIA_NIM,
    REASONING_EFFORT_LABELS,
)


def main_menu(is_admin: bool, is_subscriber: bool = True, is_owner: bool = False) -> InlineKeyboardMarkup:
    """Главное меню. Для админов и подписчиков — полный набор разделов.
    Для пользователей без активной подписки (и не админов) — только раздел
    подписок и профиль, всё остальное персональное (диалог, файлы, настройки,
    ключи) скрыто, т.к. доступ к нему всё равно заблокирован на уровне хендлеров.
    "🔑 API-ключи" (полное управление ключами) видно ТОЛЬКО владельцу — обычные
    администраторы (уровень 1) выбирают модель для себя через "🧠 Мои модели",
    как и обычные подписчики (им доступны ВСЕ активные модели, см. subscriptions.py)."""
    b = InlineKeyboardBuilder()
    if is_admin or is_subscriber:
        b.button(text="💬 Новый диалог", callback_data="chat:new")
        b.button(text="📂 Мои файлы (архивы)", callback_data="files:menu")
        b.button(text="⚙️ Настройки", callback_data="settings:menu")
        if is_owner:
            b.button(text="🔑 API-ключи", callback_data="keys:menu")
        else:
            b.button(text="🧠 Мои модели", callback_data="models:menu")
    b.button(text="👤 Профиль", callback_data="profile:menu")
    b.button(text="💳 Подписки", callback_data="subscription:menu")
    if not is_admin:
        b.button(text="🎁 Ввести промокод", callback_data="promo:enter")
    if is_admin:
        b.button(text="👑 Админ-панель", callback_data="admin:menu")
    b.adjust(1)
    return b.as_markup()


def admin_menu(is_owner: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💳 Тарифы", callback_data="admin:plans")
    b.button(text="👤 Пользователи и подписки", callback_data="admin:users")
    if is_owner:
        b.button(text="🔑 API-ключи", callback_data="keys:menu")
        b.button(text="🛡 Управление администраторами", callback_data="admin:manage_admins")
        b.button(text="🎟 Промокоды", callback_data="promo:admin_menu")
    b.button(text="💬 Контакт для оплаты", callback_data="admin:payment_contact")
    b.button(text="📨 Заявки на покупку", callback_data="admin:purchase_requests")
    b.button(text="📢 Рассылка всем пользователям", callback_data="admin:broadcast")
    b.button(text="⬅️ Назад", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()


def keys_menu(keys: list[ApiKey], is_admin: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for k in keys:
        status = "🟢" if k.is_active else "🔴"
        provider_short = "Cloud" if k.provider == PROVIDER_NVIDIA_CLOUD else "NIM"
        b.button(text=f"{status} [{provider_short}] {k.name}", callback_data=f"key:view:{k.id}")
    if is_admin:
        b.button(text="➕ Добавить ключ", callback_data="key:add")
    b.button(text="⬅️ Назад", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()


def key_view_menu(key: ApiKey, is_admin: bool, is_active_for_user: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    select_text = "✅ Уже выбран" if is_active_for_user else "☑️ Использовать этот ключ"
    b.button(text=select_text, callback_data=f"key:select:{key.id}")
    if is_admin:
        toggle_text = "⏸ Отключить" if key.is_active else "▶️ Включить"
        b.button(text=toggle_text, callback_data=f"key:toggle:{key.id}")
        mode_text = (
            "🔀 Режим: одна модель → переключить на «все модели»"
            if key.model_mode == "manual"
            else "🔀 Режим: все модели → переключить на «одна модель»"
        )
        b.button(text=mode_text, callback_data=f"key:mode_toggle:{key.id}")
        if key.model_mode == "manual":
            b.button(text="✏️ Изменить модель", callback_data=f"key:edit_model:{key.id}")
        b.button(text="🧪 Список моделей провайдера", callback_data=f"key:models:{key.id}")
        b.button(text="🎭 Изменить роль ключа", callback_data=f"key:role_menu:{key.id}")
        b.button(text="🗑 Удалить ключ", callback_data=f"key:delete:{key.id}")
    b.button(text="⬅️ Назад", callback_data="keys:menu")
    b.adjust(1)
    return b.as_markup()


def key_role_choice_menu(key_id: int) -> InlineKeyboardMarkup:
    """Выбор роли ключа: обычный чат (текст/vision), распознавание речи (ASR)
    или генерация изображений."""
    b = InlineKeyboardBuilder()
    b.button(text="💬 Чат (текст / изображения)", callback_data=f"key:role_set:{key_id}:chat")
    b.button(text="🎙 Распознавание речи (ASR)", callback_data=f"key:role_set:{key_id}:asr")
    b.button(text="🎨 Генерация изображений", callback_data=f"key:role_set:{key_id}:image_gen")
    b.button(text="⬅️ Назад", callback_data=f"key:view:{key_id}")
    b.adjust(1)
    return b.as_markup()


def provider_choice_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=PROVIDER_LABELS[PROVIDER_NVIDIA_CLOUD], callback_data=f"provider:{PROVIDER_NVIDIA_CLOUD}")
    b.button(text=PROVIDER_LABELS[PROVIDER_NVIDIA_NIM], callback_data=f"provider:{PROVIDER_NVIDIA_NIM}")
    b.button(text="❌ Отмена", callback_data="keys:menu")
    b.adjust(1)
    return b.as_markup()


def key_model_mode_choice_menu() -> InlineKeyboardMarkup:
    """Выбор при добавлении нового ключа: указать модель вручную (одну
    фиксированную) или дать доступ ко ВСЕМ моделям, реально подключённым
    к этому ключу у провайдера (список запрашивается заново при каждом выборе)."""
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Указать модель вручную", callback_data="key_model_mode:manual")
    b.button(text="🌐 Разрешить все модели ключа", callback_data="key_model_mode:all_models")
    b.button(text="❌ Отмена", callback_data="keys:menu")
    b.adjust(1)
    return b.as_markup()


def confirm_delete_key(key_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, удалить", callback_data=f"key:delete_confirm:{key_id}")
    b.button(text="❌ Отмена", callback_data=f"key:view:{key_id}")
    b.adjust(1)
    return b.as_markup()


def live_model_choice_menu(
    key_id: int, models: list[str], current_model: str | None, back_cb: str, pick_prefix: str = "key:livemodel_pick"
) -> InlineKeyboardMarkup:
    """Список моделей, ЖИВЬЁМ полученный от провайдера (для ключей в режиме
    'all_models', либо при ручном редактировании модели ключа) — пользователь
    выбирает нужную по имени, не вводя её текстом вручную (защита от опечаток).
    Callback_data кодирует ИНДЕКС модели в списке (а не имя целиком — оно может
    быть длинным и содержать символы, неудобные для callback_data).
    `pick_prefix` различает сценарий владельца (меняет модель ключа целиком) и
    сценарий обычного пользователя (выбирает модель лично для себя)."""
    b = InlineKeyboardBuilder()
    for idx, m in enumerate(models):
        mark = "✅ " if m == current_model else ""
        b.button(text=f"{mark}{m}", callback_data=f"{pick_prefix}:{key_id}:{idx}")
    b.button(text="⬅️ Назад", callback_data=back_cb)
    b.adjust(1)
    return b.as_markup()


def settings_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🧠 Модель", callback_data="settings:model")
    b.button(text="💭 Уровень мышления", callback_data="settings:reasoning")
    b.button(text="📝 Системный промпт", callback_data="settings:system_prompt")
    b.button(text="🌡 Temperature", callback_data="settings:temperature")
    b.button(text="🎯 Top-p", callback_data="settings:top_p")
    b.button(text="📏 Max tokens", callback_data="settings:max_tokens")
    b.button(text="🔀 Streaming вкл/выкл", callback_data="settings:streaming")
    b.button(text="🤖 Режим агента вкл/выкл", callback_data="settings:agent_mode")
    b.button(text="🔒 Подтверждение кода вкл/выкл", callback_data="settings:confirm_code")
    b.button(text="🎙 Ключ для голосовых (ASR)", callback_data="settings:asr_key")
    b.button(text="🧹 Очистить историю диалога", callback_data="settings:clear_history")
    b.button(text="⬅️ Назад", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()


def asr_key_choice_menu(keys: list[ApiKey], active_asr_key_id: int | None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for k in keys:
        mark = "✅ " if k.id == active_asr_key_id else ""
        b.button(text=f"{mark}{k.name}", callback_data=f"settings:asr_key_set:{k.id}")
    if active_asr_key_id is not None:
        b.button(text="🚫 Не использовать (отключить распознавание голоса)", callback_data="settings:asr_key_set:none")
    b.button(text="⬅️ Назад", callback_data="settings:menu")
    b.adjust(1)
    return b.as_markup()


def reasoning_effort_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for level, label in REASONING_EFFORT_LABELS.items():
        b.button(text=label, callback_data=f"reasoning:set:{level}")
    b.button(text="↩️ Сбросить на значение по умолчанию", callback_data="reasoning:set:default")
    b.button(text="⬅️ Назад", callback_data="settings:menu")
    b.adjust(1)
    return b.as_markup()


def code_confirmation_menu(confirmation_id: str) -> InlineKeyboardMarkup:
    """Кнопки, которые видит пользователь под сообщением с кодом, ожидающим
    подтверждения перед выполнением в песочнице."""
    b = InlineKeyboardBuilder()
    b.button(text="✅ Выполнить код", callback_data=f"code_confirm:approve:{confirmation_id}")
    b.button(text="❌ Отклонить", callback_data=f"code_confirm:deny:{confirmation_id}")
    b.adjust(1)
    return b.as_markup()


def files_menu(archives) -> InlineKeyboardMarkup:
    """Главное меню файлового браузера: список загруженных пользователем архивов."""
    b = InlineKeyboardBuilder()
    for a in archives:
        total = len(a.text_files) + len(a.images)
        b.button(text=f"📦 {a.original_name} ({total} файлов)", callback_data=f"files:archive:{a.archive_id}")
    b.button(text="⬅️ Назад", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()


def archive_files_menu(archive) -> InlineKeyboardMarkup:
    """Список файлов внутри конкретного архива (индексы вместо имён — короче для callback_data)."""
    b = InlineKeyboardBuilder()
    for i, f in enumerate(archive.text_files):
        b.button(text=f"📄 {f.filename}", callback_data=f"files:text:{archive.archive_id}:{i}:0")
    for i, img in enumerate(archive.images):
        b.button(text=f"🖼 {img.filename}", callback_data=f"files:image:{archive.archive_id}:{i}")
    b.button(text="🗑 Удалить этот архив", callback_data=f"files:delete_ask:{archive.archive_id}")
    b.button(text="⬅️ Назад", callback_data="files:menu")
    b.adjust(1)
    return b.as_markup()


def text_file_view_menu(archive_id: str, file_index: int, page: int, total_pages: int) -> InlineKeyboardMarkup:
    """Пагинация для просмотра длинного текстового файла + кнопка назад."""
    b = InlineKeyboardBuilder()
    nav_row = []
    if page > 0:
        nav_row.append(("⬅️", f"files:text:{archive_id}:{file_index}:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(("➡️", f"files:text:{archive_id}:{file_index}:{page + 1}"))
    for text, cb in nav_row:
        b.button(text=text, callback_data=cb)
    if total_pages > 1:
        b.button(text=f"стр. {page + 1}/{total_pages}", callback_data="noop")
    b.button(text="⬅️ К списку файлов", callback_data=f"files:archive:{archive_id}")
    b.adjust(len(nav_row) or 1, 1, 1)
    return b.as_markup()


def confirm_delete_archive(archive_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, удалить архив", callback_data=f"files:delete_confirm:{archive_id}")
    b.button(text="❌ Отмена", callback_data=f"files:archive:{archive_id}")
    b.adjust(1)
    return b.as_markup()


def cancel_menu(callback_data: str = "menu:main") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=callback_data)
    return b.as_markup()


def back_menu(callback_data: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data=callback_data)
    return b.as_markup()


# =====================================================================
# ПОДПИСКИ / ТАРИФЫ — витрина для пользователя
# =====================================================================


def subscription_showcase_menu(plans, is_admin: bool) -> InlineKeyboardMarkup:
    """Витрина тарифов, доступная ЛЮБОМУ пользователю (даже без подписки)."""
    b = InlineKeyboardBuilder()
    for p in plans:
        b.button(text=f"💳 {p.name} — {p.price_per_month}/мес.", callback_data=f"subscription:view:{p.id}")
    if is_admin:
        b.button(text="⚙️ Управление тарифами", callback_data="admin:plans")
    b.button(text="⬅️ Назад", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()


def subscription_plan_view_menu(plan_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🛒 Хочу купить", callback_data=f"subscription:buy:{plan_id}")
    b.button(text="⬅️ К списку тарифов", callback_data="subscription:menu")
    b.adjust(1)
    return b.as_markup()


# =====================================================================
# АДМИНКА — управление тарифами (planами)
# =====================================================================


def admin_plans_menu(plans) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for p in plans:
        status = "🟢" if p.is_active else "🔴"
        b.button(text=f"{status} {p.name}", callback_data=f"admin:plan_view:{p.id}")
    b.button(text="➕ Создать тариф", callback_data="admin:plan_add")
    b.button(text="⬅️ Назад", callback_data="admin:menu")
    b.adjust(1)
    return b.as_markup()


def admin_plan_view_menu(plan) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Название", callback_data=f"admin:plan_edit:{plan.id}:name")
    b.button(text="✏️ Описание", callback_data=f"admin:plan_edit:{plan.id}:description")
    b.button(text="✏️ Цена/мес.", callback_data=f"admin:plan_edit:{plan.id}:price")
    b.button(text="🧠 Разрешённые модели", callback_data=f"admin:plan_models:{plan.id}")
    b.button(text="✏️ Лимит в минуту", callback_data=f"admin:plan_edit:{plan.id}:rpm")
    b.button(text="✏️ Лимит в час", callback_data=f"admin:plan_edit:{plan.id}:rph")
    b.button(text="✏️ Лимит в сутки", callback_data=f"admin:plan_edit:{plan.id}:rpd")
    toggle_text = "⏸ Скрыть из витрины" if plan.is_active else "▶️ Показать в витрине"
    b.button(text=toggle_text, callback_data=f"admin:plan_toggle:{plan.id}")
    b.button(text="🗑 Удалить тариф", callback_data=f"admin:plan_delete_ask:{plan.id}")
    b.button(text="⬅️ Назад", callback_data="admin:plans")
    b.adjust(1)
    return b.as_markup()


def admin_plan_models_menu(plan, all_keys: list[ApiKey], restricted_key_ids: set[int] | None = None) -> InlineKeyboardMarkup:
    """Список ключей (моделей) с чекбоксами — включить/выключить в тарифе.
    Для ключей с режимом 'all_models', которые уже включены в тариф,
    дополнительно показывается кнопка "🎯 Настроить модели" — точечное
    ограничение, какие ИМЕННО модели этого ключа доступны в этом тарифе
    (по умолчанию, если не настроено, доступны ВСЕ модели ключа).
    `restricted_key_ids` — набор key_id, для которых уже задано точечное
    ограничение (показывается отметкой на кнопке настройки)."""
    restricted_key_ids = restricted_key_ids or set()
    b = InlineKeyboardBuilder()
    for k in all_keys:
        mark = "✅" if k.id in plan.allowed_key_ids else "⬜️"
        b.button(text=f"{mark} {k.name} ({k.model or '—'})", callback_data=f"admin:plan_model_toggle:{plan.id}:{k.id}")
        if k.id in plan.allowed_key_ids and k.model_mode == "all_models":
            restr_mark = "🎯" if k.id in restricted_key_ids else "🌐"
            b.button(
                text=f"   {restr_mark} Настроить модели ключа «{k.name}»",
                callback_data=f"admin:plan_key_models:{plan.id}:{k.id}",
            )
    b.button(text="⬅️ Назад", callback_data=f"admin:plan_view:{plan.id}")
    b.adjust(1)
    return b.as_markup()


def admin_key_model_restriction_menu(
    plan_id: int, key_id: int, live_models: list[str], selected: set[str], owner_type: str = "plan"
) -> InlineKeyboardMarkup:
    """Точечный выбор конкретных моделей ЖИВОГО списка провайдера, разрешённых
    в данном тарифе/кастомной подписке для ключа с режимом 'all_models'.
    Если `selected` пуст — ограничения нет (разрешены все модели ключа)."""
    b = InlineKeyboardBuilder()
    prefix = "admin:plan_key_model_toggle" if owner_type == "plan" else "admin:custom_key_model_toggle"
    for m in live_models:
        mark = "✅" if m in selected else "⬜️"
        b.button(text=f"{mark} {m}", callback_data=f"{prefix}:{plan_id}:{key_id}:{live_models.index(m)}")
    clear_cb = f"admin:plan_key_models_clear:{plan_id}:{key_id}" if owner_type == "plan" else f"admin:custom_key_models_clear:{plan_id}:{key_id}"
    b.button(text="♻️ Сбросить (разрешить ВСЕ модели ключа)", callback_data=clear_cb)
    back_cb = f"admin:plan_models:{plan_id}" if owner_type == "plan" else f"admin:custom_manage:{plan_id}"
    b.button(text="⬅️ Назад", callback_data=back_cb)
    b.adjust(1)
    return b.as_markup()


def confirm_delete_plan(plan_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, удалить тариф", callback_data=f"admin:plan_delete_confirm:{plan_id}")
    b.button(text="❌ Отмена", callback_data=f"admin:plan_view:{plan_id}")
    b.adjust(1)
    return b.as_markup()


def plan_choice_menu(plans, prefix: str) -> InlineKeyboardMarkup:
    """Общий выбор тарифа из списка — используется при выдаче подписки."""
    b = InlineKeyboardBuilder()
    for p in plans:
        b.button(text=p.name, callback_data=f"{prefix}:{p.id}")
    b.button(text="❌ Отмена", callback_data="admin:users")
    b.adjust(1)
    return b.as_markup()


def duration_choice_menu(prefix: str) -> InlineKeyboardMarkup:
    """Быстрый выбор длительности подписки (в днях) + произвольный срок + бессрочно."""
    b = InlineKeyboardBuilder()
    b.button(text="1 день", callback_data=f"{prefix}:1")
    b.button(text="7 дней", callback_data=f"{prefix}:7")
    b.button(text="30 дней", callback_data=f"{prefix}:30")
    b.button(text="90 дней", callback_data=f"{prefix}:90")
    b.button(text="✏️ Другой срок", callback_data=f"{prefix}:custom")
    b.button(text="♾ Бессрочно", callback_data=f"{prefix}:forever")
    b.button(text="❌ Отмена", callback_data="admin:users")
    b.adjust(2, 2, 1, 1, 1)
    return b.as_markup()


# =====================================================================
# АДМИНКА — управление конкретным пользователем (подписка, лимиты, доступ к моделям)
# =====================================================================


def admin_user_view_menu(
    telegram_id: int,
    has_subscription: bool,
    is_banned: bool = False,
    is_frozen: bool = False,
    has_custom_subscription: bool = False,
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if has_custom_subscription:
        # Управление кастомной подпиской (название/модели/лимиты/срок/заморозка/
        # отзыв) полностью вынесено в отдельную карточку admin:custom_manage —
        # чтобы не дублировать "Забрать подписку"/"Заморозить" сразу в двух
        # местах, здесь показываем только вход в это управление.
        b.button(text="🌟 Управлять кастомной подпиской", callback_data=f"admin:custom_manage:{telegram_id}")
    else:
        b.button(text="🎁 Выдать/изменить подписку", callback_data=f"admin:user_grant:{telegram_id}")
        b.button(text="🌟 Выдать кастомную подписку", callback_data=f"admin:user_custom_grant:{telegram_id}")
        if has_subscription:
            b.button(text="🗑 Забрать подписку", callback_data=f"admin:user_revoke:{telegram_id}")
            freeze_text = "▶️ Разморозить подписку" if is_frozen else "⏸ Заморозить подписку"
            b.button(text=freeze_text, callback_data=f"admin:user_freeze_toggle:{telegram_id}")
    b.button(text="🧠 Доступ к моделям (лично)", callback_data=f"admin:user_models:{telegram_id}")
    b.button(text="🚦 Личные лимиты", callback_data=f"admin:user_limits:{telegram_id}")
    b.button(text="🔄 Сбросить лимиты запросов сейчас", callback_data=f"admin:user_reset_limits:{telegram_id}")
    ban_text = "✅ Разблокировать пользователя" if is_banned else "🚫 Заблокировать пользователя"
    b.button(text=ban_text, callback_data=f"admin:user_ban_toggle:{telegram_id}")
    b.button(text="👤 Открыть профиль", callback_data=f"profile:view_other:{telegram_id}")
    b.button(text="⬅️ Назад", callback_data="admin:users")
    b.adjust(1)
    return b.as_markup()


def admin_custom_manage_menu(telegram_id: int, is_frozen: bool = False) -> InlineKeyboardMarkup:
    """Полноценное управление уже выданной кастомной подпиской конкретного
    пользователя — аналог управления обычным тарифом (admin_plan_view_menu),
    но для персональной (custom_subscriptions) подписки."""
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Название", callback_data=f"admin:custom_edit_name:{telegram_id}")
    b.button(text="🧠 Модели", callback_data=f"admin:custom_edit_models:{telegram_id}")
    b.button(text="✏️ Лимит в минуту", callback_data=f"admin:custom_edit_limit:{telegram_id}:rpm")
    b.button(text="✏️ Лимит в час", callback_data=f"admin:custom_edit_limit:{telegram_id}:rph")
    b.button(text="✏️ Лимит в сутки", callback_data=f"admin:custom_edit_limit:{telegram_id}:rpd")
    b.button(text="⏳ Изменить срок действия", callback_data=f"admin:custom_edit_duration:{telegram_id}")
    freeze_text = "▶️ Разморозить подписку" if is_frozen else "⏸ Заморозить подписку"
    b.button(text=freeze_text, callback_data=f"admin:user_freeze_toggle:{telegram_id}")
    b.button(text="🗑 Забрать подписку", callback_data=f"admin:user_revoke:{telegram_id}")
    b.button(text="⬅️ Назад", callback_data=f"admin:user_view:{telegram_id}")
    b.adjust(1)
    return b.as_markup()


def admin_user_models_menu(telegram_id: int, all_keys: list[ApiKey], overrides: dict[int, bool]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for k in all_keys:
        if k.id in overrides:
            mark = "✅ (разрешено лично)" if overrides[k.id] else "⛔ (запрещено лично)"
        else:
            mark = "· (по тарифу)"
        b.button(text=f"{mark} {k.name}", callback_data=f"admin:user_model_cycle:{telegram_id}:{k.id}")
    b.button(text="⬅️ Назад", callback_data=f"admin:user_view:{telegram_id}")
    b.adjust(1)
    return b.as_markup()


def admin_user_limits_menu(telegram_id: int, has_override: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Лимит в минуту", callback_data=f"admin:user_limit_edit:{telegram_id}:rpm")
    b.button(text="✏️ Лимит в час", callback_data=f"admin:user_limit_edit:{telegram_id}:rph")
    b.button(text="✏️ Лимит в сутки", callback_data=f"admin:user_limit_edit:{telegram_id}:rpd")
    if has_override:
        b.button(text="↩️ Сбросить на лимиты тарифа", callback_data=f"admin:user_limit_reset:{telegram_id}")
    b.button(text="⬅️ Назад", callback_data=f"admin:user_view:{telegram_id}")
    b.adjust(1)
    return b.as_markup()


def admin_users_menu(telegram_ids: list[int]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for uid in telegram_ids:
        b.button(text=f"👤 {uid}", callback_data=f"admin:user_view:{uid}")
    b.button(text="🔍 Найти/добавить по ID", callback_data="admin:user_find")
    b.button(text="⬅️ Назад", callback_data="admin:menu")
    b.adjust(1)
    return b.as_markup()


def admin_purchase_requests_menu(requests_with_plans) -> InlineKeyboardMarkup:
    """requests_with_plans — список кортежей (PurchaseRequest, Plan|None)."""
    b = InlineKeyboardBuilder()
    for req, plan in requests_with_plans:
        plan_name = plan.name if plan else "?"
        b.button(
            text=f"👤 {req.telegram_id} → {plan_name}",
            callback_data=f"admin:user_grant:{req.telegram_id}",
        )
        b.button(text=f"✅ Обработано #{req.id}", callback_data=f"admin:purchase_handled:{req.id}")
    b.button(text="⬅️ Назад", callback_data="admin:menu")
    b.adjust(2)
    return b.as_markup()


# =====================================================================
# "МОИ МОДЕЛИ" — для обычного пользователя с подпиской
# =====================================================================


def my_models_menu(keys: list[ApiKey], active_key_id: int | None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for k in keys:
        mark = "✅ " if k.id == active_key_id else ""
        if k.model_mode == "all_models":
            # Ключ в режиме "все модели" — при выборе сначала показываем живой
            # список моделей провайдера (см. handlers/models.py:cb_mymodel_live_menu),
            # а не сразу активируем ключ с непонятно какой моделью.
            label = f"{k.name} (выбрать модель из списка)"
            b.button(text=f"{mark}{label}", callback_data=f"mymodel:live_menu:{k.id}")
        else:
            label = k.model or k.name
            b.button(text=f"{mark}{label}", callback_data=f"models:select:{k.id}")
    b.button(text="⬅️ Назад", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()


# =====================================================================
# ПРОФИЛЬ
# =====================================================================


def profile_menu(is_admin: bool = False, viewing_other: int | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if viewing_other is not None:
        b.button(text="⬅️ Назад к пользователю", callback_data=f"admin:user_view:{viewing_other}")
    else:
        b.button(text="⬅️ Назад", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()


# =====================================================================
# УПРАВЛЕНИЕ АДМИНАМИ (только владелец)
# =====================================================================


def manage_admins_menu(admin_ids: list[int]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for uid in admin_ids:
        b.button(text=f"🛡 {uid}  ➖ снять", callback_data=f"admin:remove_admin:{uid}")
    b.button(text="➕ Назначить администратора", callback_data="admin:add_admin")
    b.button(text="⬅️ Назад", callback_data="admin:menu")
    b.adjust(1)
    return b.as_markup()


# =====================================================================
# РАССЫЛКА
# =====================================================================


def broadcast_confirm_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Отправить всем", callback_data="admin:broadcast_confirm")
    b.button(text="❌ Отмена", callback_data="admin:menu")
    b.adjust(1)
    return b.as_markup()


# =====================================================================
# КАСТОМНЫЕ ПОДПИСКИ (админка)
# =====================================================================


def custom_plan_models_menu(telegram_id: int, all_keys: list[ApiKey], selected: set[int]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for k in all_keys:
        mark = "✅" if k.id in selected else "⬜️"
        b.button(text=f"{mark} {k.name} ({k.model or '—'})", callback_data=f"admin:custom_model_toggle:{telegram_id}:{k.id}")
    b.button(text="➡️ Далее (срок действия)", callback_data=f"admin:custom_models_done:{telegram_id}")
    b.button(text="❌ Отмена", callback_data=f"admin:user_view:{telegram_id}")
    b.adjust(1)
    return b.as_markup()


def custom_edit_models_menu(
    telegram_id: int, all_keys: list[ApiKey], selected: set[int], restricted_key_ids: set[int] | None = None
) -> InlineKeyboardMarkup:
    """Редактирование набора моделей УЖЕ ВЫДАННОЙ кастомной подписки — в
    отличие от `custom_plan_models_menu` (используется при первичной выдаче
    и ведёт дальше к вводу названия/срока), здесь сразу сохраняет и
    возвращает к карточке управления подпиской, не трогая срок действия.
    Для ключей с режимом 'all_models' дополнительно доступна кнопка
    настройки конкретных разрешённых моделей внутри ключа."""
    restricted_key_ids = restricted_key_ids or set()
    b = InlineKeyboardBuilder()
    for k in all_keys:
        mark = "✅" if k.id in selected else "⬜️"
        b.button(text=f"{mark} {k.name} ({k.model or '—'})", callback_data=f"admin:custom_edit_model_toggle:{telegram_id}:{k.id}")
        if k.id in selected and k.model_mode == "all_models":
            restr_mark = "🎯" if k.id in restricted_key_ids else "🌐"
            b.button(
                text=f"   {restr_mark} Настроить модели ключа «{k.name}»",
                callback_data=f"admin:custom_key_models:{telegram_id}:{k.id}",
            )
    b.button(text="✅ Сохранить", callback_data=f"admin:custom_edit_models_done:{telegram_id}")
    b.adjust(1)
    return b.as_markup()


# =====================================================================
# ПРОМОКОДЫ (создание/управление — только владелец; активация — обычный пользователь)
# =====================================================================


def promo_admin_menu(codes) -> InlineKeyboardMarkup:
    """codes — list[PromoCode]."""
    b = InlineKeyboardBuilder()
    for p in codes:
        status = "🟢" if p.is_active else "🔴"
        b.button(text=f"{status} {p.code}", callback_data=f"promo:view:{p.code}")
    b.button(text="➕ Создать промокод", callback_data="promo:create_start")
    b.button(text="⬅️ Назад", callback_data="admin:menu")
    b.adjust(1)
    return b.as_markup()


def promo_view_menu(promo) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    toggle_text = "⏸ Отключить" if promo.is_active else "▶️ Включить"
    b.button(text=toggle_text, callback_data=f"promo:toggle:{promo.code}")
    b.button(text="✏️ Срок выдаваемой подписки", callback_data=f"promo:edit:{promo.code}:duration_days")
    b.button(text="✏️ Лимит активаций", callback_data=f"promo:edit:{promo.code}:max_activations")
    b.button(text="✏️ Срок действия кода (с)", callback_data=f"promo:edit:{promo.code}:valid_from")
    b.button(text="✏️ Срок действия кода (по)", callback_data=f"promo:edit:{promo.code}:valid_until")
    audience_text = "👥 Аудитория: все → сделать «только подписчики»" if promo.target_audience == "all" \
        else "👥 Аудитория: только подписчики → сделать «все»"
    b.button(text=audience_text, callback_data=f"promo:toggle_audience:{promo.code}")
    b.button(text="🗑 Удалить промокод", callback_data=f"promo:delete_ask:{promo.code}")
    b.button(text="⬅️ Назад", callback_data="promo:admin_menu")
    b.adjust(1)
    return b.as_markup()


def confirm_delete_promo(code: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, удалить", callback_data=f"promo:delete_confirm:{code}")
    b.button(text="❌ Отмена", callback_data=f"promo:view:{code}")
    b.adjust(1)
    return b.as_markup()


def promo_reward_type_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💳 Выдать существующий тариф", callback_data="promo:reward_type:plan")
    b.button(text="🌟 Собрать кастомную подписку", callback_data="promo:reward_type:custom")
    b.button(text="❌ Отмена", callback_data="promo:admin_menu")
    b.adjust(1)
    return b.as_markup()


def promo_plan_choice_menu(plans) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for p in plans:
        b.button(text=p.name, callback_data=f"promo:create_plan:{p.id}")
    b.button(text="❌ Отмена", callback_data="promo:admin_menu")
    b.adjust(1)
    return b.as_markup()


def promo_custom_models_menu(all_keys: list[ApiKey], selected: set[int]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for k in all_keys:
        mark = "✅" if k.id in selected else "⬜️"
        b.button(text=f"{mark} {k.name} ({k.model or '—'})", callback_data=f"promo:create_model_toggle:{k.id}")
    b.button(text="➡️ Далее", callback_data="promo:create_models_done")
    b.button(text="❌ Отмена", callback_data="promo:admin_menu")
    b.adjust(1)
    return b.as_markup()


def promo_audience_menu(prefix: str = "promo:create_audience") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="👥 Все пользователи", callback_data=f"{prefix}:all")
    b.button(text="⭐ Только с активной подпиской", callback_data=f"{prefix}:subscribers_only")
    b.button(text="❌ Отмена", callback_data="promo:admin_menu")
    b.adjust(1)
    return b.as_markup()


def promo_duration_menu(prefix: str = "promo:create_duration") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="1 день", callback_data=f"{prefix}:1")
    b.button(text="7 дней", callback_data=f"{prefix}:7")
    b.button(text="30 дней", callback_data=f"{prefix}:30")
    b.button(text="90 дней", callback_data=f"{prefix}:90")
    b.button(text="✏️ Другой срок", callback_data=f"{prefix}:custom")
    b.button(text="♾ Бессрочно", callback_data=f"{prefix}:forever")
    b.button(text="❌ Отмена", callback_data="promo:admin_menu")
    b.adjust(2, 2, 1, 1, 1)
    return b.as_markup()
