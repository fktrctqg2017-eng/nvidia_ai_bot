"""Ядро AI-агента: ReAct-цикл (Reason + Act) поверх NVIDIA Cloud API / NVIDIA NIM.

Почему промпт-инжиниринг, а не "нативный" function calling API:
    Бот целенаправленно поддерживает ЛЮБУЮ модель, доступную через NVIDIA
    Cloud API / NIM — от маленьких Llama/Mistral до крупных reasoning-моделей.
    Качество и сам факт поддержки нативного OpenAI-style `tools=[...]`
    сильно различается между моделями и версиями NIM-контейнеров, поэтому
    единый способ, который гарантированно работает везде — явно попросить
    модель в системном промпте возвращать простые, легко парсящиеся
    структуры (fenced-блоки кода/JSON) и разбирать их на стороне бота.
    Это ровно тот вариант, который в задании обозначен как основной путь
    ("если API не поддерживает нативный Function Calling — через
    промпт-инжиниринг"), и он одновременно самый переносимый.

Протокол взаимодействия с моделью (описывается ей в system prompt):
    1. Если для ответа модели нужно выполнить Python-код — она возвращает
       код в обычном markdown-блоке ```python ... ```. Ничего оборачивать
       в JSON для этого не нужно — это самый надёжный способ получить от
       модели валидный код, т.к. так её обучали генерировать код изначально.
    2. Если модели нужно обратиться к одному из "инструментов" бота
       (список файлов в ранее загруженном архиве, чтение конкретного файла
       из архива и т.п.) — она возвращает ОДИН JSON-объект вида
       {"action": "<имя_действия>", ...параметры...} в блоке ```json ... ```
       и больше ничего в ответе.
    3. Если у модели уже есть весь ответ для пользователя — она просто
       отвечает обычным текстом (без кода и без JSON-действия). Это и есть
       финальный ответ, цикл агента останавливается.

Агент выполняет действие, добавляет результат обратно в историю сообщений
как системную "наблюдение" (Observation) и снова спрашивает модель — до
тех пор, пока не получит обычный текстовый (финальный) ответ, либо пока
не будет исчерпан лимит шагов (защита от зацикливания).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import sandbox
import tools as tools_module
from config import config
from database import ApiKey
from file_handler import ExtractedImage, ExtractedTextFile
from providers import reasoning_system_prompt_hint, simple_chat_completion

# ------------------------------------------------------------------ системный промпт агента

def _build_agent_system_prompt() -> str:
    """Собирает системный промпт агента динамически из реестра tools.TOOL_DESCRIPTIONS,
    чтобы добавление нового инструмента в tools.py автоматически появлялось в
    промпте без необходимости редактировать этот файл."""
    tool_lines = "\n\n".join(
        f"   ```json\n   {desc}\n   ```" for desc in tools_module.TOOL_DESCRIPTIONS.values()
    )
    return f"""\
Ты — полезный AI-агент, работающий в Telegram-боте. У тебя есть доступ к
инструментам, которыми можно пользоваться для решения задач пользователя.

ДОСТУПНЫЕ ИНСТРУМЕНТЫ:

1. Выполнение Python-кода в изолированной песочнице.
   Чтобы выполнить код — верни ЕГО И ТОЛЬКО ЕГО в обычном markdown-блоке:
   ```python
   # твой код здесь
   ```
   Не добавляй в этот же ответ никакого другого текста, кроме этого блока
   кода — иначе автоматический парсер не сможет однозначно разобрать ответ.
   После выполнения ты получишь stdout/stderr кода и сможешь либо
   выполнить ещё код, либо дать финальный ответ пользователю на основе
   результата.
   Ограничения песочницы: нет доступа к сети/интернету, таймаут выполнения
   ~15 секунд, лимит памяти ~256 MB. Не пиши код, который ожидает доступ к
   интернету, GUI, или бесконечно долгую работу. Для доступа к интернету
   используй инструмент web_search/fetch_url ниже, а не код.
   Важно: пользователь может увидеть твой код перед выполнением и отклонить
   его (если у него включено подтверждение кода) — в этом случае ты получишь
   сообщение об отказе вместо результата выполнения. Учитывай это и, если
   код отклонили, предложи альтернативный подход или объяснение без кода.

2. Работа с ранее загруженными пользователем архивами и файлами (.zip, .tar,
   .tar.gz, .tar.bz2, .tar.xz, .gz, а также одиночные документы — .pdf, .docx,
   .xlsx, .txt, код и практически любой другой текстовый файл — если они есть,
   их список с ID и списком файлов будет показан в контексте диалога).
   Чтобы получить список файлов архива — верни ЕДИНСТВЕННЫЙ JSON-объект в
   блоке:
   ```json
   {{"action": "list_archive_files", "archive_id": "<id архива>"}}
   ```
   Чтобы прочитать содержимое конкретного файла из архива — верни:
   ```json
   {{"action": "read_archive_file", "archive_id": "<id архива>", "filename": "<имя файла>"}}
   ```

3. Дополнительные инструменты общего назначения — верни ЕДИНСТВЕННЫЙ JSON-объект
   в блоке ```json ... ``` в одном из следующих форматов:

{tool_lines}

ВАЖНЫЕ ПРАВИЛА:
- Если тебе нужно выполнить код или вызвать инструмент — твой ответ должен
  СОСТОЯТЬ ТОЛЬКО ИЗ ОДНОГО такого блока (```python или ```json), без
  дополнительных пояснений до или после блока.
- Если у тебя уже есть достаточно информации, чтобы ответить пользователю —
  просто напиши обычный текстовый ответ на русском языке (или на языке
  пользователя), без блоков кода и без JSON. Это будет считаться финальным
  ответом, и он будет показан пользователю.
- Не выдумывай результаты выполнения кода или вызова инструментов — всегда
  дожидайся реального результата от системы и основывай финальный ответ на нём.
- Для вопросов о текущих событиях, ценах, погоде, актуальных фактах — используй
  web_search, а не полагайся на свои внутренние знания (они могут быть устаревшими).
- Изображения, которые прислал пользователь (напрямую, голосом через транскрипцию,
  или внутри архива), уже доступны тебе непосредственно в сообщении пользователя —
  если модель поддерживает работу с изображениями, ты можешь их проанализировать
  без вызова каких-либо инструментов.
"""


AGENT_SYSTEM_PROMPT = _build_agent_system_prompt()

MAX_AGENT_STEPS_DEFAULT = config.agent_max_steps


class CodeExecutionDenied(Exception):
    """Пользователь отклонил выполнение сгенерированного кода (см. подтверждение в chat.py/files.py)."""


# ------------------------------------------------------------------ хранилище архивов в памяти


@dataclass
class StoredArchive:
    archive_id: str
    original_name: str
    text_files: list[ExtractedTextFile]   # полное содержимое (с учётом MAX_TEXT_FILE_CHARS_STORAGE)
    images: list[ExtractedImage]
    created_at: float = field(default_factory=time.time)

    def text_file_by_name(self, filename: str) -> Optional[ExtractedTextFile]:
        return next((f for f in self.text_files if f.filename == filename), None)

    def listing(self) -> str:
        lines = [f"Архив «{self.original_name}» (id={self.archive_id}):"]
        for f in self.text_files:
            lines.append(f"  📄 {f.filename}")
        for img in self.images:
            lines.append(f"  🖼 {img.filename} (изображение — уже показано модели, если это vision-модель)")
        return "\n".join(lines)


class ArchiveStore:
    """Простое in-memory хранилище распакованных архивов на пользователя.

    Хранится только в оперативной памяти процесса бота (не в SQLite),
    поэтому при перезапуске бота список ранее загруженных архивов
    очищается — это осознанный компромисс простоты, так как хранить
    потенциально мегабайты кода/картинок в БД ради пере-открытия чата после
    рестарта обычно избыточно. Пользователь всегда может прислать архив
    заново.
    """

    MAX_ARCHIVES_PER_USER = 5

    def __init__(self) -> None:
        self._data: dict[int, dict[str, StoredArchive]] = {}
        self._counters: dict[int, int] = {}

    def add(
        self,
        telegram_id: int,
        original_name: str,
        text_files: list[ExtractedTextFile],
        images: list[ExtractedImage],
    ) -> StoredArchive:
        user_archives = self._data.setdefault(telegram_id, {})
        counter = self._counters.get(telegram_id, 0) + 1
        self._counters[telegram_id] = counter
        archive_id = f"a{counter}"
        archive = StoredArchive(
            archive_id=archive_id,
            original_name=original_name,
            text_files=text_files,
            images=images,
        )
        user_archives[archive_id] = archive
        # ограничиваем число хранимых архивов на пользователя (FIFO-вытеснение)
        if len(user_archives) > self.MAX_ARCHIVES_PER_USER:
            oldest_id = min(user_archives, key=lambda k: user_archives[k].created_at)
            if oldest_id != archive_id:
                del user_archives[oldest_id]
        return archive

    def get(self, telegram_id: int, archive_id: str) -> Optional[StoredArchive]:
        return self._data.get(telegram_id, {}).get(archive_id)

    def list_for_user(self, telegram_id: int) -> list[StoredArchive]:
        return list(self._data.get(telegram_id, {}).values())

    def remove(self, telegram_id: int, archive_id: str) -> bool:
        user_archives = self._data.get(telegram_id, {})
        if archive_id in user_archives:
            del user_archives[archive_id]
            return True
        return False

    def context_hint(self, telegram_id: int) -> str:
        """Короткая подсказка для system/контекста о том, какие архивы уже загружены."""
        archives = self.list_for_user(telegram_id)
        if not archives:
            return ""
        lines = ["📦 Ранее загруженные пользователем архивы (доступны через инструменты):"]
        for a in archives:
            lines.append(a.listing())
        return "\n".join(lines)


# Единый экземпляр на процесс бота (используется всеми хендлерами через импорт)
archive_store = ArchiveStore()


# ------------------------------------------------------------------ парсинг ответа модели


# Действия, обрабатываемые напрямую в ReAct-цикле (не через реестр tools.py),
# т.к. они завязаны на archive_store/sandbox, которые физически живут в этом модуле.
BUILTIN_ACTIONS = {"execute_code", "list_archive_files", "read_archive_file"}


@dataclass
class ParsedAction:
    kind: str          # "execute_code" | "list_archive_files" | "read_archive_file" | имя из tools.TOOLS
    payload: dict


def _find_fenced_block(text: str, language: str) -> Optional[str]:
    pattern = rf"```{language}\s*\n(.*?)```"
    match = re.search(pattern, text, flags=re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else None


def parse_model_response(text: str) -> tuple[Optional[ParsedAction], Optional[str]]:
    """Разбирает ответ модели.

    Возвращает кортеж (action, final_text):
    - если найден блок ```json {"action": ...}``` с известным действием (встроенным
      или зарегистрированным в tools.TOOLS) — вернёт (ParsedAction, None);
    - если найден блок ```python ...``` — вернёт (ParsedAction("execute_code"), None);
    - иначе весь текст считается финальным ответом — вернёт (None, text).
    """
    json_block = _find_fenced_block(text, "json")
    if json_block:
        try:
            data = json.loads(json_block)
            action_name = data.get("action")
            if action_name in BUILTIN_ACTIONS or action_name in tools_module.TOOLS:
                return ParsedAction(kind=action_name, payload=data), None
        except (json.JSONDecodeError, AttributeError):
            pass  # некорректный JSON — считаем, что это был просто текст с примером кода

    python_blocks = sandbox.extract_code_blocks(text, language="python")
    if python_blocks:
        code = "\n\n".join(python_blocks)
        return ParsedAction(kind="execute_code", payload={"code": code}), None

    return None, text.strip()


# ------------------------------------------------------------------ результат работы агента


@dataclass
class AgentStep:
    kind: str            # "execute_code" | "list_archive_files" | "read_archive_file"
    detail: str           # что именно сделал агент (для лога/отображения пользователю)
    observation: str      # что получил в ответ


@dataclass
class AgentResult:
    final_text: str
    steps: list[AgentStep]
    stopped_due_to_limit: bool = False


# ------------------------------------------------------------------ основной цикл ReAct


async def run_agent_turn(
    key: ApiKey,
    model: str,
    messages: list[dict],
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout: float,
    telegram_id: int,
    max_steps: Optional[int] = None,
    on_step: Optional[Callable[[str], Awaitable[None]]] = None,
    confirm_code_callback: Optional[Callable[[str], Awaitable[bool]]] = None,
    reasoning_effort: Optional[str] = None,
    db: Optional[Any] = None,
    send_photo: Optional[Callable[[bytes, str], Awaitable[None]]] = None,
) -> AgentResult:
    """Запускает ReAct-цикл: модель -> действие -> наблюдение -> модель -> ...

    `messages` должен уже содержать системный промпт (включая AGENT_SYSTEM_PROMPT)
    и всю историю диалога вплоть до последнего сообщения пользователя.
    Функция мутирует локальную копию `messages`, добавляя туда служебные шаги
    цикла, но НЕ трогает историю в базе данных — за сохранение в БД финального
    ответа (и опционально сокращённого лога шагов) отвечает вызывающий код.

    `confirm_code_callback`, если передан, вызывается с текстом сгенерированного
    кода ПЕРЕД каждым его выполнением в песочнице и должен вернуть True (выполнить)
    или False (отклонить). Если пользователь отклоняет выполнение — модели
    возвращается наблюдение об отказе, и она может либо предложить другой
    подход, либо дать финальный ответ без выполнения кода. Если callback не
    передан — код выполняется без подтверждения (как раньше).

    `db` и `send_photo` пробрасываются в ToolContext для инструментов, которым
    нужен доступ к API-ключам пользователя (например, generate_image) —
    если не переданы, такие инструменты вернут понятную ошибку вместо падения.
    """
    tool_ctx = tools_module.ToolContext(telegram_id=telegram_id, db=db, send_photo=send_photo)
    if max_steps is None:
        max_steps = config.agent_max_steps

    working_messages = list(messages)
    steps: list[AgentStep] = []

    for step_index in range(max_steps):
        response_text = await simple_chat_completion(
            key, model, working_messages, temperature, top_p, max_tokens, timeout,
            reasoning_effort=reasoning_effort,
        )
        if not response_text:
            return AgentResult(final_text="(модель вернула пустой ответ)", steps=steps)

        action, final_text = parse_model_response(response_text)

        if action is None:
            return AgentResult(final_text=final_text or response_text, steps=steps)

        working_messages.append({"role": "assistant", "content": response_text})

        if action.kind == "execute_code":
            code = action.payload.get("code", "")

            if confirm_code_callback is not None:
                approved = await confirm_code_callback(code)
                if not approved:
                    observation = (
                        "Пользователь ОТКЛОНИЛ выполнение этого кода в песочнице. "
                        "Код НЕ был выполнен. Либо предложи пользователю другой, более "
                        "безопасный/понятный подход и объясни, что предлагаешь сделать, "
                        "либо, если дальнейшие действия не нужны, просто ответь пользователю текстом."
                    )
                    steps.append(
                        AgentStep(kind="execute_code_denied", detail=code, observation=observation)
                    )
                    working_messages.append(
                        {
                            "role": "user",
                            "content": (
                                "[АВТОМАТИЧЕСКОЕ СООБЩЕНИЕ СИСТЕМЫ — результат выполнения твоего "
                                f"последнего действия «execute_code»]\n\n{observation}"
                            ),
                        }
                    )
                    continue

            if on_step:
                await on_step("⚙️ Выполняю сгенерированный код в песочнице…")
            result = await sandbox.run_python_code(code, timeout=sandbox.DEFAULT_TIMEOUT_SECONDS)
            observation = result.to_llm_text()
            steps.append(AgentStep(kind="execute_code", detail=code, observation=observation))

        elif action.kind == "list_archive_files":
            archive_id = str(action.payload.get("archive_id", ""))
            archive = archive_store.get(telegram_id, archive_id)
            if on_step:
                await on_step(f"📂 Смотрю список файлов архива {archive_id}…")
            observation = archive.listing() if archive else f"Архив с id={archive_id!r} не найден."
            steps.append(AgentStep(kind="list_archive_files", detail=archive_id, observation=observation))

        elif action.kind == "read_archive_file":
            archive_id = str(action.payload.get("archive_id", ""))
            filename = str(action.payload.get("filename", ""))
            if on_step:
                await on_step(f"📄 Читаю файл «{filename}» из архива {archive_id}…")
            archive = archive_store.get(telegram_id, archive_id)
            text_file = archive.text_file_by_name(filename) if archive else None
            if not archive:
                observation = f"Архив с id={archive_id!r} не найден."
            elif text_file is None:
                available = ", ".join(f.filename for f in archive.text_files) or "—"
                observation = f"Файл {filename!r} не найден в архиве {archive_id!r}. Доступные файлы: {available}"
            else:
                # Модели отдаём полное содержимое файла (без обрезки под лимит промпта) —
                # ведь она сама явно попросила именно этот файл, значит, он ей важен целиком.
                observation = text_file.full_content
            steps.append(AgentStep(kind="read_archive_file", detail=f"{archive_id}/{filename}", observation=observation))

        elif action.kind in tools_module.TOOLS:
            if on_step:
                await on_step(f"🛠 Использую инструмент «{action.kind}»…")
            observation = await tools_module.call_tool(action.kind, action.payload, ctx=tool_ctx)
            steps.append(AgentStep(kind=action.kind, detail=str(action.payload), observation=observation))

        else:
            # неизвестное действие — не должно происходить благодаря парсеру, но на всякий случай
            return AgentResult(final_text=response_text, steps=steps)

        # Наблюдение добавляем как сообщение пользователя со специальной пометкой —
        # это гарантированно поддерживается ЛЮБОЙ моделью (в отличие от роли "tool",
        # которая в части NIM-контейнеров ожидает строгого соответствия tool_call_id).
        working_messages.append(
            {
                "role": "user",
                "content": (
                    "[АВТОМАТИЧЕСКОЕ СООБЩЕНИЕ СИСТЕМЫ — результат выполнения твоего "
                    f"последнего действия «{action.kind}»]\n\n{observation}\n\n"
                    "Продолжай решение задачи (выполни ещё код/инструмент при необходимости) "
                    "или дай финальный текстовый ответ пользователю."
                ),
            }
        )

    return AgentResult(
        final_text=(
            "⚠️ Достигнут лимит шагов агента ({} шагов), не дождавшись финального ответа модели. "
            "Вот последний промежуточный результат:\n\n{}".format(max_steps, steps[-1].observation if steps else "—")
        ),
        steps=steps,
        stopped_due_to_limit=True,
    )
