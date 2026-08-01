"""Файловый браузер: просмотр содержимого ранее загруженных архивов прямо
в интерфейсе бота — список архивов -> список файлов внутри -> просмотр
конкретного файла (текст с пагинацией или изображение как фото).
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, CallbackQuery

from keyboards import (
    archive_files_menu,
    confirm_delete_archive,
    files_menu,
    main_menu,
    text_file_view_menu,
)
from llm_agent import archive_store
from text_utils import escape_html, safe_edit_text

router = Router(name="browser")

# Сколько символов текстового файла показывать на одной "странице" в Telegram
PAGE_SIZE = 3000


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "files:menu")
async def cb_files_menu(callback: CallbackQuery, is_admin: bool, is_subscriber: bool) -> None:
    if not is_admin and not is_subscriber:
        await callback.answer("Файлы доступны только с активной подпиской. Смотрите «💳 Подписки».", show_alert=True)
        return
    archives = archive_store.list_for_user(callback.from_user.id)
    if not archives:
        text = (
            "📂 <b>Мои файлы</b>\n\n"
            "У вас пока нет загруженных архивов. Пришлите боту документ "
            "(.zip, .tar, .tar.gz/.tgz, .tar.bz2/.tbz2, .tar.xz/.txz, .gz), "
            "чтобы он появился здесь."
        )
    else:
        text = f"📂 <b>Мои файлы</b>\n\nЗагруженные архивы ({len(archives)}):"
    await safe_edit_text(callback.message, text, reply_markup=files_menu(archives))
    await callback.answer()


@router.callback_query(F.data.startswith("files:archive:"))
async def cb_view_archive(callback: CallbackQuery) -> None:
    archive_id = callback.data.split(":")[-1]
    archive = archive_store.get(callback.from_user.id, archive_id)
    if not archive:
        await callback.answer("Архив не найден (возможно, устарел после перезапуска бота).", show_alert=True)
        return
    text = (
        f"📦 <b>{escape_html(archive.original_name)}</b>\n"
        f"ID: <code>{archive.archive_id}</code>\n\n"
        f"📄 Текстовых/кодовых файлов: {len(archive.text_files)}\n"
        f"🖼 Изображений: {len(archive.images)}\n\n"
        f"Выберите файл для просмотра:"
    )
    await safe_edit_text(callback.message, text, reply_markup=archive_files_menu(archive))
    await callback.answer()


@router.callback_query(F.data.startswith("files:text:"))
async def cb_view_text_file(callback: CallbackQuery) -> None:
    try:
        _, _, archive_id, index_str, page_str = callback.data.split(":")
        file_index, page = int(index_str), int(page_str)
    except ValueError:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    archive = archive_store.get(callback.from_user.id, archive_id)
    if not archive or file_index >= len(archive.text_files):
        await callback.answer("Файл не найден (возможно, архив устарел после перезапуска бота).", show_alert=True)
        return

    file = archive.text_files[file_index]
    content = file.full_content
    total_pages = max(1, (len(content) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = content[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    truncated_note = ""
    if file.storage_truncated:
        truncated_note = "\n\n⚠️ Файл был обрезан при загрузке (превышал лимит хранения)."

    text = (
        f"📄 <b>{escape_html(file.filename)}</b> (стр. {page + 1}/{total_pages})\n\n"
        f"<pre>{escape_html(chunk) or '(пустой файл)'}</pre>"
        f"{truncated_note}"
    )
    try:
        await safe_edit_text(
            callback.message, text, reply_markup=text_file_view_menu(archive_id, file_index, page, total_pages)
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data.startswith("files:image:"))
async def cb_view_image(callback: CallbackQuery) -> None:
    try:
        _, _, archive_id, index_str = callback.data.split(":")
        image_index = int(index_str)
    except ValueError:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    archive = archive_store.get(callback.from_user.id, archive_id)
    if not archive or image_index >= len(archive.images):
        await callback.answer("Изображение не найдено (возможно, архив устарел после перезапуска бота).", show_alert=True)
        return

    image = archive.images[image_index]
    await callback.answer("📤 Отправляю изображение…")
    photo = BufferedInputFile(image.raw_bytes, filename=image.filename or "image.png")
    await callback.message.answer_photo(photo, caption=f"🖼 {image.filename}")


@router.callback_query(F.data.startswith("files:delete_ask:"))
async def cb_delete_archive_ask(callback: CallbackQuery) -> None:
    archive_id = callback.data.split(":")[-1]
    await safe_edit_text(callback.message, "⚠️ Удалить этот архив из памяти бота? Это не отменяемое действие "
        "(но вы всегда можете загрузить архив заново).",
        reply_markup=confirm_delete_archive(archive_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("files:delete_confirm:"))
async def cb_delete_archive_confirm(callback: CallbackQuery, is_admin: bool) -> None:
    archive_id = callback.data.split(":")[-1]
    removed = archive_store.remove(callback.from_user.id, archive_id)
    await callback.answer("🗑 Архив удалён" if removed else "Архив уже был удалён", show_alert=True)
    archives = archive_store.list_for_user(callback.from_user.id)
    text = "📂 <b>Мои файлы</b>\n\n" + (
        f"Загруженные архивы ({len(archives)}):" if archives else "У вас пока нет загруженных архивов."
    )
    await safe_edit_text(callback.message, text, reply_markup=files_menu(archives))
