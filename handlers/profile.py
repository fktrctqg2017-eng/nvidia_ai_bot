"""Раздел «👤 Профиль»: имя, ID, права, статус подписки со сроком, оставшаяся
квота запросов (и когда сброс), список доступных моделей (полными названиями)
и дата регистрации (когда пользователь первый раз написал /start).

Администраторы/владелец могут открыть профиль ЛЮБОГО пользователя из
админ-панели (кнопка «👤 Открыть профиль» в карточке пользователя) — тогда
кнопка "Назад" ведёт обратно к карточке пользователя в админке, а не в
главное меню.
"""

from __future__ import annotations

import time

from aiogram import F, Router
from aiogram.types import CallbackQuery

import roles
import subscriptions as sub_logic
from database import Database
from keyboards import profile_menu
from text_utils import escape_html, safe_edit_text

router = Router(name="profile")


def _format_registered_at(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


async def _build_profile_text(db: Database, config, telegram_id: int, display_name: str) -> str:
    role = await roles.get_user_role(db, config, telegram_id)
    profile = await db.ensure_user_profile(telegram_id)

    kind, expires_at = await sub_logic.get_subscription_kind_and_expiry(db, telegram_id, role.is_admin)
    plan = await sub_logic.get_user_plan(db, telegram_id, is_admin=role.is_admin)

    if kind == "admin":
        sub_line = "Подписка: есть (admin, служебная)"
        term_line = "Срок подписки: бессрочно (действует, пока вы администратор/владелец)"
    elif kind == "none":
        sub_line = "Подписка: нету"
        term_line = "Срок подписки: —"
    else:
        _, name = kind.split(":", 1)
        sub_line = f"Подписка: есть ({escape_html(name)})"
        term_line = f"Срок подписки: {sub_logic.format_time_left(expires_at)}"

    if profile.is_frozen:
        sub_line += " ⏸ [ЗАМОРОЖЕНА]"
    if profile.is_banned:
        sub_line += " 🚫 [ЗАБЛОКИРОВАН]"

    # Квота
    quota = await sub_logic.get_quota_info(db, telegram_id, plan)
    if not quota.has_limits:
        quota_line = "Квота: без ограничений"
    else:
        reset_str = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(quota.reset_at)) if quota.reset_at else "?"
        quota_line = (
            f"Квота: осталось {quota.remaining} из {quota.limit} запросов "
            f"(окно: {quota.window_label}), сброс в {reset_str}"
        )

    # Доступные модели — полные названия
    allowed_ids = await sub_logic.get_effective_allowed_key_ids(db, telegram_id, is_admin=role.is_admin)
    if allowed_ids:
        all_keys = await db.list_api_keys()
        key_map = {k.id: k for k in all_keys}
        names = [key_map[kid].model or key_map[kid].name for kid in allowed_ids if kid in key_map]
        models_line = "Доступные модели:\n" + "\n".join(f"  • <code>{escape_html(n)}</code>" for n in sorted(names))
    else:
        models_line = "Доступные модели: нет"

    return (
        f"👤 <b>Профиль</b>\n\n"
        f"Имя пользователя: {escape_html(display_name)}\n"
        f"ID пользователя: <code>{telegram_id}</code>\n"
        f"Права пользователя: <b>{role.label}</b>\n"
        f"{sub_line}\n"
        f"{term_line}\n"
        f"{quota_line}\n"
        f"{models_line}\n"
        f"Дата регистрации: {_format_registered_at(profile.registered_at)}"
    )


@router.callback_query(F.data == "profile:menu")
async def cb_profile_menu(callback: CallbackQuery, db: Database, config) -> None:
    user = callback.from_user
    display_name = user.full_name or (user.username or str(user.id))
    text = await _build_profile_text(db, config, user.id, display_name)
    await safe_edit_text(callback.message, text, reply_markup=profile_menu())
    await callback.answer()


@router.callback_query(F.data.startswith("profile:view_other:"))
async def cb_profile_view_other(callback: CallbackQuery, db: Database, config, is_admin: bool) -> None:
    if not is_admin:
        await callback.answer("Только для администратора.", show_alert=True)
        return
    target_id = int(callback.data.split(":")[-1])
    try:
        chat = await callback.bot.get_chat(target_id)
        display_name = chat.full_name or (chat.username or str(target_id))
    except Exception:  # noqa: BLE001
        display_name = str(target_id)
    text = await _build_profile_text(db, config, target_id, display_name)
    await safe_edit_text(callback.message, text, reply_markup=profile_menu(is_admin=True, viewing_other=target_id))
    await callback.answer()
