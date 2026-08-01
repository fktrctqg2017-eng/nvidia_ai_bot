from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import subscriptions as sub_logic
from database import Database
from keyboards import main_menu
from text_utils import escape_html, safe_edit_text

router = Router(name="common")


WELCOME_SUBSCRIBER = (
    "🤖 <b>NVIDIA AI Bot</b>\n\n"
    "Я — приватный AI-ассистент, работающий через <b>NVIDIA Cloud API</b> "
    "и <b>NVIDIA NIM</b>.\n\n"
    "Просто напишите мне сообщение, чтобы начать диалог с моделью.\n"
    "Используйте меню ниже для настройки поведения бота."
)


def no_access_text(full_name: str, telegram_id: int) -> str:
    """Заготовленный ответ для пользователя без активной подписки — по
    формату, заданному в требованиях: имя, ID, и явное указание перейти
    в раздел подписок."""
    return (
        f"Здравствуйте {escape_html(full_name)} {telegram_id}, у вас нету доступа "
        f"к диалогу с моделью, для получения доступа перейдите в раздел подписки "
        f"и приобретите подписку."
    )


@router.message(Command("start"))
async def cmd_start(
    message: Message, db: Database, is_admin: bool, is_owner: bool, is_subscriber: bool, state: FSMContext
) -> None:
    await state.clear()
    user = message.from_user
    # Регистрируем дату первого /start (если профиля ещё нет) — используется
    # в «👤 Профиль → Дата регистрации» и как основа для списка рассылки.
    await db.ensure_user_profile(user.id)

    if is_admin or is_subscriber:
        await message.answer(WELCOME_SUBSCRIBER, reply_markup=main_menu(is_admin, is_subscriber, is_owner))
    else:
        full_name = user.full_name or (user.username or "")
        await message.answer(
            no_access_text(full_name, user.id),
            reply_markup=main_menu(is_admin, is_subscriber, is_owner),
        )


@router.message(Command("menu"))
async def cmd_menu(message: Message, is_admin: bool, is_owner: bool, is_subscriber: bool, state: FSMContext) -> None:
    await state.clear()
    await message.answer("📋 Главное меню", reply_markup=main_menu(is_admin, is_subscriber, is_owner))


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, is_admin: bool, is_owner: bool, is_subscriber: bool) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu(is_admin, is_subscriber, is_owner))


@router.callback_query(F.data == "menu:main")
async def cb_main_menu(
    callback: CallbackQuery, is_admin: bool, is_owner: bool, is_subscriber: bool, state: FSMContext
) -> None:
    await state.clear()
    await safe_edit_text(callback.message, "📋 Главное меню", reply_markup=main_menu(is_admin, is_subscriber, is_owner))
    await callback.answer()
