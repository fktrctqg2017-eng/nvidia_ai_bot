"""Промокоды.

- Создавать/включать/отключать/удалять/редактировать промокоды может ТОЛЬКО
  владелец бота (см. handlers/admin.py -> "🎟 Промокоды" в админ-панели,
  видна только при is_owner=True).
- Активировать промокод (ввести код) может любой обычный пользователь
  (НЕ администратор и НЕ владелец — см. promo_logic.redeem_promo_code).
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import promo_logic
from database import Database
from keyboards import (
    cancel_menu,
    confirm_delete_promo,
    promo_admin_menu,
    promo_audience_menu,
    promo_custom_models_menu,
    promo_duration_menu,
    promo_plan_choice_menu,
    promo_reward_type_menu,
    promo_view_menu,
)
from states import PromoCreateStates, PromoEditStates, PromoRedeemStates
from text_utils import escape_html, safe_edit_reply_markup, safe_edit_text

router = Router(name="promo")


def _parse_optional_int(text: str) -> tuple[bool, int | None]:
    text = text.strip()
    if text == "-":
        return True, None
    if text.isdigit() and int(text) > 0:
        return True, int(text)
    return False, None


async def _key_names_map(db: Database) -> dict[int, str]:
    keys = await db.list_api_keys()
    return {k.id: (k.model or k.name) for k in keys}


# =====================================================================
# АКТИВАЦИЯ ПРОМОКОДА (любой обычный пользователь)
# =====================================================================


@router.callback_query(F.data == "promo:enter")
async def cb_promo_enter(callback: CallbackQuery, is_admin: bool, state: FSMContext) -> None:
    if is_admin:
        await callback.answer("🚫 Администраторы и владелец не могут использовать промокоды.", show_alert=True)
        return
    await state.set_state(PromoRedeemStates.waiting_code)
    await safe_edit_text(callback.message, "🎁 Введите промокод текстом:",
        reply_markup=cancel_menu("menu:main"),
    )
    await callback.answer()


@router.message(PromoRedeemStates.waiting_code)
async def process_promo_redeem(message: Message, state: FSMContext, db: Database, is_admin: bool) -> None:
    code = (message.text or "").strip()
    if not code:
        await message.answer("Промокод не может быть пустым. Попробуйте снова или /cancel.")
        return
    await state.clear()
    result = await promo_logic.redeem_promo_code(db, message.from_user.id, code, is_admin)
    if result.ok:
        duration_text = "бессрочно" if result.duration_days is None else f"{result.duration_days} дн."
        await message.answer(
            f"🎉 Промокод активирован! Вам выдана подписка «{escape_html(result.reward_name)}» "
            f"на срок: {duration_text}.\nОткройте «👤 Профиль», чтобы увидеть детали."
        )
    else:
        await message.answer(result.reason)


# =====================================================================
# АДМИНКА ПРОМОКОДОВ (только владелец)
# =====================================================================


@router.callback_query(F.data == "promo:admin_menu")
async def cb_promo_admin_menu(callback: CallbackQuery, is_owner: bool, db: Database) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    codes = await db.list_promo_codes()
    text = "🎟 <b>Промокоды</b>\n\n🟢 активен, 🔴 отключен." if codes else "🎟 <b>Промокоды</b>\n\nПромокодов пока нет — создайте первый."
    await safe_edit_text(callback.message, text, reply_markup=promo_admin_menu(codes))
    await callback.answer()


async def _show_promo_view(message_or_callback, db: Database, code: str) -> None:
    promo = await db.get_promo_code(code)
    if promo is None:
        text = "Промокод не найден (возможно, был удалён)."
        markup = promo_admin_menu(await db.list_promo_codes())
    else:
        plan_name = None
        if promo.reward_type == "plan" and promo.plan_id:
            plan = await db.get_plan(promo.plan_id)
            plan_name = plan.name if plan else None
        key_names = await _key_names_map(db)
        text = promo_logic.promo_summary_text(promo, plan_name, key_names)
        markup = promo_view_menu(promo)
    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(text, reply_markup=markup)
    else:
        await safe_edit_text(message_or_callback.message, text, reply_markup=markup)


@router.callback_query(F.data.startswith("promo:view:"))
async def cb_promo_view(callback: CallbackQuery, is_owner: bool, db: Database) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    code = callback.data.split(":", 2)[-1]
    await _show_promo_view(callback, db, code)
    await callback.answer()


@router.callback_query(F.data.startswith("promo:toggle:"))
async def cb_promo_toggle(callback: CallbackQuery, is_owner: bool, db: Database) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    code = callback.data.split(":", 2)[-1]
    promo = await db.get_promo_code(code)
    if not promo:
        await callback.answer("Промокод не найден", show_alert=True)
        return
    await db.set_promo_code_active(code, not promo.is_active)
    await callback.answer("Статус изменён")
    await _show_promo_view(callback, db, code)


@router.callback_query(F.data.startswith("promo:toggle_audience:"))
async def cb_promo_toggle_audience(callback: CallbackQuery, is_owner: bool, db: Database) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    code = callback.data.split(":", 2)[-1]
    promo = await db.get_promo_code(code)
    if not promo:
        await callback.answer("Промокод не найден", show_alert=True)
        return
    new_audience = "subscribers_only" if promo.target_audience == "all" else "all"
    await db.update_promo_code(code, target_audience=new_audience)
    await callback.answer("Аудитория изменена")
    await _show_promo_view(callback, db, code)


@router.callback_query(F.data.startswith("promo:delete_ask:"))
async def cb_promo_delete_ask(callback: CallbackQuery, is_owner: bool) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    code = callback.data.split(":", 2)[-1]
    await safe_edit_text(callback.message, f"⚠️ Удалить промокод «{escape_html(code)}»? Активировавшие его пользователи подписку НЕ потеряют.",
        reply_markup=confirm_delete_promo(code),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("promo:delete_confirm:"))
async def cb_promo_delete_confirm(callback: CallbackQuery, is_owner: bool, db: Database) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    code = callback.data.split(":", 2)[-1]
    await db.delete_promo_code(code)
    await callback.answer("🗑 Промокод удалён", show_alert=True)
    codes = await db.list_promo_codes()
    await safe_edit_text(callback.message, "🎟 <b>Промокоды</b>", reply_markup=promo_admin_menu(codes))


# ---------------------------------------------------------------- редактирование полей промокода


_EDIT_PROMPTS = {
    "duration_days": "Введите новый срок выдаваемой подписки в днях (число или «-» для бессрочной):",
    "max_activations": "Введите новый лимит активаций (число или «-» для безлимита):",
    "valid_from": "Через сколько ДНЕЙ ОТ СЕЙЧАС промокод начнёт действовать (число, «0» — сразу, «-» — без ограничения):",
    "valid_until": "Через сколько ДНЕЙ ОТ СЕЙЧАС промокод перестанет действовать (число, «-» — без ограничения):",
}

_EDIT_DB_FIELDS = {
    "duration_days": "duration_days",
    "max_activations": "max_activations",
    "valid_from": "valid_from",
    "valid_until": "valid_until",
}


@router.callback_query(F.data.startswith("promo:edit:"))
async def cb_promo_edit(callback: CallbackQuery, is_owner: bool, state: FSMContext) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    _, _, code, field = callback.data.split(":")
    await state.set_state(PromoEditStates.editing_field)
    await state.update_data(promo_code=code, field_name=field)
    await safe_edit_text(callback.message, _EDIT_PROMPTS.get(field, "Введите новое значение:"),
        reply_markup=cancel_menu(f"promo:view:{code}"),
    )
    await callback.answer()


@router.message(PromoEditStates.editing_field)
async def process_promo_edit_value(message: Message, state: FSMContext, db: Database) -> None:
    import time as _time

    data = await state.get_data()
    code = data["promo_code"]
    field = data["field_name"]
    text = (message.text or "").strip()

    if field in {"duration_days", "max_activations"}:
        ok, value = _parse_optional_int(text)
        if not ok:
            await message.answer("❗ Нужно положительное целое число или «-». Попробуйте снова или /cancel.")
            return
        await db.update_promo_code(code, **{_EDIT_DB_FIELDS[field]: value})
    elif field in {"valid_from", "valid_until"}:
        if text == "-":
            value = None
        elif text.isdigit():
            value = int(_time.time()) + int(text) * 86400
        else:
            await message.answer("❗ Нужно неотрицательное целое число дней или «-». Попробуйте снова или /cancel.")
            return
        await db.update_promo_code(code, **{_EDIT_DB_FIELDS[field]: value})
    else:
        await message.answer("Неизвестное поле.")
        await state.clear()
        return

    await state.clear()
    await message.answer("✅ Промокод обновлён.")
    await _show_promo_view(message, db, code)


# =====================================================================
# СОЗДАНИЕ НОВОГО ПРОМОКОДА (только владелец) — FSM с несколькими шагами
# =====================================================================


@router.callback_query(F.data == "promo:create_start")
async def cb_promo_create_start(callback: CallbackQuery, is_owner: bool, state: FSMContext) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    await state.set_state(PromoCreateStates.waiting_code_text)
    await safe_edit_text(callback.message, "Введите название промокода (латиница/цифры, например «WELCOME2026»):",
        reply_markup=cancel_menu("promo:admin_menu"),
    )
    await callback.answer()


@router.message(PromoCreateStates.waiting_code_text)
async def process_promo_create_code(message: Message, state: FSMContext, db: Database) -> None:
    code = (message.text or "").strip().upper()
    if not code or " " in code:
        await message.answer("❗ Название не может быть пустым и не должно содержать пробелов. Попробуйте снова или /cancel.")
        return
    existing = await db.get_promo_code(code)
    if existing is not None:
        await message.answer("❗ Промокод с таким названием уже существует. Введите другое название или /cancel.")
        return
    await state.update_data(code=code)
    await state.set_state(None)
    await message.answer(
        f"Промокод «{escape_html(code)}». Что он будет выдавать?",
        reply_markup=promo_reward_type_menu(),
    )


@router.callback_query(F.data.startswith("promo:reward_type:"))
async def cb_promo_reward_type(callback: CallbackQuery, is_owner: bool, db: Database, state: FSMContext) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    reward_type = callback.data.split(":")[-1]
    await state.update_data(reward_type=reward_type)
    if reward_type == "plan":
        plans = await db.list_plans()
        if not plans:
            await callback.answer("Сначала создайте хотя бы один тариф в «💳 Тарифы».", show_alert=True)
            return
        await safe_edit_text(callback.message, "Выберите тариф, который будет выдавать промокод:",
            reply_markup=promo_plan_choice_menu(plans),
        )
    else:
        await state.update_data(custom_selected_keys=[])
        all_keys = await db.list_api_keys()
        await safe_edit_text(callback.message, "Выберите модели, которые будут доступны по этому промокоду:",
            reply_markup=promo_custom_models_menu(all_keys, set()),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("promo:create_plan:"))
async def cb_promo_create_plan(callback: CallbackQuery, is_owner: bool, state: FSMContext) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    plan_id = int(callback.data.split(":")[-1])
    await state.update_data(plan_id=plan_id)
    await safe_edit_text(callback.message, "На какой срок будет выдаваться подписка при активации промокода?",
        reply_markup=promo_duration_menu(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("promo:create_model_toggle:"))
async def cb_promo_create_model_toggle(callback: CallbackQuery, is_owner: bool, db: Database, state: FSMContext) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    key_id = int(callback.data.split(":")[-1])
    data = await state.get_data()
    selected = set(data.get("custom_selected_keys", []))
    if key_id in selected:
        selected.discard(key_id)
    else:
        selected.add(key_id)
    await state.update_data(custom_selected_keys=sorted(selected))
    all_keys = await db.list_api_keys()
    await safe_edit_reply_markup(callback.message, reply_markup=promo_custom_models_menu(all_keys, selected))
    await callback.answer()


@router.callback_query(F.data == "promo:create_models_done")
async def cb_promo_create_models_done(callback: CallbackQuery, is_owner: bool, state: FSMContext) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    await state.set_state(PromoCreateStates.waiting_custom_name)
    await safe_edit_text(callback.message, "Введите название кастомной подписки, которая будет выдаваться по промокоду (например «VIP через промокод»):",
        reply_markup=cancel_menu("promo:admin_menu"),
    )
    await callback.answer()


@router.message(PromoCreateStates.waiting_custom_name)
async def process_promo_custom_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым. Попробуйте снова или /cancel.")
        return
    await state.update_data(custom_name=name)
    await state.set_state(PromoCreateStates.waiting_custom_rpm)
    await message.answer("Лимит запросов В МИНУТУ для этой подписки. Введите число, либо «-» для безлимита.")


@router.message(PromoCreateStates.waiting_custom_rpm)
async def process_promo_custom_rpm(message: Message, state: FSMContext) -> None:
    ok, value = _parse_optional_int(message.text or "")
    if not ok:
        await message.answer("❗ Нужно положительное целое число или «-». Попробуйте снова или /cancel.")
        return
    await state.update_data(custom_rpm=value)
    await state.set_state(PromoCreateStates.waiting_custom_rph)
    await message.answer("Лимит запросов В ЧАС. Введите число, либо «-» для безлимита.")


@router.message(PromoCreateStates.waiting_custom_rph)
async def process_promo_custom_rph(message: Message, state: FSMContext) -> None:
    ok, value = _parse_optional_int(message.text or "")
    if not ok:
        await message.answer("❗ Нужно положительное целое число или «-». Попробуйте снова или /cancel.")
        return
    await state.update_data(custom_rph=value)
    await state.set_state(PromoCreateStates.waiting_custom_rpd)
    await message.answer("Лимит запросов В СУТКИ. Введите число, либо «-» для безлимита.")


@router.message(PromoCreateStates.waiting_custom_rpd)
async def process_promo_custom_rpd(message: Message, state: FSMContext) -> None:
    ok, value = _parse_optional_int(message.text or "")
    if not ok:
        await message.answer("❗ Нужно положительное целое число или «-». Попробуйте снова или /cancel.")
        return
    await state.update_data(custom_rpd=value)
    await state.set_state(None)
    await message.answer(
        "На какой срок будет выдаваться эта подписка при активации промокода?",
        reply_markup=promo_duration_menu(),
    )


@router.callback_query(F.data.startswith("promo:create_duration:"))
async def cb_promo_create_duration(callback: CallbackQuery, is_owner: bool, state: FSMContext) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    duration_str = callback.data.split(":")[-1]
    if duration_str == "custom":
        await state.set_state(PromoCreateStates.waiting_custom_duration)
        await safe_edit_text(callback.message, "Введите срок выдаваемой подписки в днях (целое число):",
            reply_markup=cancel_menu("promo:admin_menu"),
        )
        await callback.answer()
        return

    duration_days = None if duration_str == "forever" else int(duration_str)
    await state.update_data(duration_days=duration_days)
    await state.set_state(PromoCreateStates.waiting_max_activations)
    await safe_edit_text(callback.message, "Сколько РАЗНЫХ пользователей смогут активировать этот промокод? "
        "Введите число, либо «-» для неограниченного количества.",
        reply_markup=cancel_menu("promo:admin_menu"),
    )
    await callback.answer()


@router.message(PromoCreateStates.waiting_custom_duration)
async def process_promo_custom_duration(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("❗ Нужно положительное целое число дней. Попробуйте снова или /cancel.")
        return
    await state.update_data(duration_days=int(text))
    await state.set_state(PromoCreateStates.waiting_max_activations)
    await message.answer(
        "Сколько РАЗНЫХ пользователей смогут активировать этот промокод? "
        "Введите число, либо «-» для неограниченного количества.",
        reply_markup=cancel_menu("promo:admin_menu"),
    )


@router.message(PromoCreateStates.waiting_max_activations)
async def process_promo_max_activations(message: Message, state: FSMContext) -> None:
    ok, value = _parse_optional_int(message.text or "")
    if not ok:
        await message.answer("❗ Нужно положительное целое число или «-». Попробуйте снова или /cancel.")
        return
    await state.update_data(max_activations=value)
    await state.set_state(None)
    await message.answer(
        "Для какой группы пользователей будет доступен этот промокод?",
        reply_markup=promo_audience_menu(),
    )


@router.callback_query(F.data.startswith("promo:create_audience:"))
async def cb_promo_create_audience(callback: CallbackQuery, is_owner: bool, db: Database, state: FSMContext) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    audience = callback.data.split(":")[-1]
    data = await state.get_data()

    code = data["code"]
    reward_type = data["reward_type"]
    duration_days = data.get("duration_days")
    max_activations = data.get("max_activations")

    if reward_type == "plan":
        await db.create_promo_code(
            code=code,
            reward_type="plan",
            created_by=callback.from_user.id,
            plan_id=data["plan_id"],
            duration_days=duration_days,
            max_activations=max_activations,
            target_audience=audience,
        )
    else:
        await db.create_promo_code(
            code=code,
            reward_type="custom",
            created_by=callback.from_user.id,
            custom_name=data.get("custom_name"),
            custom_allowed_key_ids=data.get("custom_selected_keys", []),
            custom_rpm_limit=data.get("custom_rpm"),
            custom_rph_limit=data.get("custom_rph"),
            custom_rpd_limit=data.get("custom_rpd"),
            duration_days=duration_days,
            max_activations=max_activations,
            target_audience=audience,
        )

    await state.clear()
    await callback.answer("✅ Промокод создан", show_alert=True)
    await _show_promo_view(callback, db, code)
