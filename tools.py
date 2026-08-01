"""Реестр инструментов ("tools") для AI-агента.

Архитектура:
    Каждый инструмент — это async-функция вида `async def run(**kwargs) -> str`,
    зарегистрированная в словаре TOOLS под своим именем действия ("action").
    Модель просит вызвать инструмент, вернув JSON вида:
        {"action": "web_search", "query": "..."}
    Бот (см. llm_agent.py) находит функцию в TOOLS по имени action, вызывает её
    с остальными полями JSON как kwargs и возвращает результат (строку) модели
    как "наблюдение". Это позволяет добавлять НОВЫЕ инструменты, просто дописав
    функцию в этот файл и одну запись в TOOLS/TOOL_DESCRIPTIONS — не трогая
    остальной код агента.

    Инструменты, которым нужен доступ к API-ключам/telegram_id (генерация
    изображений, отправка результата в чат и т.п.), получают дополнительный
    объект `ToolContext` — он передаётся в run() как kwarg `_ctx` агентом.

Все инструменты обязаны:
    - никогда не бросать исключения наружу (ловить всё внутри и возвращать
      текст ошибки как обычный результат — иначе упадёт весь ReAct-цикл);
    - возвращать разумно ограниченный по размеру текст (агент сам не обрезает
      результаты инструментов, чтобы не терять важную информацию, поэтому
      каждый инструмент обрезает себя сам, где это уместно).
"""

from __future__ import annotations

import ast
import operator
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx

MAX_TOOL_RESULT_CHARS = 8000
HTTP_TIMEOUT = 15.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass
class ToolContext:
    """Контекст, доступный инструментам, которым нужны API-ключи/telegram_id
    (например, генерация изображений). Заполняется в llm_agent.run_agent_turn.
    """

    telegram_id: int
    db: Any = None  # database.Database — типизировать как Any, чтобы избежать циклического импорта
    send_photo: Optional[Callable[[bytes, str], Awaitable[None]]] = None


def _truncate(text: str, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [результат обрезан, всего {len(text)} символов]"


# ------------------------------------------------------------------ 1. Калькулятор (безопасный eval)

_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARYOPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _safe_eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"Недопустимая константа: {node.value!r}")
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        left = _safe_eval_node(node.left)
        right = _safe_eval_node(node.right)
        return _ALLOWED_BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
        return _ALLOWED_UNARYOPS[type(node.op)](_safe_eval_node(node.operand))
    raise ValueError(f"Недопустимая операция в выражении: {type(node).__name__}")


async def tool_calculator(expression: str = "", **_: Any) -> str:
    """Безопасно вычисляет арифметическое выражение (без произвольного кода —
    только числа и +, -, *, /, //, %, **, скобки). Для сложных вычислений
    (циклы, функции, библиотеки) агент должен использовать execute_code."""
    if not expression:
        return "Ошибка: не передано выражение для вычисления (параметр 'expression')."
    try:
        tree = ast.parse(expression, mode="eval")
        result = _safe_eval_node(tree.body)
        return f"Результат: {result}"
    except ZeroDivisionError:
        return "Ошибка: деление на ноль."
    except Exception as e:  # noqa: BLE001
        return f"Ошибка вычисления выражения {expression!r}: {e}"


# ------------------------------------------------------------------ 2. Текущая дата и время


async def tool_current_datetime(**_: Any) -> str:
    """Возвращает текущую дату и время (UTC и локальное представление)."""
    now_utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    weekday = time.strftime("%A", time.gmtime())
    return f"Текущая дата и время (UTC): {now_utc}, день недели: {weekday}"


# ------------------------------------------------------------------ 3. Веб-поиск (DuckDuckGo, без API-ключа)


async def tool_web_search(query: str = "", max_results: int = 5, **_: Any) -> str:
    """Ищет в интернете через DuckDuckGo (HTML-версия, не требует API-ключа)
    и возвращает список найденных страниц: заголовок, ссылка, краткое описание.
    """
    if not query:
        return "Ошибка: не передан поисковый запрос (параметр 'query')."
    max_results = max(1, min(int(max_results or 5), 10))

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": USER_AGENT},
            )
            resp.raise_for_status()
    except Exception as e:  # noqa: BLE001
        return f"Ошибка веб-поиска: не удалось обратиться к поисковику ({e})."

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(resp.text, "html.parser")
        results = soup.select(".result")[:max_results]
        if not results:
            return f"По запросу {query!r} ничего не найдено."

        lines = [f"Результаты веб-поиска по запросу {query!r}:\n"]
        for i, res in enumerate(results, start=1):
            title_el = res.select_one(".result__title")
            snippet_el = res.select_one(".result__snippet")
            link_el = res.select_one(".result__a")

            title = title_el.get_text(strip=True) if title_el else "(без заголовка)"
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""

            url = ""
            if link_el is not None:
                href = link_el.get("href", "")
                parsed = urlparse(href)
                qs = parse_qs(parsed.query)
                url = unquote(qs.get("uddg", [href])[0])

            lines.append(f"{i}. {title}\n   URL: {url}\n   {snippet}\n")

        return _truncate("\n".join(lines))
    except Exception as e:  # noqa: BLE001
        return f"Ошибка разбора результатов поиска: {e}"


# ------------------------------------------------------------------ 4. Чтение содержимого веб-страницы по URL


async def tool_fetch_url(url: str = "", **_: Any) -> str:
    """Скачивает страницу по URL и возвращает её текстовое содержимое
    (без HTML-тегов, скриптов и стилей), обрезанное до разумного размера."""
    if not url:
        return "Ошибка: не передан URL (параметр 'url')."
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
    except Exception as e:  # noqa: BLE001
        return f"Ошибка загрузки страницы {url!r}: {e}"

    content_type = resp.headers.get("content-type", "")
    if "text/html" not in content_type and "text/plain" not in content_type:
        return f"Страница {url!r} имеет тип содержимого {content_type!r} — не текст/HTML, пропущено."

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        lines = [line for line in text.split("\n") if line.strip()]
        cleaned = "\n".join(lines)
        return _truncate(cleaned or "(страница не содержит текста)")
    except Exception as e:  # noqa: BLE001
        return f"Ошибка разбора страницы {url!r}: {e}"


# ------------------------------------------------------------------ 5. Генерация изображения (требует ключ с ролью image_gen)


async def tool_generate_image(prompt: str = "", _ctx: Optional[ToolContext] = None, **_: Any) -> str:
    """Генерирует изображение по текстовому описанию через NVIDIA NIM Visual GenAI
    (Stable Diffusion / FLUX и т.п.) и отправляет его пользователю в Telegram.
    Требует, чтобы администратор добавил и активировал ключ с ролью
    'генерация изображений' (см. «🔑 Ключи» → выбрать ключ → «🎨 Роль: image_gen»),
    а пользователь выбрал его в «⚙️ Настройки → 🎨 Ключ для изображений».
    """
    if not prompt:
        return "Ошибка: не передано описание изображения (параметр 'prompt')."
    if _ctx is None or _ctx.db is None:
        return "Ошибка: инструмент генерации изображений недоступен в этом контексте."

    from database import KEY_ROLE_IMAGE_GEN
    import providers

    settings = await _ctx.db.get_user_settings(_ctx.telegram_id)
    key = None
    if settings.active_image_key_id:
        key = await _ctx.db.get_api_key(settings.active_image_key_id)

    if not key or not key.is_active or key.role != KEY_ROLE_IMAGE_GEN:
        return (
            "Генерация изображений недоступна: не выбран рабочий ключ с ролью "
            "'генерация изображений'. Сообщи пользователю, что нужно попросить "
            "администратора добавить такой ключ в «🔑 Ключи», а затем выбрать его "
            "в «⚙️ Настройки → 🎨 Ключ для изображений»."
        )

    try:
        image_bytes = await providers.generate_image(key, prompt, timeout=120.0)
    except Exception as e:  # noqa: BLE001
        return f"Ошибка генерации изображения: {e}"

    if _ctx.send_photo is not None:
        try:
            await _ctx.send_photo(image_bytes, prompt)
        except Exception as e:  # noqa: BLE001
            return f"Изображение сгенерировано, но не удалось отправить его пользователю: {e}"
        return f"Изображение по запросу {prompt!r} успешно сгенерировано и отправлено пользователю в чат."
    return "Изображение сгенерировано, но механизм отправки в чат недоступен."


# ------------------------------------------------------------------ реестр инструментов

# Имя действия -> функция-обработчик
TOOLS: dict[str, Callable[..., Awaitable[str]]] = {
    "calculator": tool_calculator,
    "current_datetime": tool_current_datetime,
    "web_search": tool_web_search,
    "fetch_url": tool_fetch_url,
    "generate_image": tool_generate_image,
}

# Человекочитаемое описание каждого инструмента для системного промпта агента.
# Формат специально составлен единообразно, чтобы новые инструменты было легко
# дописывать в том же стиле.
TOOL_DESCRIPTIONS: dict[str, str] = {
    "calculator": (
        '{"action": "calculator", "expression": "2 + 2 * 2"}\n'
        "  Точно вычисляет арифметическое выражение (+, -, *, /, //, %, **, скобки)."
    ),
    "current_datetime": (
        '{"action": "current_datetime"}\n'
        "  Возвращает текущую дату и время (UTC)."
    ),
    "web_search": (
        '{"action": "web_search", "query": "поисковый запрос", "max_results": 5}\n'
        "  Ищет актуальную информацию в интернете (заголовки, ссылки, краткие описания)."
    ),
    "fetch_url": (
        '{"action": "fetch_url", "url": "https://example.com/page"}\n'
        "  Скачивает и возвращает текстовое содержимое конкретной веб-страницы по ссылке."
    ),
    "generate_image": (
        '{"action": "generate_image", "prompt": "подробное описание желаемого изображения"}\n'
        "  Генерирует изображение и отправляет его пользователю в чат "
        "(требует настроенный ключ для генерации изображений)."
    ),
}


def tool_names() -> set[str]:
    return set(TOOLS.keys())


async def call_tool(action: str, payload: dict, ctx: Optional[ToolContext] = None) -> str:
    """Единая точка вызова инструмента по имени action. Никогда не бросает
    исключения — при любой ошибке возвращает текстовое описание проблемы,
    чтобы ReAct-цикл агента не падал целиком из-за одного неудачного вызова."""
    func = TOOLS.get(action)
    if func is None:
        return f"Ошибка: неизвестный инструмент {action!r}."
    kwargs = {k: v for k, v in payload.items() if k != "action"}
    try:
        return await func(_ctx=ctx, **kwargs)
    except TypeError as e:
        return f"Ошибка вызова инструмента {action!r}: некорректные параметры ({e})."
    except Exception as e:  # noqa: BLE001
        return f"Непредвиденная ошибка при выполнении инструмента {action!r}: {e}"
