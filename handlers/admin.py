"""Админ-панель: тарифы (планы), подписки пользователей, персональные
переопределения доступа к моделям и лимитов, заявки на покупку, контакт
для оплаты. Whitelist полностью упразднён — подписка (см. subscriptions.py)
теперь единственный уровень доступа к диалогу с моделью.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import roles
import subscriptions as sub_logic
from database import Database
from keyboards import (
    admin_custom_manage_menu,
    admin_key_model_restriction_menu,
    admin_menu,
    admin_plan_models_menu,
    admin_plan_view_menu,
    admin_plans_menu,
    admin_purchase_requests_menu,
    admin_user_limits_menu,
    admin_user_models_menu,
    admin_user_view_menu,
    admin_users_menu,
    broadcast_confirm_menu,
    cancel_menu,
    confirm_delete_plan,
    custom_edit_models_menu,
    custom_plan_models_menu,
    duration_choice_menu,
    main_menu,
    manage_admins_menu,
    plan_choice_menu,
)
from states import (
    BanUserStates,
    BroadcastStates,
    CustomSubscriptionEditStates,
    CustomSubscriptionStates,
    GrantSubscriptionStates,
    ManageAdminsStates,
    PaymentContactStates,
    PlanStates,
)
from text_utils import escape_html, safe_edit_reply_markup, safe_edit_text

router = Router(name="admin")


def _require_admin(is_admin: bool) -> bool:
    return is_admin


@router.callback_query(F.data == "admin:menu")
async def cb_admin_menu(callback: CallbackQuery, is_admin: bool, is_owner: bool) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    await safe_edit_text(callback.message, "👑 Админ-панель", reply_markup=admin_menu(is_owner))
    await callback.answer()


# =====================================================================
# ТАРИФЫ (ПЛАНЫ)
# =====================================================================


async def _key_names_map(db: Database) -> dict[int, str]:
    keys = await db.list_api_keys()
    return {k.id: (k.model or k.name) for k in keys}


@router.callback_query(F.data == "admin:plans")
async def cb_admin_plans(callback: CallbackQuery, is_admin: bool, db: Database) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    plans = await db.list_plans()
    text = "💳 <b>Тарифы</b>\n\n🟢 виден в витрине, 🔴 скрыт." if plans else "💳 <b>Тарифы</b>\n\nТарифов пока нет — создайте первый."
    await safe_edit_text(callback.message, text, reply_markup=admin_plans_menu(plans))
    await callback.answer()


@router.callback_query(F.data.startswith("admin:plan_view:"))
async def cb_admin_plan_view(callback: CallbackQuery, is_admin: bool, db: Database) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    plan_id = int(callback.data.split(":")[-1])
    plan = await db.get_plan(plan_id)
    if not plan:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    key_names = await _key_names_map(db)
    text = sub_logic.plan_summary_text(plan, key_names) + f"\n\nСтатус: {'🟢 виден в витрине' if plan.is_active else '🔴 скрыт'}"
    await safe_edit_text(callback.message, text, reply_markup=admin_plan_view_menu(plan))
    await callback.answer()


@router.callback_query(F.data == "admin:plan_add")
async def cb_admin_plan_add(callback: CallbackQuery, is_admin: bool, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    await state.set_state(PlanStates.waiting_name)
    await safe_edit_text(callback.message, "Введите название нового тарифа (например «Базовый»):",
        reply_markup=cancel_menu("admin:plans"),
    )
    await callback.answer()


@router.message(PlanStates.waiting_name)
async def process_plan_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым. Попробуйте снова или /cancel.")
        return
    await state.update_data(name=name)
    await state.set_state(PlanStates.waiting_description)
    await message.answer(
        "Введите описание тарифа (что входит, для кого и т.д.). Отправьте «-», чтобы оставить пустым.",
        reply_markup=cancel_menu("admin:plans"),
    )


@router.message(PlanStates.waiting_description)
async def process_plan_description(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    await state.update_data(description="" if text == "-" else text)
    await state.set_state(PlanStates.waiting_price)
    await message.answer(
        "Введите цену за месяц как текст (например «199 руб.» или «$5»):",
        reply_markup=cancel_menu("admin:plans"),
    )


@router.message(PlanStates.waiting_price)
async def process_plan_price(message: Message, state: FSMContext) -> None:
    price = (message.text or "").strip()
    if not price:
        await message.answer("Цена не может быть пустой. Попробуйте снова или /cancel.")
        return
    await state.update_data(price=price)
    await state.set_state(PlanStates.waiting_rpm)
    await message.answer(
        "Лимит запросов В МИНУТУ. Введите число, либо «-» для безлимита.",
        reply_markup=cancel_menu("admin:plans"),
    )


def _parse_optional_int(text: str) -> tuple[bool, int | None]:
    text = text.strip()
    if text == "-":
        return True, None
    if text.isdigit() and int(text) > 0:
        return True, int(text)
    return False, None


@router.message(PlanStates.waiting_rpm)
async def process_plan_rpm(message: Message, state: FSMContext) -> None:
    ok, value = _parse_optional_int(message.text or "")
    if not ok:
        await message.answer("❗ Нужно положительное целое число или «-». Попробуйте снова или /cancel.")
        return
    await state.update_data(rpm=value)
    await state.set_state(PlanStates.waiting_rph)
    await message.answer("Лимит запросов В ЧАС. Введите число, либо «-» для безлимита.", reply_markup=cancel_menu("admin:plans"))


@router.message(PlanStates.waiting_rph)
async def process_plan_rph(message: Message, state: FSMContext) -> None:
    ok, value = _parse_optional_int(message.text or "")
    if not ok:
        await message.answer("❗ Нужно положительное целое число или «-». Попробуйте снова или /cancel.")
        return
    await state.update_data(rph=value)
    await state.set_state(PlanStates.waiting_rpd)
    await message.answer("Лимит запросов В СУТКИ. Введите число, либо «-» для безлимита.", reply_markup=cancel_menu("admin:plans"))


@router.message(PlanStates.waiting_rpd)
async def process_plan_rpd(message: Message, state: FSMContext, db: Database, is_admin: bool) -> None:
    ok, value = _parse_optional_int(message.text or "")
    if not ok:
        await message.answer("❗ Нужно положительное целое число или «-». Попробуйте снова или /cancel.")
        return
    data = await state.get_data()
    plan_id = await db.create_plan(
        name=data["name"],
        description=data["description"],
        price_per_month=data["price"],
        allowed_key_ids=[],
        rpm_limit=data["rpm"],
        rph_limit=data["rph"],
        rpd_limit=value,
    )
    await state.clear()
    await message.answer(
        f"✅ Тариф «{escape_html(data['name'])}» создан (id={plan_id}).\n"
        f"Не забудьте назначить разрешённые модели: откройте тариф → «🧠 Разрешённые модели»."
    )
    plans = await db.list_plans()
    await message.answer("💳 <b>Тарифы</b>", reply_markup=admin_plans_menu(plans))


@router.callback_query(F.data.startswith("admin:plan_edit:"))
async def cb_admin_plan_edit(callback: CallbackQuery, is_admin: bool, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    _, _, plan_id_str, field_name = callback.data.split(":")
    plan_id = int(plan_id_str)
    await state.set_state(PlanStates.editing_field)
    await state.update_data(plan_id=plan_id, field_name=field_name)

    prompts = {
        "name": "Введите новое название тарифа:",
        "description": "Введите новое описание тарифа:",
        "price": "Введите новую цену за месяц (текст, например «199 руб.»):",
        "rpm": "Введите новый лимит запросов в минуту (число или «-» для безлимита):",
        "rph": "Введите новый лимит запросов в час (число или «-» для безлимита):",
        "rpd": "Введите новый лимит запросов в сутки (число или «-» для безлимита):",
    }
    await safe_edit_text(callback.message, prompts.get(field_name, "Введите новое значение:"),
        reply_markup=cancel_menu(f"admin:plan_view:{plan_id}"),
    )
    await callback.answer()


@router.message(PlanStates.editing_field)
async def process_plan_edit_value(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    plan_id = data["plan_id"]
    field_name = data["field_name"]
    text = (message.text or "").strip()

    db_field_map = {"name": "name", "description": "description", "price": "price_per_month",
                    "rpm": "rpm_limit", "rph": "rph_limit", "rpd": "rpd_limit"}

    if field_name in {"rpm", "rph", "rpd"}:
        ok, value = _parse_optional_int(text)
        if not ok:
            await message.answer("❗ Нужно положительное целое число или «-». Попробуйте снова или /cancel.")
            return
    elif field_name == "description" and text == "-":
        value = ""
    else:
        if not text:
            await message.answer("Значение не может быть пустым. Попробуйте снова или /cancel.")
            return
        value = text

    await db.update_plan(plan_id, **{db_field_map[field_name]: value})
    await state.clear()
    plan = await db.get_plan(plan_id)
    await message.answer("✅ Тариф обновлён.")
    if plan:
        key_names = await _key_names_map(db)
        text_out = sub_logic.plan_summary_text(plan, key_names)
        await message.answer(text_out, reply_markup=admin_plan_view_menu(plan))


@router.callback_query(F.data.startswith("admin:plan_models:"))
async def cb_admin_plan_models(callback: CallbackQuery, is_admin: bool, db: Database) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    plan_id = int(callback.data.split(":")[-1])
    plan = await db.get_plan(plan_id)
    if not plan:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    all_keys = await db.list_api_keys()
    restrictions = await db.get_all_key_model_restrictions("plan", plan_id)
    await safe_edit_text(
        callback.message,
        f"🧠 Модели, разрешённые в тарифе «{escape_html(plan.name)}»:\n"
        f"Нажмите, чтобы включить/выключить весь ключ. Для ключей с режимом "
        f"«все модели» доступна кнопка 🎯/🌐 — точечно ограничить, КАКИЕ ИМЕННО "
        f"модели этого ключа доступны в этом тарифе (по умолчанию — все).",
        reply_markup=admin_plan_models_menu(plan, all_keys, restricted_key_ids=set(restrictions.keys())),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:plan_model_toggle:"))
async def cb_admin_plan_model_toggle(callback: CallbackQuery, is_admin: bool, db: Database) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    _, _, plan_id_str, key_id_str = callback.data.split(":")
    plan_id, key_id = int(plan_id_str), int(key_id_str)
    plan = await db.get_plan(plan_id)
    if not plan:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    allowed = set(plan.allowed_key_ids)
    if key_id in allowed:
        allowed.discard(key_id)
        # Ключ исключён из тарифа целиком — точечное ограничение для него
        # больше не имеет смысла, чистим, чтобы не копить "мусор".
        await db.clear_key_model_restrictions("plan", plan_id, key_id)
    else:
        allowed.add(key_id)
    await db.update_plan(plan_id, allowed_key_ids=sorted(allowed))
    plan = await db.get_plan(plan_id)
    all_keys = await db.list_api_keys()
    restrictions = await db.get_all_key_model_restrictions("plan", plan_id)
    await safe_edit_reply_markup(
        callback.message,
        reply_markup=admin_plan_models_menu(plan, all_keys, restricted_key_ids=set(restrictions.keys())),
    )
    await callback.answer()


# ---------------------------------------------------------------- точечное ограничение моделей внутри ключа (all_models) для ТАРИФА


@router.callback_query(F.data.startswith("admin:plan_key_models:"))
async def cb_admin_plan_key_models(callback: CallbackQuery, is_admin: bool, db: Database, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    _, _, plan_id_str, key_id_str = callback.data.split(":")
    plan_id, key_id = int(plan_id_str), int(key_id_str)
    key = await db.get_api_key(key_id)
    if not key:
        await callback.answer("Ключ не найден", show_alert=True)
        return

    await callback.answer("Запрашиваю список моделей у провайдера…")
    try:
        from config import config as app_config
        from providers import list_models

        live_models = await list_models(key, app_config.request_timeout)
    except Exception as e:  # noqa: BLE001
        await callback.message.answer(f"⚠️ Не удалось получить список моделей: {e}")
        return
    if not live_models:
        await callback.message.answer("Провайдер не вернул список моделей.")
        return

    live_models = live_models[:100]
    await state.update_data(**{f"admin_live_models_plan_{plan_id}_{key_id}": live_models})

    current_restriction = await db.get_key_model_restrictions("plan", plan_id, key_id)
    await safe_edit_text(
        callback.message,
        f"🎯 Выберите, какие модели ключа «{escape_html(key.name)}» доступны в этом тарифе "
        f"(если ничего не отмечено — доступны ВСЕ модели ключа):",
        reply_markup=admin_key_model_restriction_menu(
            plan_id, key_id, live_models, set(current_restriction), owner_type="plan"
        ),
    )


@router.callback_query(F.data.startswith("admin:plan_key_model_toggle:"))
async def cb_admin_plan_key_model_toggle(callback: CallbackQuery, is_admin: bool, db: Database, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    _, _, plan_id_str, key_id_str, index_str = callback.data.split(":")
    plan_id, key_id, index = int(plan_id_str), int(key_id_str), int(index_str)

    data = await state.get_data()
    live_models = data.get(f"admin_live_models_plan_{plan_id}_{key_id}")
    if not live_models or not (0 <= index < len(live_models)):
        await callback.answer("Список моделей устарел, откройте настройку заново.", show_alert=True)
        return
    model_name = live_models[index]

    current = set(await db.get_key_model_restrictions("plan", plan_id, key_id))
    if model_name in current:
        current.discard(model_name)
    else:
        current.add(model_name)
    await db.set_key_model_restrictions("plan", plan_id, key_id, sorted(current))

    key = await db.get_api_key(key_id)
    await safe_edit_reply_markup(
        callback.message,
        reply_markup=admin_key_model_restriction_menu(plan_id, key_id, live_models, current, owner_type="plan"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:plan_key_models_clear:"))
async def cb_admin_plan_key_models_clear(callback: CallbackQuery, is_admin: bool, db: Database) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    _, _, plan_id_str, key_id_str = callback.data.split(":")
    plan_id, key_id = int(plan_id_str), int(key_id_str)
    await db.clear_key_model_restrictions("plan", plan_id, key_id)
    await callback.answer("✅ Ограничение снято — доступны все модели ключа", show_alert=True)
    plan = await db.get_plan(plan_id)
    all_keys = await db.list_api_keys()
    restrictions = await db.get_all_key_model_restrictions("plan", plan_id)
    await safe_edit_text(
        callback.message,
        f"🧠 Модели, разрешённые в тарифе «{escape_html(plan.name)}»:",
        reply_markup=admin_plan_models_menu(plan, all_keys, restricted_key_ids=set(restrictions.keys())),
    )


@router.callback_query(F.data.startswith("admin:plan_toggle:"))
async def cb_admin_plan_toggle(callback: CallbackQuery, is_admin: bool, db: Database) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    plan_id = int(callback.data.split(":")[-1])
    plan = await db.get_plan(plan_id)
    if not plan:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    await db.set_plan_active(plan_id, not plan.is_active)
    await callback.answer("Статус изменён")
    plan = await db.get_plan(plan_id)
    key_names = await _key_names_map(db)
    text = sub_logic.plan_summary_text(plan, key_names) + f"\n\nСтатус: {'🟢 виден в витрине' if plan.is_active else '🔴 скрыт'}"
    await safe_edit_text(callback.message, text, reply_markup=admin_plan_view_menu(plan))


@router.callback_query(F.data.startswith("admin:plan_delete_ask:"))
async def cb_admin_plan_delete_ask(callback: CallbackQuery, is_admin: bool) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    plan_id = int(callback.data.split(":")[-1])
    await safe_edit_text(callback.message, "⚠️ Удалить тариф? Все подписки пользователей на него тоже будут аннулированы.",
        reply_markup=confirm_delete_plan(plan_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:plan_delete_confirm:"))
async def cb_admin_plan_delete_confirm(callback: CallbackQuery, is_admin: bool, db: Database) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    plan_id = int(callback.data.split(":")[-1])
    await db.delete_plan(plan_id)
    await callback.answer("🗑 Тариф удалён", show_alert=True)
    plans = await db.list_plans()
    await safe_edit_text(callback.message, "💳 <b>Тарифы</b>", reply_markup=admin_plans_menu(plans))


# =====================================================================
# ПОЛЬЗОВАТЕЛИ И ПОДПИСКИ
# =====================================================================


@router.callback_query(F.data == "admin:users")
async def cb_admin_users(callback: CallbackQuery, is_admin: bool, db: Database) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    subs = await db.list_subscriptions()
    ids = [s.telegram_id for s in subs]
    text = "👤 <b>Пользователи с подпиской</b>\n\nВыберите пользователя или найдите по ID." if ids else \
        "👤 <b>Пользователи с подпиской</b>\n\nПодписчиков пока нет. Найдите пользователя по ID, чтобы выдать подписку."
    await safe_edit_text(callback.message, text, reply_markup=admin_users_menu(ids))
    await callback.answer()


@router.callback_query(F.data == "admin:user_find")
async def cb_admin_user_find(callback: CallbackQuery, is_admin: bool, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    await state.set_state(GrantSubscriptionStates.waiting_telegram_id)
    await safe_edit_text(callback.message, "Отправьте Telegram ID пользователя (число).",
        reply_markup=cancel_menu("admin:users"),
    )
    await callback.answer()


@router.message(GrantSubscriptionStates.waiting_telegram_id)
async def process_user_find(message: Message, state: FSMContext, is_admin: bool, db: Database) -> None:
    text = (message.text or "").strip()
    if not text.lstrip("-").isdigit():
        await message.answer("❗ Нужен числовой Telegram ID. Попробуйте снова или /cancel.")
        return
    telegram_id = int(text)
    await state.clear()
    await _show_user_view(message, db, telegram_id, is_admin)


async def _show_user_view(message_or_callback, db: Database, telegram_id: int, is_admin: bool) -> None:
    # Роль ЦЕЛЕВОГО пользователя (не того, кто смотрит карточку!) — нужна,
    # чтобы правильно показать его подписку (например, если он сам админ —
    # у него виртуальный admin-план, а не обычная/кастомная подписка).
    from config import config as app_config

    target_role = await roles.get_user_role(db, app_config, telegram_id)
    kind, expires_at = await sub_logic.get_subscription_kind_and_expiry(db, telegram_id, target_role.is_admin)

    profile = await db.get_user_profile(telegram_id)
    is_banned = bool(profile and profile.is_banned)
    is_frozen = bool(profile and profile.is_frozen)

    lines = [f"👤 <b>Пользователь</b> <code>{telegram_id}</code>\n"]
    lines.append(f"Права: <b>{target_role.label}</b>")

    has_custom_subscription = False
    if kind == "admin":
        lines.append("Подписка: служебная (admin), бессрочно")
        has_subscription = False  # нет смысла предлагать выдать/забрать обычную подписку
    elif kind == "none":
        lines.append("Подписка: нету")
        has_subscription = False
    else:
        sub_type, name = kind.split(":", 1)
        label = "Тариф" if sub_type == "plan" else "Кастомная подписка"
        lines.append(f"{label}: {escape_html(name)}")
        lines.append(f"Осталось: {sub_logic.format_time_left(expires_at)}")
        has_subscription = True
        has_custom_subscription = sub_type == "custom"

    if is_frozen:
        lines.append("⏸ Подписка ЗАМОРОЖЕНА")
    if is_banned:
        reason = f" (причина: {escape_html(profile.banned_reason)})" if profile and profile.banned_reason else ""
        lines.append(f"🚫 Пользователь ЗАБЛОКИРОВАН{reason}")

    overrides = await db.get_user_key_overrides(telegram_id)
    if overrides:
        lines.append(f"\nПерсональных переопределений моделей: {len(overrides)}")
    limit_override = await db.get_user_limit_override(telegram_id)
    if limit_override:
        lines.append("Есть персональные лимиты (переопределяют тариф).")

    text = "\n".join(lines)
    markup = admin_user_view_menu(
        telegram_id, has_subscription=has_subscription, is_banned=is_banned, is_frozen=is_frozen,
        has_custom_subscription=has_custom_subscription,
    )
    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(text, reply_markup=markup)
    else:
        await safe_edit_text(message_or_callback.message, text, reply_markup=markup)


@router.callback_query(F.data.startswith("admin:user_view:"))
async def cb_admin_user_view(callback: CallbackQuery, is_admin: bool, db: Database) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    telegram_id = int(callback.data.split(":")[-1])
    await _show_user_view(callback, db, telegram_id, is_admin)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:user_reset_limits:"))
async def cb_admin_user_reset_limits(callback: CallbackQuery, is_admin: bool, db: Database) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    telegram_id = int(callback.data.split(":")[-1])
    count = await db.reset_user_rate_limits(telegram_id)
    await callback.answer(f"🔄 Лимиты сброшены (удалено записей: {count})", show_alert=True)
    await _show_user_view(callback, db, telegram_id, is_admin)


@router.callback_query(F.data.startswith("admin:user_freeze_toggle:"))
async def cb_admin_user_freeze_toggle(callback: CallbackQuery, is_admin: bool, db: Database) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    telegram_id = int(callback.data.split(":")[-1])
    profile = await db.get_user_profile(telegram_id)
    is_frozen_now = bool(profile and profile.is_frozen)
    if is_frozen_now:
        await db.unfreeze_subscription(telegram_id)
        await callback.answer("▶️ Подписка разморожена, остаток срока восстановлен", show_alert=True)
        try:
            await callback.bot.send_message(telegram_id, "▶️ Ваша подписка разморожена администратором.")
        except Exception:  # noqa: BLE001
            pass
    else:
        await db.freeze_subscription(telegram_id)
        await callback.answer("⏸ Подписка заморожена", show_alert=True)
        try:
            await callback.bot.send_message(
                telegram_id, "⏸ Ваша подписка временно заморожена администратором."
            )
        except Exception:  # noqa: BLE001
            pass
    await _show_user_view(callback, db, telegram_id, is_admin)


@router.callback_query(F.data.startswith("admin:user_ban_toggle:"))
async def cb_admin_user_ban_toggle(callback: CallbackQuery, is_admin: bool, db: Database, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    telegram_id = int(callback.data.split(":")[-1])
    profile = await db.get_user_profile(telegram_id)
    is_banned_now = bool(profile and profile.is_banned)
    if is_banned_now:
        await db.set_user_banned(telegram_id, False)
        await callback.answer("✅ Пользователь разблокирован", show_alert=True)
        try:
            await callback.bot.send_message(telegram_id, "✅ Вы были разблокированы администратором.")
        except Exception:  # noqa: BLE001
            pass
        await _show_user_view(callback, db, telegram_id, is_admin)
    else:
        await state.set_state(BanUserStates.waiting_reason)
        await state.update_data(target_telegram_id=telegram_id)
        await safe_edit_text(callback.message, "Укажите причину блокировки (или отправьте «-», чтобы не указывать):",
            reply_markup=cancel_menu(f"admin:user_view:{telegram_id}"),
        )
        await callback.answer()


@router.message(BanUserStates.waiting_reason)
async def process_ban_reason(message: Message, state: FSMContext, db: Database, is_admin: bool) -> None:
    data = await state.get_data()
    telegram_id = data["target_telegram_id"]
    text = (message.text or "").strip()
    reason = None if text == "-" else text
    await db.set_user_banned(telegram_id, True, reason)
    await state.clear()
    await message.answer(f"🚫 Пользователь <code>{telegram_id}</code> заблокирован.")
    try:
        reason_line = f" Причина: {escape_html(reason)}" if reason else ""
        await message.bot.send_message(telegram_id, f"🚫 Вы заблокированы в этом боте.{reason_line}")
    except Exception:  # noqa: BLE001
        pass
    await _show_user_view(message, db, telegram_id, is_admin)


@router.callback_query(F.data.startswith("admin:user_grant:"))
async def cb_admin_user_grant(callback: CallbackQuery, is_admin: bool, db: Database, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    telegram_id = int(callback.data.split(":")[-1])
    plans = await db.list_plans()
    if not plans:
        await callback.answer("Сначала создайте хотя бы один тариф.", show_alert=True)
        return
    await state.update_data(target_telegram_id=telegram_id)
    await safe_edit_text(callback.message, f"Выберите тариф для пользователя <code>{telegram_id}</code>:",
        reply_markup=plan_choice_menu(plans, f"admin:user_grant_plan:{telegram_id}"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:user_grant_plan:"))
async def cb_admin_user_grant_plan(callback: CallbackQuery, is_admin: bool) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    _, _, telegram_id_str, plan_id_str = callback.data.split(":")
    await safe_edit_text(callback.message, "На какой срок выдать подписку?",
        reply_markup=duration_choice_menu(f"admin:user_grant_duration:{telegram_id_str}:{plan_id_str}"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:user_grant_duration:"))
async def cb_admin_user_grant_duration(
    callback: CallbackQuery, is_admin: bool, db: Database, state: FSMContext
) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    _, _, telegram_id_str, plan_id_str, duration_str = callback.data.split(":")
    telegram_id, plan_id = int(telegram_id_str), int(plan_id_str)

    if duration_str == "custom":
        await state.set_state(GrantSubscriptionStates.waiting_custom_duration)
        await state.update_data(target_telegram_id=telegram_id, target_plan_id=plan_id)
        await safe_edit_text(callback.message, "Введите срок подписки в днях (целое число):",
            reply_markup=cancel_menu("admin:users"),
        )
        await callback.answer()
        return

    duration_days = None if duration_str == "forever" else int(duration_str)
    await db.grant_subscription(telegram_id, plan_id, callback.from_user.id, duration_days)
    plan = await db.get_plan(plan_id)
    await callback.answer("✅ Подписка выдана", show_alert=True)
    await callback.message.answer(
        f"✅ Пользователю <code>{telegram_id}</code> выдан тариф «{escape_html(plan.name) if plan else plan_id}» "
        f"на срок: {'бессрочно' if duration_days is None else f'{duration_days} дн.'}"
    )
    try:
        await callback.bot.send_message(
            telegram_id,
            f"🎉 Вам выдана подписка «{escape_html(plan.name) if plan else ''}»! "
            f"Откройте /start, чтобы начать пользоваться ботом.",
        )
    except Exception:  # noqa: BLE001
        pass
    await _show_user_view(callback, db, telegram_id, is_admin)


@router.message(GrantSubscriptionStates.waiting_custom_duration)
async def process_custom_duration(message: Message, state: FSMContext, db: Database) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("❗ Нужно положительное целое число дней. Попробуйте снова или /cancel.")
        return
    data = await state.get_data()
    telegram_id, plan_id = data["target_telegram_id"], data["target_plan_id"]
    await db.grant_subscription(telegram_id, plan_id, message.from_user.id, int(text))
    plan = await db.get_plan(plan_id)
    await state.clear()
    await message.answer(
        f"✅ Пользователю <code>{telegram_id}</code> выдан тариф «{escape_html(plan.name) if plan else plan_id}» "
        f"на {text} дн."
    )
    try:
        await message.bot.send_message(
            telegram_id,
            f"🎉 Вам выдана подписка «{escape_html(plan.name) if plan else ''}»! "
            f"Откройте /start, чтобы начать пользоваться ботом.",
        )
    except Exception:  # noqa: BLE001
        pass


@router.callback_query(F.data.startswith("admin:user_revoke:"))
async def cb_admin_user_revoke(callback: CallbackQuery, is_admin: bool, db: Database) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    telegram_id = int(callback.data.split(":")[-1])
    # revoke_any_subscription (а не revoke_subscription!) отзывает ЛЮБОЙ тип
    # подписки — обычную ИЛИ кастомную, какая бы ни была выдана. Раньше здесь
    # вызывался revoke_subscription, который трогал только обычные тарифы —
    # из-за этого кнопка "Забрать подписку" не работала для кастомных подписок.
    await db.revoke_any_subscription(telegram_id)
    await callback.answer("🗑 Подписка отозвана", show_alert=True)
    await _show_user_view(callback, db, telegram_id, is_admin)


@router.callback_query(F.data.startswith("admin:user_models:"))
async def cb_admin_user_models(callback: CallbackQuery, is_admin: bool, db: Database) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    telegram_id = int(callback.data.split(":")[-1])
    all_keys = await db.list_api_keys()
    overrides = await db.get_user_key_overrides(telegram_id)
    await safe_edit_text(callback.message, f"🧠 Персональный доступ к моделям для <code>{telegram_id}</code>\n"
        f"Нажатие переключает: по тарифу → разрешено лично → запрещено лично → по тарифу.",
        reply_markup=admin_user_models_menu(telegram_id, all_keys, overrides),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:user_model_cycle:"))
async def cb_admin_user_model_cycle(callback: CallbackQuery, is_admin: bool, db: Database) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    _, _, telegram_id_str, key_id_str = callback.data.split(":")
    telegram_id, key_id = int(telegram_id_str), int(key_id_str)

    overrides = await db.get_user_key_overrides(telegram_id)
    current = overrides.get(key_id)
    # Цикл: нет переопределения -> allowed=True -> allowed=False -> нет переопределения
    if current is None:
        await db.set_user_key_override(telegram_id, key_id, True)
    elif current is True:
        await db.set_user_key_override(telegram_id, key_id, False)
    else:
        await db.clear_user_key_override(telegram_id, key_id)

    all_keys = await db.list_api_keys()
    overrides = await db.get_user_key_overrides(telegram_id)
    await safe_edit_reply_markup(callback.message, reply_markup=admin_user_models_menu(telegram_id, all_keys, overrides)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:user_limits:"))
async def cb_admin_user_limits(callback: CallbackQuery, is_admin: bool, db: Database) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    telegram_id = int(callback.data.split(":")[-1])
    override = await db.get_user_limit_override(telegram_id)
    text = f"🚦 Личные лимиты для <code>{telegram_id}</code>\n\n"
    if override:
        text += (
            f"В минуту: {override['rpm_limit'] or 'без лимита'}\n"
            f"В час: {override['rph_limit'] or 'без лимита'}\n"
            f"В сутки: {override['rpd_limit'] or 'без лимита'}\n\n"
            f"Эти значения ЗАМЕНЯЮТ лимиты тарифа."
        )
    else:
        text += "Персональных лимитов нет — используются лимиты тарифа."
    await safe_edit_text(callback.message, text, reply_markup=admin_user_limits_menu(telegram_id, override is not None))
    await callback.answer()


@router.callback_query(F.data.startswith("admin:user_limit_edit:"))
async def cb_admin_user_limit_edit(callback: CallbackQuery, is_admin: bool, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    _, _, telegram_id_str, field = callback.data.split(":")
    await state.set_state(GrantSubscriptionStates.waiting_limit_value)
    await state.update_data(target_telegram_id=int(telegram_id_str), limit_field=field)
    labels = {"rpm": "в минуту", "rph": "в час", "rpd": "в сутки"}
    await safe_edit_text(callback.message, f"Введите личный лимит запросов {labels.get(field, field)} (число или «-» для безлимита):",
        reply_markup=cancel_menu(f"admin:user_limits:{telegram_id_str}"),
    )
    await callback.answer()


@router.message(GrantSubscriptionStates.waiting_limit_value)
async def process_user_limit_value(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    ok, value = _parse_optional_int(message.text or "")
    if not ok:
        await message.answer("❗ Нужно положительное целое число или «-». Попробуйте снова или /cancel.")
        return

    telegram_id = data["target_telegram_id"]
    field = data["limit_field"]
    current = await db.get_user_limit_override(telegram_id) or {"rpm_limit": None, "rph_limit": None, "rpd_limit": None}
    field_map = {"rpm": "rpm_limit", "rph": "rph_limit", "rpd": "rpd_limit"}
    current[field_map[field]] = value
    await db.set_user_limit_override(
        telegram_id, rpm_limit=current["rpm_limit"], rph_limit=current["rph_limit"], rpd_limit=current["rpd_limit"]
    )
    await state.clear()
    await message.answer(f"✅ Личный лимит для <code>{telegram_id}</code> обновлён.")


@router.callback_query(F.data.startswith("admin:user_limit_reset:"))
async def cb_admin_user_limit_reset(callback: CallbackQuery, is_admin: bool, db: Database) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    telegram_id = int(callback.data.split(":")[-1])
    await db.clear_user_limit_override(telegram_id)
    await callback.answer("↩️ Сброшено на лимиты тарифа", show_alert=True)
    override = await db.get_user_limit_override(telegram_id)
    text = f"🚦 Личные лимиты для <code>{telegram_id}</code>\n\nПерсональных лимитов нет — используются лимиты тарифа."
    await safe_edit_text(callback.message, text, reply_markup=admin_user_limits_menu(telegram_id, override is not None))


# =====================================================================
# ЗАЯВКИ НА ПОКУПКУ
# =====================================================================


@router.callback_query(F.data == "admin:purchase_requests")
async def cb_admin_purchase_requests(callback: CallbackQuery, is_admin: bool, db: Database) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    requests = await db.list_purchase_requests(only_pending=True)
    pairs = []
    for req in requests:
        plan = await db.get_plan(req.plan_id)
        pairs.append((req, plan))
    text = "📨 <b>Заявки на покупку</b>\n\n" + (
        "Нет необработанных заявок." if not pairs else
        "Нажмите на пользователя, чтобы выдать подписку, либо отметьте заявку обработанной."
    )
    await safe_edit_text(callback.message, text, reply_markup=admin_purchase_requests_menu(pairs))
    await callback.answer()


@router.callback_query(F.data.startswith("admin:purchase_handled:"))
async def cb_admin_purchase_handled(callback: CallbackQuery, is_admin: bool, db: Database) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    request_id = int(callback.data.split(":")[-1])
    await db.mark_purchase_request_handled(request_id)
    await callback.answer("Отмечено как обработано", show_alert=True)
    await cb_admin_purchase_requests(callback, is_admin, db)


# =====================================================================
# КОНТАКТ ДЛЯ ОПЛАТЫ
# =====================================================================


@router.callback_query(F.data == "admin:payment_contact")
async def cb_admin_payment_contact(callback: CallbackQuery, is_admin: bool, db: Database, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    current = await db.get_bot_setting("payment_contact") or "не задан"
    await state.set_state(PaymentContactStates.waiting_contact)
    await safe_edit_text(callback.message, f"💬 Текущий контакт для оплаты: {escape_html(current)}\n\n"
        f"Отправьте новый контакт (например «@my_username»):",
        reply_markup=cancel_menu("admin:menu"),
    )
    await callback.answer()


@router.message(PaymentContactStates.waiting_contact)
async def process_payment_contact(message: Message, state: FSMContext, db: Database, is_admin: bool) -> None:
    contact = (message.text or "").strip()
    if not contact:
        await message.answer("Контакт не может быть пустым. Попробуйте снова или /cancel.")
        return
    await db.set_bot_setting("payment_contact", contact)
    await state.clear()
    await message.answer(f"✅ Контакт для оплаты обновлён: {escape_html(contact)}", reply_markup=main_menu(is_admin))


# =====================================================================
# КАСТОМНЫЕ ПОДПИСКИ (выдача персонального набора моделей/лимитов)
# =====================================================================


@router.callback_query(F.data.startswith("admin:user_custom_grant:"))
async def cb_admin_user_custom_grant(callback: CallbackQuery, is_admin: bool, db: Database, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    telegram_id = int(callback.data.split(":")[-1])
    await state.update_data(custom_target_id=telegram_id, custom_selected_keys=[])
    all_keys = await db.list_api_keys()
    await safe_edit_text(callback.message, f"🌟 Выбор моделей для кастомной подписки пользователя <code>{telegram_id}</code>:",
        reply_markup=custom_plan_models_menu(telegram_id, all_keys, set()),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:custom_model_toggle:"))
async def cb_admin_custom_model_toggle(callback: CallbackQuery, is_admin: bool, db: Database, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    _, _, telegram_id_str, key_id_str = callback.data.split(":")
    telegram_id, key_id = int(telegram_id_str), int(key_id_str)
    data = await state.get_data()
    selected = set(data.get("custom_selected_keys", []))
    if key_id in selected:
        selected.discard(key_id)
    else:
        selected.add(key_id)
    await state.update_data(custom_selected_keys=sorted(selected))
    all_keys = await db.list_api_keys()
    await safe_edit_reply_markup(callback.message, reply_markup=custom_plan_models_menu(telegram_id, all_keys, selected))
    await callback.answer()


@router.callback_query(F.data.startswith("admin:custom_models_done:"))
async def cb_admin_custom_models_done(callback: CallbackQuery, is_admin: bool, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    telegram_id = int(callback.data.split(":")[-1])
    await state.set_state(CustomSubscriptionStates.waiting_name)
    await safe_edit_text(callback.message, "Введите название этой кастомной подписки (например «VIP индивидуальный»):",
        reply_markup=cancel_menu(f"admin:user_view:{telegram_id}"),
    )
    await callback.answer()


@router.message(CustomSubscriptionStates.waiting_name)
async def process_custom_sub_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым. Попробуйте снова или /cancel.")
        return
    await state.update_data(custom_name=name)
    data = await state.get_data()
    telegram_id = data["custom_target_id"]
    await message.answer(
        "На какой срок выдать эту подписку?",
        reply_markup=duration_choice_menu(f"admin:custom_duration:{telegram_id}"),
    )


@router.callback_query(F.data.startswith("admin:custom_duration:"))
async def cb_admin_custom_duration(callback: CallbackQuery, is_admin: bool, db: Database, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    _, _, telegram_id_str, duration_str = callback.data.split(":")
    telegram_id = int(telegram_id_str)

    if duration_str == "custom":
        await state.set_state(CustomSubscriptionStates.waiting_custom_duration)
        await safe_edit_text(callback.message, "Введите срок подписки в днях (целое число):",
            reply_markup=cancel_menu(f"admin:user_view:{telegram_id}"),
        )
        await callback.answer()
        return

    duration_days = None if duration_str == "forever" else int(duration_str)
    await _finish_custom_grant(callback, db, state, telegram_id, duration_days, is_admin)


@router.message(CustomSubscriptionStates.waiting_custom_duration)
async def process_custom_duration_value(message: Message, state: FSMContext, db: Database, is_admin: bool) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("❗ Нужно положительное целое число дней. Попробуйте снова или /cancel.")
        return
    data = await state.get_data()
    telegram_id = data["custom_target_id"]
    await _finish_custom_grant(message, db, state, telegram_id, int(text), is_admin)


async def _finish_custom_grant(message_or_callback, db: Database, state: FSMContext, telegram_id: int, duration_days, is_admin: bool) -> None:
    data = await state.get_data()
    granted_by = (
        message_or_callback.from_user.id
        if isinstance(message_or_callback, Message)
        else message_or_callback.from_user.id
    )
    await db.grant_custom_subscription(
        telegram_id,
        name=data["custom_name"],
        allowed_key_ids=data.get("custom_selected_keys", []),
        rpm_limit=None,
        rph_limit=None,
        rpd_limit=None,
        granted_by=granted_by,
        duration_days=duration_days,
    )
    await state.clear()

    bot = message_or_callback.bot
    duration_text = "бессрочно" if duration_days is None else f"{duration_days} дн."
    text_out = f"✅ Пользователю <code>{telegram_id}</code> выдана кастомная подписка «{escape_html(data['custom_name'])}» на срок: {duration_text}."
    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(text_out)
    else:
        await message_or_callback.answer("✅ Кастомная подписка выдана", show_alert=True)
        await message_or_callback.message.answer(text_out)
    try:
        await bot.send_message(
            telegram_id,
            f"🎉 Вам выдана персональная подписка «{escape_html(data['custom_name'])}»! "
            f"Откройте /start, чтобы начать пользоваться ботом.",
        )
    except Exception:  # noqa: BLE001
        pass

    await _show_user_view(message_or_callback, db, telegram_id, is_admin)


# =====================================================================
# УПРАВЛЕНИЕ АДМИНИСТРАТОРАМИ (только владелец)
# =====================================================================


@router.callback_query(F.data == "admin:manage_admins")
async def cb_manage_admins(callback: CallbackQuery, is_owner: bool, db: Database) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    admin_ids = await db.list_bot_admins()
    text = "🛡 <b>Администраторы бота</b>\n\n" + (
        "Динамически назначенных администраторов пока нет." if not admin_ids else
        "Список администраторов, назначенных через бота (плюс те, что заданы статично в config.py):"
    )
    await safe_edit_text(callback.message, text, reply_markup=manage_admins_menu(admin_ids))
    await callback.answer()


@router.callback_query(F.data == "admin:add_admin")
async def cb_add_admin_start(callback: CallbackQuery, is_owner: bool, state: FSMContext) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    await state.set_state(ManageAdminsStates.waiting_add_id)
    await safe_edit_text(callback.message, "Отправьте Telegram ID пользователя, которого нужно назначить администратором:",
        reply_markup=cancel_menu("admin:manage_admins"),
    )
    await callback.answer()


@router.message(ManageAdminsStates.waiting_add_id)
async def process_add_admin(message: Message, state: FSMContext, db: Database) -> None:
    text = (message.text or "").strip()
    if not text.lstrip("-").isdigit():
        await message.answer("❗ Нужен числовой Telegram ID. Попробуйте снова или /cancel.")
        return
    telegram_id = int(text)
    await db.set_bot_admin(telegram_id, True)
    await state.clear()
    await message.answer(f"✅ Пользователь <code>{telegram_id}</code> назначен администратором.")
    try:
        await message.bot.send_message(
            telegram_id, "🛡 Вы назначены администратором этого бота. Откройте /start, чтобы увидеть админ-панель."
        )
    except Exception:  # noqa: BLE001
        pass
    admin_ids = await db.list_bot_admins()
    await message.answer("🛡 <b>Администраторы бота</b>", reply_markup=manage_admins_menu(admin_ids))


@router.callback_query(F.data.startswith("admin:remove_admin:"))
async def cb_remove_admin(callback: CallbackQuery, is_owner: bool, db: Database) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    telegram_id = int(callback.data.split(":")[-1])
    await db.set_bot_admin(telegram_id, False)
    await callback.answer("➖ Администратор снят", show_alert=True)
    try:
        await callback.bot.send_message(telegram_id, "ℹ️ Вы больше не являетесь администратором этого бота.")
    except Exception:  # noqa: BLE001
        pass
    admin_ids = await db.list_bot_admins()
    await safe_edit_text(callback.message, "🛡 <b>Администраторы бота</b>", reply_markup=manage_admins_menu(admin_ids))


# =====================================================================
# РАССЫЛКА ВСЕМ ПОЛЬЗОВАТЕЛЯМ
# =====================================================================


@router.callback_query(F.data == "admin:broadcast")
async def cb_broadcast_start(callback: CallbackQuery, is_admin: bool, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    await state.set_state(BroadcastStates.waiting_text)
    await safe_edit_text(callback.message, "📢 Введите текст сообщения для рассылки ВСЕМ пользователям, которые хоть раз запускали /start.\n"
        "Поддерживается HTML-разметка Telegram.",
        reply_markup=cancel_menu("admin:menu"),
    )
    await callback.answer()


@router.message(BroadcastStates.waiting_text)
async def process_broadcast_text(message: Message, state: FSMContext, db: Database) -> None:
    text = message.html_text or message.text or ""
    if not text.strip():
        await message.answer("Текст не может быть пустым. Попробуйте снова или /cancel.")
        return
    await state.update_data(broadcast_text=text)
    total = len(await db.list_all_user_ids())
    await message.answer(
        f"Получателей: {total}.\n\n--- Предпросмотр ---\n{text}\n--- конец предпросмотра ---\n\nОтправить?",
        reply_markup=broadcast_confirm_menu(),
    )


@router.callback_query(F.data == "admin:broadcast_confirm")
async def cb_broadcast_confirm(callback: CallbackQuery, is_admin: bool, db: Database, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    data = await state.get_data()
    text = data.get("broadcast_text")
    if not text:
        await callback.answer("Текст рассылки потерян, начните заново.", show_alert=True)
        return
    await state.clear()
    await callback.answer("📢 Рассылка запущена…", show_alert=True)

    user_ids = await db.list_all_user_ids()
    success = 0
    fail = 0
    for uid in user_ids:
        try:
            await callback.bot.send_message(uid, text)
            success += 1
        except Exception:  # noqa: BLE001
            fail += 1

    await db.log_broadcast(callback.from_user.id, text, len(user_ids), success, fail)
    await callback.message.answer(
        f"✅ Рассылка завершена.\nВсего получателей: {len(user_ids)}\nУспешно: {success}\nНе удалось: {fail}"
    )


# =====================================================================
# УПРАВЛЕНИЕ УЖЕ ВЫДАННОЙ КАСТОМНОЙ ПОДПИСКОЙ (полный паритет с тарифами)
# =====================================================================
# Раньше кастомную подписку можно было только ВЫДАТЬ, но не отредактировать/
# забрать (кнопка "Забрать подписку" была сломана — вызывала revoke_subscription,
# которая трогает только обычные тарифы) и не изменить после выдачи. Теперь
# доступно полноценное управление: название, модели, лимиты, срок действия,
# заморозка/разморозка, отзыв — всё то же самое, что есть у обычных тарифов.


async def _show_custom_manage_view(message_or_callback, db: Database, telegram_id: int) -> None:
    custom = await db.get_custom_subscription(telegram_id)
    if custom is None:
        text = f"У пользователя <code>{telegram_id}</code> нет кастомной подписки (возможно, истекла или была отозвана)."
        markup = None
    else:
        profile = await db.get_user_profile(telegram_id)
        is_frozen = bool(profile and profile.is_frozen)
        key_names = await _key_names_map(db)
        lines = [
            f"🌟 <b>Кастомная подписка</b> пользователя <code>{telegram_id}</code>\n",
            f"Название: {escape_html(custom.name)}",
        ]
        if custom.allowed_key_ids:
            names = [key_names.get(kid, f"#{kid}") for kid in custom.allowed_key_ids]
            lines.append("Модели: " + ", ".join(names))
        else:
            lines.append("Модели: не назначены")
        limits = []
        if custom.rpm_limit:
            limits.append(f"{custom.rpm_limit}/мин")
        if custom.rph_limit:
            limits.append(f"{custom.rph_limit}/час")
        if custom.rpd_limit:
            limits.append(f"{custom.rpd_limit}/сутки")
        lines.append("Лимиты: " + (", ".join(limits) if limits else "без ограничений"))
        lines.append(f"Осталось: {sub_logic.format_time_left(custom.expires_at)}")
        if is_frozen:
            lines.append("⏸ Подписка ЗАМОРОЖЕНА (срок не идёт)")
        text = "\n".join(lines)
        markup = admin_custom_manage_menu(telegram_id, is_frozen=is_frozen)

    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(text, reply_markup=markup)
    else:
        await safe_edit_text(message_or_callback.message, text, reply_markup=markup)


@router.callback_query(F.data.startswith("admin:custom_manage:"))
async def cb_custom_manage(callback: CallbackQuery, is_admin: bool, db: Database) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    telegram_id = int(callback.data.split(":")[-1])
    await _show_custom_manage_view(callback, db, telegram_id)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:custom_edit_name:"))
async def cb_custom_edit_name(callback: CallbackQuery, is_admin: bool, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    telegram_id = int(callback.data.split(":")[-1])
    await state.set_state(CustomSubscriptionEditStates.waiting_name)
    await state.update_data(custom_edit_target_id=telegram_id)
    await safe_edit_text(
        callback.message,
        "Введите новое название кастомной подписки:",
        reply_markup=cancel_menu(f"admin:custom_manage:{telegram_id}"),
    )
    await callback.answer()


@router.message(CustomSubscriptionEditStates.waiting_name)
async def process_custom_edit_name(message: Message, state: FSMContext, db: Database) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым. Попробуйте снова или /cancel.")
        return
    data = await state.get_data()
    telegram_id = data["custom_edit_target_id"]
    # update_custom_subscription НЕ трогает expires_at — переименование
    # никак не влияет на оставшийся срок действия подписки.
    await db.update_custom_subscription(telegram_id, name=name)
    await state.clear()
    await message.answer("✅ Название обновлено.")
    await _show_custom_manage_view(message, db, telegram_id)


@router.callback_query(F.data.startswith("admin:custom_edit_models:"))
async def cb_custom_edit_models(callback: CallbackQuery, is_admin: bool, db: Database, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    telegram_id = int(callback.data.split(":")[-1])
    custom = await db.get_custom_subscription(telegram_id)
    if custom is None:
        await callback.answer("У пользователя нет кастомной подписки.", show_alert=True)
        return
    # Предзаполняем уже выбранные модели — иначе при каждом открытии редактор
    # "сбрасывал" бы выбор, и пришлось бы отмечать заново все ранее выбранные модели.
    await state.update_data(custom_edit_target_id=telegram_id, custom_edit_selected_keys=list(custom.allowed_key_ids))
    all_keys = await db.list_api_keys()
    restrictions = await db.get_all_key_model_restrictions("custom", telegram_id)
    await safe_edit_text(
        callback.message,
        f"🧠 Модели кастомной подписки пользователя <code>{telegram_id}</code>:\n"
        f"Для ключей с режимом «все модели» доступна кнопка 🎯/🌐 — точечно "
        f"ограничить, какие ИМЕННО модели ключа доступны (по умолчанию — все).",
        reply_markup=custom_edit_models_menu(
            telegram_id, all_keys, set(custom.allowed_key_ids), restricted_key_ids=set(restrictions.keys())
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:custom_edit_model_toggle:"))
async def cb_custom_edit_model_toggle(callback: CallbackQuery, is_admin: bool, db: Database, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    _, _, telegram_id_str, key_id_str = callback.data.split(":")
    telegram_id, key_id = int(telegram_id_str), int(key_id_str)
    data = await state.get_data()
    selected = set(data.get("custom_edit_selected_keys", []))
    if key_id in selected:
        selected.discard(key_id)
        # Ключ исключён из подписки целиком — точечное ограничение для него
        # больше не имеет смысла, чистим, чтобы не копить "мусор".
        await db.clear_key_model_restrictions("custom", telegram_id, key_id)
    else:
        selected.add(key_id)
    await state.update_data(custom_edit_selected_keys=sorted(selected))
    all_keys = await db.list_api_keys()
    restrictions = await db.get_all_key_model_restrictions("custom", telegram_id)
    await safe_edit_reply_markup(
        callback.message,
        reply_markup=custom_edit_models_menu(telegram_id, all_keys, selected, restricted_key_ids=set(restrictions.keys())),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:custom_edit_models_done:"))
async def cb_custom_edit_models_done(callback: CallbackQuery, is_admin: bool, db: Database, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    telegram_id = int(callback.data.split(":")[-1])
    data = await state.get_data()
    selected = data.get("custom_edit_selected_keys", [])
    # update_custom_subscription НЕ трогает expires_at — смена набора моделей
    # никак не влияет на оставшийся срок действия подписки.
    await db.update_custom_subscription(telegram_id, allowed_key_ids=selected)
    await state.clear()
    await callback.answer("✅ Модели обновлены", show_alert=True)
    await _show_custom_manage_view(callback, db, telegram_id)


# ---------------------------------------------------------------- точечное ограничение моделей внутри ключа (all_models) для КАСТОМНОЙ ПОДПИСКИ


@router.callback_query(F.data.startswith("admin:custom_key_models:"))
async def cb_custom_key_models(callback: CallbackQuery, is_admin: bool, db: Database, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    _, _, telegram_id_str, key_id_str = callback.data.split(":")
    telegram_id, key_id = int(telegram_id_str), int(key_id_str)
    key = await db.get_api_key(key_id)
    if not key:
        await callback.answer("Ключ не найден", show_alert=True)
        return

    await callback.answer("Запрашиваю список моделей у провайдера…")
    try:
        from config import config as app_config
        from providers import list_models

        live_models = await list_models(key, app_config.request_timeout)
    except Exception as e:  # noqa: BLE001
        await callback.message.answer(f"⚠️ Не удалось получить список моделей: {e}")
        return
    if not live_models:
        await callback.message.answer("Провайдер не вернул список моделей.")
        return

    live_models = live_models[:100]
    await state.update_data(**{f"admin_live_models_custom_{telegram_id}_{key_id}": live_models})

    current_restriction = await db.get_key_model_restrictions("custom", telegram_id, key_id)
    await safe_edit_text(
        callback.message,
        f"🎯 Выберите, какие модели ключа «{escape_html(key.name)}» доступны в этой "
        f"кастомной подписке (если ничего не отмечено — доступны ВСЕ модели ключа):",
        reply_markup=admin_key_model_restriction_menu(
            telegram_id, key_id, live_models, set(current_restriction), owner_type="custom"
        ),
    )


@router.callback_query(F.data.startswith("admin:custom_key_model_toggle:"))
async def cb_custom_key_model_toggle(callback: CallbackQuery, is_admin: bool, db: Database, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    _, _, telegram_id_str, key_id_str, index_str = callback.data.split(":")
    telegram_id, key_id, index = int(telegram_id_str), int(key_id_str), int(index_str)

    data = await state.get_data()
    live_models = data.get(f"admin_live_models_custom_{telegram_id}_{key_id}")
    if not live_models or not (0 <= index < len(live_models)):
        await callback.answer("Список моделей устарел, откройте настройку заново.", show_alert=True)
        return
    model_name = live_models[index]

    current = set(await db.get_key_model_restrictions("custom", telegram_id, key_id))
    if model_name in current:
        current.discard(model_name)
    else:
        current.add(model_name)
    await db.set_key_model_restrictions("custom", telegram_id, key_id, sorted(current))

    await safe_edit_reply_markup(
        callback.message,
        reply_markup=admin_key_model_restriction_menu(telegram_id, key_id, live_models, current, owner_type="custom"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:custom_key_models_clear:"))
async def cb_custom_key_models_clear(callback: CallbackQuery, is_admin: bool, db: Database) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    _, _, telegram_id_str, key_id_str = callback.data.split(":")
    telegram_id, key_id = int(telegram_id_str), int(key_id_str)
    await db.clear_key_model_restrictions("custom", telegram_id, key_id)
    await callback.answer("✅ Ограничение снято — доступны все модели ключа", show_alert=True)
    await _show_custom_manage_view(callback, db, telegram_id)


@router.callback_query(F.data.startswith("admin:custom_edit_limit:"))
async def cb_custom_edit_limit(callback: CallbackQuery, is_admin: bool, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    _, _, telegram_id_str, field = callback.data.split(":")
    telegram_id = int(telegram_id_str)
    await state.set_state(CustomSubscriptionEditStates.waiting_limit_value)
    await state.update_data(custom_edit_target_id=telegram_id, custom_edit_limit_field=field)
    labels = {"rpm": "в минуту", "rph": "в час", "rpd": "в сутки"}
    await safe_edit_text(
        callback.message,
        f"Введите лимит запросов {labels.get(field, field)} (число или «-» для безлимита):",
        reply_markup=cancel_menu(f"admin:custom_manage:{telegram_id}"),
    )
    await callback.answer()


@router.message(CustomSubscriptionEditStates.waiting_limit_value)
async def process_custom_edit_limit_value(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    ok, value = _parse_optional_int(message.text or "")
    if not ok:
        await message.answer("❗ Нужно положительное целое число или «-». Попробуйте снова или /cancel.")
        return
    telegram_id = data["custom_edit_target_id"]
    field = data["custom_edit_limit_field"]
    field_map = {"rpm": "rpm_limit", "rph": "rph_limit", "rpd": "rpd_limit"}
    # update_custom_subscription НЕ трогает expires_at — изменение лимита
    # никак не влияет на оставшийся срок действия подписки.
    await db.update_custom_subscription(telegram_id, **{field_map[field]: value})
    await state.clear()
    await message.answer("✅ Лимит обновлён.")
    await _show_custom_manage_view(message, db, telegram_id)


@router.callback_query(F.data.startswith("admin:custom_edit_duration:"))
async def cb_custom_edit_duration_start(callback: CallbackQuery, is_admin: bool, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    telegram_id = int(callback.data.split(":")[-1])
    await safe_edit_text(
        callback.message,
        "На какой срок (от текущего момента) продлить/изменить эту подписку?",
        reply_markup=duration_choice_menu(f"admin:custom_edit_duration_set:{telegram_id}"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:custom_edit_duration_set:"))
async def cb_custom_edit_duration_set(callback: CallbackQuery, is_admin: bool, db: Database, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    _, _, telegram_id_str, duration_str = callback.data.split(":")
    telegram_id = int(telegram_id_str)

    if duration_str == "custom":
        await state.set_state(CustomSubscriptionEditStates.waiting_duration)
        await state.update_data(custom_edit_target_id=telegram_id)
        await safe_edit_text(
            callback.message,
            "Введите новый срок действия в днях (целое число, отсчитывается от текущего момента):",
            reply_markup=cancel_menu(f"admin:custom_manage:{telegram_id}"),
        )
        await callback.answer()
        return

    duration_days = None if duration_str == "forever" else int(duration_str)
    # set_custom_subscription_duration корректно учитывает заморозку: если
    # подписка сейчас заморожена, новый срок сразу же ставится на паузу, чтобы
    # при разморозке восстановился именно ОН, а не устаревший остаток от
    # предыдущего срока (см. database._resync_freeze_state).
    await db.set_custom_subscription_duration(telegram_id, duration_days)
    await callback.answer("✅ Срок действия обновлён", show_alert=True)
    await _show_custom_manage_view(callback, db, telegram_id)


@router.message(CustomSubscriptionEditStates.waiting_duration)
async def process_custom_edit_duration_value(message: Message, state: FSMContext, db: Database) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("❗ Нужно положительное целое число дней. Попробуйте снова или /cancel.")
        return
    data = await state.get_data()
    telegram_id = data["custom_edit_target_id"]
    await db.set_custom_subscription_duration(telegram_id, int(text))
    await state.clear()
    await message.answer("✅ Срок действия обновлён.")
    await _show_custom_manage_view(message, db, telegram_id)
