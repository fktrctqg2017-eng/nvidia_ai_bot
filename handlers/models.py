"""Раздел "🧠 Мои модели" — для обычных подписчиков И для администраторов
уровня 1 (НЕ владельца). Показывает модели, реально доступные пользователю:
- обычному подписчику — тариф + персональные переопределения (см.
  subscriptions.get_effective_allowed_key_ids);
- администратору уровня 1 — ВСЕ активные модели (виртуальный admin-план,
  is_admin=True), т.к. полное управление ключами (добавление/удаление/
  включение/выключение) доступно ТОЛЬКО владельцу через «🔑 API-ключи»
  (см. handlers/keys.py).
Под полным названием модели (взятым из API-ключа), а не абстрактным именем
ключа. Здесь нельзя добавлять/удалять ключи — только выбрать, какой из
разрешённых использовать для диалога.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

import subscriptions as sub_logic
from database import KEY_MODEL_MODE_ALL, Database
from keyboards import live_model_choice_menu, my_models_menu
from providers import list_models
from text_utils import escape_html, safe_edit_text

router = Router(name="models")


@router.callback_query(F.data == "models:menu")
async def cb_models_menu(callback: CallbackQuery, db: Database, is_admin: bool, is_owner: bool) -> None:
    user_id = callback.from_user.id
    if is_owner:
        await callback.answer("Владелец использует «🔑 API-ключи» для полного управления моделями.", show_alert=True)
        return

    allowed_ids = await sub_logic.get_effective_allowed_key_ids(db, user_id, is_admin=is_admin)
    if not allowed_ids:
        await safe_edit_text(callback.message, "🧠 <b>Мои модели</b>\n\n"
            "У вас пока нет доступных моделей. Оформите подписку в «💳 Подписки», "
            "чтобы получить доступ.",
            reply_markup=my_models_menu([], None),
        )
        await callback.answer()
        return

    all_keys = await db.list_api_keys(only_active=True)
    keys = [k for k in all_keys if k.id in allowed_ids]

    settings = await db.get_user_settings(user_id)
    text = "🧠 <b>Мои модели</b>\n\nВыберите модель для диалога:"
    await safe_edit_text(callback.message, text, reply_markup=my_models_menu(keys, settings.active_key_id))
    await callback.answer()


@router.callback_query(F.data.startswith("models:select:"))
async def cb_models_select(callback: CallbackQuery, db: Database, is_admin: bool, is_owner: bool) -> None:
    user_id = callback.from_user.id
    key_id = int(callback.data.split(":")[-1])

    key = await db.get_api_key(key_id)
    if not key or not key.is_active:
        await callback.answer("Модель недоступна (возможно, отключена администратором).", show_alert=True)
        return

    allowed_ids = await sub_logic.get_effective_allowed_key_ids(db, user_id, is_admin=is_admin)
    if key_id not in allowed_ids:
        await callback.answer("Эта модель не входит в ваш тариф.", show_alert=True)
        return

    await db.update_user_settings(user_id, active_key_id=key_id, model_override=None)
    # callback.answer(show_alert=True) - это всплывающее окно Telegram, HTML там не
    # парсится, поэтому текст передаём БЕЗ escape_html (иначе будут видны &amp; и т.п.)
    await callback.answer(f"✅ Теперь используется: {key.model or key.name}", show_alert=True)
    await cb_models_menu(callback, db, is_admin, is_owner)


@router.callback_query(F.data.startswith("mymodel:live_menu:"))
async def cb_mymodel_live_menu(callback: CallbackQuery, db: Database, is_admin: bool, is_owner: bool, state: FSMContext) -> None:
    """Для ключей в режиме 'all_models' — показывает пользователю ЖИВОЙ
    список моделей, реально подключённых к ключу у провайдера, и позволяет
    выбрать любую из них лично для себя (не трогая настройки самого ключа)."""
    user_id = callback.from_user.id
    key_id = int(callback.data.split(":")[-1])

    key = await db.get_api_key(key_id)
    if not key or not key.is_active:
        await callback.answer("Модель недоступна (возможно, отключена администратором).", show_alert=True)
        return

    allowed_ids = await sub_logic.get_effective_allowed_key_ids(db, user_id, is_admin=is_admin)
    if key_id not in allowed_ids:
        await callback.answer("Эта модель не входит в ваш тариф.", show_alert=True)
        return

    await callback.answer("Запрашиваю список моделей у провайдера…")
    try:
        from config import config as app_config

        models = await list_models(key, app_config.request_timeout)
    except Exception as e:  # noqa: BLE001
        await callback.message.answer(f"⚠️ Не удалось получить список моделей: {e}")
        return
    if not models:
        await callback.message.answer("Провайдер не вернул список моделей.")
        return

    # Если админ/владелец ограничил доступ конкретными моделями ЭТОГО ключа
    # именно в тарифе/подписке пользователя — показываем только их, а не
    # весь живой список провайдера (иначе пользователь мог бы выбрать модель,
    # формально не входящую в его тариф).
    restricted = await sub_logic.get_restricted_models_for_key(db, user_id, key_id, is_admin=is_admin)
    if restricted:
        models = [m for m in models if m in restricted]
        if not models:
            await callback.message.answer(
                "⚠️ Ваш тариф ограничивает доступ к моделям этого ключа, но ни одна из "
                "разрешённых моделей сейчас не подключена у провайдера. Обратитесь к администратору."
            )
            return

    models = models[:100]
    await state.update_data(**{f"live_models_user_{key_id}": models})

    settings = await db.get_user_settings(user_id)
    current_model = settings.model_override if settings.active_key_id == key_id else None
    await safe_edit_text(
        callback.message,
        f"🧪 Модели ключа «{escape_html(key.name)}» — выберите модель для своего диалога:",
        reply_markup=live_model_choice_menu(
            key_id, models, current_model, back_cb="models:menu", pick_prefix="mymodel:livepick"
        ),
    )


@router.callback_query(F.data.startswith("mymodel:livepick:"))
async def cb_mymodel_livepick(callback: CallbackQuery, db: Database, is_admin: bool, is_owner: bool, state: FSMContext) -> None:
    """Пользователь выбрал конкретную модель из живого списка ключа с режимом
    'all_models' — сохраняем ЛИЧНОЕ переопределение модели (model_override),
    не трогая саму настройку ключа (иначе один пользователь незаметно менял бы
    модель для всех остальных, использующих тот же ключ)."""
    user_id = callback.from_user.id
    _, _, key_id_str, index_str = callback.data.split(":")
    key_id, index = int(key_id_str), int(index_str)

    key = await db.get_api_key(key_id)
    if not key or not key.is_active:
        await callback.answer("Модель недоступна (возможно, отключена администратором).", show_alert=True)
        return

    allowed_ids = await sub_logic.get_effective_allowed_key_ids(db, user_id, is_admin=is_admin)
    if key_id not in allowed_ids:
        await callback.answer("Эта модель не входит в ваш тариф.", show_alert=True)
        return

    data = await state.get_data()
    cached_models = data.get(f"live_models_user_{key_id}")
    if cached_models and 0 <= index < len(cached_models):
        chosen_model = cached_models[index]
    else:
        try:
            from config import config as app_config

            models = await list_models(key, app_config.request_timeout)
        except Exception as e:  # noqa: BLE001
            await callback.answer(f"Не удалось получить список моделей: {e}", show_alert=True)
            return
        if not (0 <= index < len(models)):
            await callback.answer("Модель не найдена в актуальном списке, попробуйте открыть список заново.", show_alert=True)
            return
        chosen_model = models[index]

    # Финальная проверка на случай, если точечное ограничение изменилось
    # (или было применено) уже ПОСЛЕ того, как список моделей был показан
    # пользователю — не полагаемся только на фильтрацию при отображении.
    restricted = await sub_logic.get_restricted_models_for_key(db, user_id, key_id, is_admin=is_admin)
    if restricted and chosen_model not in restricted:
        await callback.answer("Эта модель больше не входит в разрешённый список вашего тарифа.", show_alert=True)
        return

    await db.update_user_settings(user_id, active_key_id=key_id, model_override=chosen_model)
    await callback.answer(f"✅ Теперь используется: {chosen_model}", show_alert=True)
    await cb_models_menu(callback, db, is_admin, is_owner)
