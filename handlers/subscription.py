"""Раздел "💳 Подписки" — витрина тарифов, доступная любому пользователю
(даже без активной подписки), и оформление заявки на покупку ("Хочу купить").

Реальная оплата НЕ автоматизирована (по требованию): пользователь нажимает
"Хочу купить", бот создаёт заявку в БД и уведомляет всех администраторов —
дальше оплата и выдача подписки происходят вручную через админ-панель
(«👑 Админ-панель → 👤 Пользователи и подписки» или прямо из уведомления).
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from config import config
from database import Database
from keyboards import subscription_plan_view_menu, subscription_showcase_menu
from text_utils import escape_html, safe_edit_text
import subscriptions as sub_logic

router = Router(name="subscription")


async def _key_names_map(db: Database) -> dict[int, str]:
    keys = await db.list_api_keys()
    return {k.id: (k.model or k.name) for k in keys}


@router.callback_query(F.data == "subscription:menu")
async def cb_subscription_menu(callback: CallbackQuery, db: Database, is_admin: bool) -> None:
    plans = await db.list_plans(only_active=not is_admin)
    payment_contact = await db.get_bot_setting("payment_contact")

    kind, expires_at = await sub_logic.get_subscription_kind_and_expiry(db, callback.from_user.id, is_admin)
    text_parts = ["💳 <b>Подписки</b>\n"]
    if kind == "admin":
        text_parts.append("Ваш текущий доступ: <b>служебный (admin)</b>, бессрочно\n")
    elif kind != "none":
        sub_type, name = kind.split(":", 1)
        label = "тариф" if sub_type == "plan" else "кастомная подписка"
        text_parts.append(
            f"Ваш текущий {label}: <b>{escape_html(name)}</b> "
            f"(осталось: {sub_logic.format_time_left(expires_at)})\n"
        )
    if not plans:
        text_parts.append("Пока нет доступных тарифов.")
    else:
        text_parts.append("Выберите тариф, чтобы посмотреть подробности:")
    if payment_contact:
        text_parts.append(f"\nПокупка осуществляется через: {escape_html(payment_contact)}")

    await safe_edit_text(callback.message, "\n".join(text_parts), reply_markup=subscription_showcase_menu(plans, is_admin)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("subscription:view:"))
async def cb_subscription_view(callback: CallbackQuery, db: Database) -> None:
    plan_id = int(callback.data.split(":")[-1])
    plan = await db.get_plan(plan_id)
    if not plan:
        await callback.answer("Тариф не найден.", show_alert=True)
        return
    key_names = await _key_names_map(db)
    text = sub_logic.plan_summary_text(plan, key_names)
    await safe_edit_text(callback.message, text, reply_markup=subscription_plan_view_menu(plan.id))
    await callback.answer()


@router.callback_query(F.data.startswith("subscription:buy:"))
async def cb_subscription_buy(callback: CallbackQuery, db: Database) -> None:
    plan_id = int(callback.data.split(":")[-1])
    plan = await db.get_plan(plan_id)
    if not plan:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    user = callback.from_user
    await db.create_purchase_request(user.id, plan_id)

    payment_contact = await db.get_bot_setting("payment_contact")
    contact_line = (
        f"\n\nДля оплаты напишите: {escape_html(payment_contact)}"
        if payment_contact
        else "\n\nАдминистратор свяжется с вами для оплаты."
    )
    await callback.message.answer(
        f"✅ Заявка на тариф «{escape_html(plan.name)}» отправлена администратору.{contact_line}"
    )
    await callback.answer("Заявка отправлена!", show_alert=True)

    # Уведомляем всех администраторов о новой заявке
    full_name = escape_html(user.full_name or "")
    username_part = f" (@{user.username})" if user.username else ""
    notify_text = (
        "📨 <b>Новая заявка на подписку</b>\n\n"
        f"Пользователь: {full_name}{username_part}\n"
        f"ID: <code>{user.id}</code>\n"
        f"Тариф: <b>{escape_html(plan.name)}</b> ({escape_html(plan.price_per_month)}/мес.)\n\n"
        f"Обработать: «👑 Админ-панель → 📨 Заявки на покупку»"
    )
    dynamic_admin_ids = await db.list_bot_admins()
    notify_ids = set(config.owner_ids) | set(config.admin_ids) | set(dynamic_admin_ids)
    for admin_id in notify_ids:
        try:
            await callback.bot.send_message(admin_id, notify_text)
        except Exception:  # noqa: BLE001
            pass  # админ мог не запускать бота / заблокировать его — не критично
