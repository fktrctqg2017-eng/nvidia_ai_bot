"""Обработка файлов, присланных пользователем в чат: архивы (с изображениями
и/или текстовыми/кодовыми файлами) И одиночные документы любого типа —
PDF, DOCX, XLSX и произвольные текстовые/кодовые файлы, отправленные
напрямую (не в архиве).

Поддерживаемые форматы архивов: .zip, .tar, .tar.gz/.tgz, .tar.bz2/.tbz2, .tar.xz/.txz, .gz.
Поддерживаемые одиночные документы: .pdf, .docx, .xlsx/.xlsm (специальный
разбор через pypdf/python-docx/openpyxl) + ЛЮБОЙ файл, похожий на текст
(файлы с известными кодовыми/текстовыми расширениями читаются напрямую,
а файлы с неизвестным расширением — эвристически, если удаётся прочитать
их как UTF-8 текст; иначе явно поддерживаемые бинарные форматы вроде .exe/
.mp3/.zip и т.п. отклоняются с понятным сообщением).

Логика для архивов:
1. Пользователь присылает документ (архив одного из поддерживаемых форматов).
2. Бот скачивает его во временный файл, безопасно распаковывает
   (см. file_handler.py — защита от path traversal и архивных бомб).
3. Классифицирует содержимое: картинки -> base64 для Vision API,
   текст/код/документы (PDF/DOCX/XLSX) -> текст в контекст.
4. Сохраняет распакованное содержимое в оперативной памяти (llm_agent.archive_store),
   чтобы модель могла позже обратиться к конкретным файлам через инструменты
   list_archive_files / read_archive_file (агентский режим), а пользователь —
   просмотреть их напрямую через «📂 Мои файлы» (см. handlers/browser.py).
5. Сразу отправляет мультимодальный запрос модели (текст из подписи к файлу +
   картинки + содержимое текстовых файлов), чтобы пользователь получил анализ
   архива сразу же, без необходимости писать дополнительное сообщение.

Логика для ОДИНОЧНОГО документа — та же самая (шаги 3-5), только вместо
распаковки архива читается один файл целиком.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.types import Message

import subscriptions as sub_logic
from config import config
from database import Database
from file_handler import (
    MAX_DOCUMENT_SIZE_BYTES,
    ZipSecurityError,
    build_multimodal_user_content,
    classify_and_read,
    extract_archive_safely,
    is_supported_archive,
    read_single_document,
)
from handlers.chat import _build_confirm_callback
from keyboards import archive_files_menu
from llm_agent import AGENT_SYSTEM_PROMPT, archive_store, run_agent_turn
from providers import reasoning_system_prompt_hint, simple_chat_completion
from text_utils import escape_html, send_long_text

router = Router(name="files")

# Telegram Bot API не позволяет обычным ботам скачивать файлы крупнее 20 MB.
MAX_ARCHIVE_SIZE_BYTES = 20 * 1024 * 1024

ARCHIVE_MIME_TYPES = {
    "application/zip",
    "application/x-zip-compressed",
    "application/x-zip",
    "application/x-tar",
    "application/gzip",
    "application/x-gzip",
    "application/x-bzip2",
    "application/x-xz",
    "application/x-compressed-tar",
}


def _looks_like_archive(message: Message) -> bool:
    doc = message.document
    if doc is None:
        return False
    if doc.mime_type in ARCHIVE_MIME_TYPES:
        return True
    return bool(doc.file_name and is_supported_archive(doc.file_name))


@router.message(F.document, _looks_like_archive)
async def handle_archive_document(message: Message, db: Database, is_admin: bool, is_owner: bool, is_subscriber: bool) -> None:
    user_id = message.from_user.id
    doc = message.document

    if not is_admin and not is_subscriber:
        from handlers.common import no_access_text

        user = message.from_user
        await message.answer(no_access_text(user.full_name or (user.username or ""), user.id))
        return

    if doc.file_size and doc.file_size > MAX_ARCHIVE_SIZE_BYTES:
        await message.answer(
            f"⚠️ Файл слишком большой ({doc.file_size / 1024 / 1024:.1f} MB). "
            f"Максимум для Telegram-бота: {MAX_ARCHIVE_SIZE_BYTES / 1024 / 1024:.0f} MB."
        )
        return

    settings = await db.get_user_settings(user_id)
    hint = "«🔑 API-ключи»" if is_owner else "«🧠 Мои модели»"
    if not settings.active_key_id:
        await message.answer(f"⚠️ У вас не выбран активный API-ключ. Откройте меню {hint} и выберите ключ.")
        return
    key = await db.get_api_key(settings.active_key_id)
    if not key or not key.is_active:
        await message.answer(f"⚠️ Выбранный API-ключ недоступен. Выберите другой в меню {hint}.")
        return

    model = settings.model_override or key.model
    if not model:
        await message.answer("⚠️ Для этого ключа не задана модель. Задайте её в «⚙️ Настройки» → «Модель».")
        return

    access = await sub_logic.check_chat_access(db, user_id, key, is_admin, model=model)
    if not access.allowed:
        await message.answer(f"⛔ {access.reason}")
        return

    status_msg = await message.answer("📥 Скачиваю и распаковываю архив…")
    await message.bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_DOCUMENT)

    try:
        with tempfile.TemporaryDirectory(prefix="archive_download_") as tmp_dir:
            archive_name = doc.file_name or "archive"
            archive_path = Path(tmp_dir) / archive_name
            await message.bot.download(doc, destination=archive_path)

            try:
                with extract_archive_safely(archive_path, original_filename=archive_name) as extracted_dir:
                    extraction = classify_and_read(extracted_dir)
            except ZipSecurityError as e:
                await status_msg.edit_text(f"⛔ Архив отклонён по соображениям безопасности:\n{escape_html(str(e))}")
                return

        if extraction.is_empty:
            await status_msg.edit_text(
                "⚠️ В архиве не найдено ни изображений, ни поддерживаемых текстовых/кодовых файлов.\n\n"
                + escape_html(extraction.summary())
            )
            return

        # Сохраняем в памяти для последующего использования: агентом (инструменты
        # list/read) и пользователем напрямую через «📂 Мои файлы» (handlers/browser.py).
        archive = archive_store.add(
            user_id, doc.file_name or "archive", extraction.text_files, extraction.images
        )

        await status_msg.edit_text(
            f"✅ {escape_html(extraction.summary())}\n\n"
            f"🆔 ID архива: <code>{archive.archive_id}</code> "
            f"(можно открыть в «📂 Мои файлы» в любой момент)\n\n"
            f"⏳ Отправляю содержимое модели на анализ…"
        )
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

        caption = (message.caption or "").strip() or (
            "Проанализируй содержимое этого архива: опиши, что изображено на картинках "
            "(если есть), и разбери код/текстовые файлы (если есть)."
        )
        user_content = build_multimodal_user_content(caption, extraction)

        temperature = settings.temperature if settings.temperature is not None else config.default_temperature
        top_p = settings.top_p if settings.top_p is not None else config.default_top_p
        max_tokens = settings.max_tokens if settings.max_tokens is not None else config.default_max_tokens
        reasoning_effort = settings.reasoning_effort or config.default_reasoning_effort

        base_messages = []
        system_parts = []
        if settings.agent_mode:
            system_parts.append(AGENT_SYSTEM_PROMPT)
        reasoning_hint = reasoning_system_prompt_hint(reasoning_effort)
        if reasoning_hint:
            system_parts.append(reasoning_hint)
        if settings.system_prompt:
            system_parts.append(settings.system_prompt)
        hint = archive_store.context_hint(user_id)
        if hint:
            system_parts.append(hint)
        if system_parts:
            base_messages.append({"role": "system", "content": "\n\n".join(system_parts)})

        base_messages.append({"role": "user", "content": user_content})

        if settings.agent_mode:
            confirm_callback = await _build_confirm_callback(message, db, user_id)
            result = await run_agent_turn(
                key=key,
                model=model,
                messages=base_messages,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                timeout=config.request_timeout,
                telegram_id=user_id,
                on_step=lambda text: status_msg.edit_text(text),
                confirm_code_callback=confirm_callback,
                reasoning_effort=reasoning_effort,
            )
            answer = result.final_text
        else:
            try:
                answer = await simple_chat_completion(
                    key, model, base_messages, temperature, top_p, max_tokens, config.request_timeout,
                    reasoning_effort=reasoning_effort,
                )
            except Exception as e:  # noqa: BLE001
                answer = f"⚠️ Ошибка запроса к модели: {e}"

        # В историю диалога добавляем компактную текстовую версию (без base64-картинок,
        # чтобы не раздувать контекст последующих сообщений).
        history_note = f"[Пользователь загрузил архив {doc.file_name}] {caption}"
        await db.add_history_message(user_id, "user", history_note)
        await db.add_history_message(user_id, "assistant", answer or "(пустой ответ)")

        await send_long_text(message, escape_html(answer or "(пустой ответ от модели)"))
        try:
            await status_msg.delete()
        except Exception:  # noqa: BLE001
            pass

        # Дополнительно показываем инлайн-кнопки для мгновенного просмотра файлов
        # этого архива, не заходя в общее меню «📂 Мои файлы».
        await message.answer(
            "📂 Файлы этого архива можно посмотреть прямо здесь:",
            reply_markup=archive_files_menu(archive),
        )

    except Exception as e:  # noqa: BLE001
        await status_msg.edit_text(f"⚠️ Не удалось обработать архив: {escape_html(str(e))}")


# =====================================================================
# ОДИНОЧНЫЙ ДОКУМЕНТ (не архив) — PDF/DOCX/XLSX/любой текстовый/кодовый файл
# =====================================================================


@router.message(F.document)
async def handle_single_document(message: Message, db: Database, is_admin: bool, is_owner: bool, is_subscriber: bool) -> None:
    """Обрабатывает документ, присланный НАПРЯМУЮ (не архивом) — например,
    PDF-отчёт, DOCX-договор, XLSX-таблицу или любой текстовый/кодовый файл.
    Архивы перехватываются выше, в handle_archive_document (регистрируется
    раньше в этом же роутере, поэтому для архивов этот хендлер не вызывается —
    aiogram матчит первый подходящий хендлер и останавливается)."""
    user_id = message.from_user.id
    doc = message.document

    # Если это архив — им уже занялся handle_archive_document (зарегистрирован
    # выше в этом файле и матчится первым для aiogram, так что эта проверка —
    # просто явная документация инварианта, а не обязательная защита).
    if _looks_like_archive(message):
        return

    if not is_admin and not is_subscriber:
        from handlers.common import no_access_text

        user = message.from_user
        await message.answer(no_access_text(user.full_name or (user.username or ""), user.id))
        return

    if doc.file_size and doc.file_size > MAX_DOCUMENT_SIZE_BYTES:
        await message.answer(
            f"⚠️ Файл слишком большой ({doc.file_size / 1024 / 1024:.1f} MB). "
            f"Максимум для Telegram-бота: {MAX_DOCUMENT_SIZE_BYTES / 1024 / 1024:.0f} MB."
        )
        return

    settings = await db.get_user_settings(user_id)
    hint = "«🔑 API-ключи»" if is_owner else "«🧠 Мои модели»"
    if not settings.active_key_id:
        await message.answer(f"⚠️ У вас не выбран активный API-ключ. Откройте меню {hint} и выберите ключ.")
        return
    key = await db.get_api_key(settings.active_key_id)
    if not key or not key.is_active:
        await message.answer(f"⚠️ Выбранный API-ключ недоступен. Выберите другой в меню {hint}.")
        return

    model = settings.model_override or key.model
    if not model:
        await message.answer("⚠️ Для этого ключа не задана модель. Задайте её в «⚙️ Настройки» → «Модель».")
        return

    access = await sub_logic.check_chat_access(db, user_id, key, is_admin, model=model)
    if not access.allowed:
        await message.answer(f"⛔ {access.reason}")
        return

    status_msg = await message.answer("📥 Скачиваю и анализирую документ…")
    await message.bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_DOCUMENT)

    file_name = doc.file_name or "document"

    try:
        with tempfile.TemporaryDirectory(prefix="document_download_") as tmp_dir:
            doc_path = Path(tmp_dir) / file_name
            await message.bot.download(doc, destination=doc_path)

            try:
                text_file = read_single_document(doc_path, file_name)
            except ValueError as e:
                await status_msg.edit_text(
                    f"⚠️ Не удалось обработать файл «{escape_html(file_name)}»: {escape_html(str(e))}\n\n"
                    f"Поддерживаются: PDF, DOCX, XLSX, а также обычные текстовые/кодовые файлы. "
                    f"Для картинок — просто отправьте их как фото, для наборов файлов — заархивируйте их."
                )
                return

        # Сохраняем как "виртуальный архив" из одного файла — тот же механизм,
        # что и для архивов: доступен агенту через list/read_archive_file и
        # пользователю через «📂 Мои файлы».
        archive = archive_store.add(user_id, file_name, [text_file], [])

        await status_msg.edit_text(
            f"✅ Документ «{escape_html(file_name)}» обработан "
            f"({len(text_file.full_content)} символов извлечено"
            f"{', обрезан для контекста модели' if text_file.truncated else ''}).\n\n"
            f"🆔 ID: <code>{archive.archive_id}</code> (можно открыть в «📂 Мои файлы»)\n\n"
            f"⏳ Отправляю содержимое модели на анализ…"
        )
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

        caption = (message.caption or "").strip() or (
            f"Проанализируй содержимое этого документа ({file_name}) и вкратце опиши, что в нём."
        )
        user_content = (
            f"{caption}\n\n📄 Содержимое файла «{file_name}»"
            f"{' (обрезано)' if text_file.truncated else ''}:\n{text_file.content}"
        )

        temperature = settings.temperature if settings.temperature is not None else config.default_temperature
        top_p = settings.top_p if settings.top_p is not None else config.default_top_p
        max_tokens = settings.max_tokens if settings.max_tokens is not None else config.default_max_tokens
        reasoning_effort = settings.reasoning_effort or config.default_reasoning_effort

        base_messages = []
        system_parts = []
        if settings.agent_mode:
            system_parts.append(AGENT_SYSTEM_PROMPT)
        reasoning_hint = reasoning_system_prompt_hint(reasoning_effort)
        if reasoning_hint:
            system_parts.append(reasoning_hint)
        if settings.system_prompt:
            system_parts.append(settings.system_prompt)
        hint = archive_store.context_hint(user_id)
        if hint:
            system_parts.append(hint)
        if system_parts:
            base_messages.append({"role": "system", "content": "\n\n".join(system_parts)})

        base_messages.append({"role": "user", "content": user_content})

        if settings.agent_mode:
            confirm_callback = await _build_confirm_callback(message, db, user_id)
            result = await run_agent_turn(
                key=key,
                model=model,
                messages=base_messages,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                timeout=config.request_timeout,
                telegram_id=user_id,
                on_step=lambda text: status_msg.edit_text(text),
                confirm_code_callback=confirm_callback,
                reasoning_effort=reasoning_effort,
            )
            answer = result.final_text
        else:
            try:
                answer = await simple_chat_completion(
                    key, model, base_messages, temperature, top_p, max_tokens, config.request_timeout,
                    reasoning_effort=reasoning_effort,
                )
            except Exception as e:  # noqa: BLE001
                answer = f"⚠️ Ошибка запроса к модели: {e}"

        history_note = f"[Пользователь загрузил документ {file_name}] {caption}"
        await db.add_history_message(user_id, "user", history_note)
        await db.add_history_message(user_id, "assistant", answer or "(пустой ответ)")

        await send_long_text(message, escape_html(answer or "(пустой ответ от модели)"))
        try:
            await status_msg.delete()
        except Exception:  # noqa: BLE001
            pass

        await message.answer(
            "📂 Этот документ можно посмотреть прямо здесь:",
            reply_markup=archive_files_menu(archive),
        )

    except Exception as e:  # noqa: BLE001
        await status_msg.edit_text(f"⚠️ Не удалось обработать документ: {escape_html(str(e))}")
