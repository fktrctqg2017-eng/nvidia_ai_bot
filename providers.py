"""Работа с провайдерами через OpenAI-совместимый протокол.

NVIDIA Cloud API (build.nvidia.com / integrate.api.nvidia.com) и
NVIDIA NIM (self-hosted микросервисы) оба реализуют OpenAI-совместимый
REST API, поэтому оба поддерживаются через один и тот же клиент
`openai.AsyncOpenAI`, различаясь только `base_url` (и, для NIM, часто
не требуется валидный API-ключ, но поле обязательно у SDK, поэтому
допускается placeholder вроде "not-needed").

Управление "мышлением" (reasoning/thinking) модели:
    Разные семейства моделей, доступных через NVIDIA Cloud API/NIM, включают
    и настраивают reasoning совершенно по-разному:
      - Llama Nemotron: системный промпт "detailed thinking on"/"off";
      - Nemotron Nano/Super: "/think" или "/no_think" в системном промпте;
      - DeepSeek-R1/V3, Qwen3 и многие модели во vLLM/NIM: параметр
        chat_template_kwargs={"thinking": true/false} или
        {"enable_thinking": true/false} в extra_body;
      - модели с "нативной" OpenAI-style поддержкой: стандартный параметр
        reasoning_effort ("low"/"medium"/"high").
    Чтобы бот одинаково хорошо работал с ЛЮБОЙ из них без необходимости
    вручную указывать "тип" модели, при каждом запросе мы одновременно
    передаём ВСЕ эти варианты (сервер молча игнорирует те, что его модель
    не поддерживает — лишние поля в chat_template_kwargs/extra_body не
    вызывают ошибок ни в vLLM, ни в NIM, ни в NVIDIA Cloud API). Единственное
    исключение — управление через системный промпт ("detailed thinking on"),
    которое встраивается в системное сообщение диалога отдельно, так как
    это не параметр запроса, а часть самого промпта.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Optional

from openai import APIError, AsyncOpenAI, AuthenticationError

from database import ApiKey

# Уровни "усилия" мышления, которые понимает бот (единый словарь для UI).
REASONING_EFFORT_OFF = "off"
REASONING_EFFORT_LOW = "low"
REASONING_EFFORT_MEDIUM = "medium"
REASONING_EFFORT_HIGH = "high"

REASONING_EFFORT_LABELS = {
    REASONING_EFFORT_OFF: "🚫 Выключено (без рассуждений, быстрее)",
    REASONING_EFFORT_LOW: "🌱 Низкое",
    REASONING_EFFORT_MEDIUM: "🌿 Среднее",
    REASONING_EFFORT_HIGH: "🌳 Высокое (медленнее, подробнее)",
}


def _reasoning_extra_body(effort: Optional[str]) -> Optional[dict]:
    """Строит extra_body с параметрами reasoning в НЕСКОЛЬКИХ форматах сразу,
    чтобы покрыть максимум моделей/бэкендов (vLLM/NIM молча игнорируют
    незнакомые ключи chat_template_kwargs).

    Если effort пуст или "off" — НЕ отправляем extra_body вообще (возвращаем
    None), а не {"thinking": False, ...}. Так надёжнее: некоторые модели/NIM
    контейнеры включают reasoning по умолчанию и реагируют только на явное
    отсутствие параметра, а часть простых (не reasoning) моделей может
    вернуть ошибку на незнакомый chat_template_kwargs. Раз бот не просит
    рассуждать — лучше вообще не трогать этот параметр.
    """
    if not effort or effort == REASONING_EFFORT_OFF:
        return None
    chat_template_kwargs = {
        # DeepSeek-V3.x/R1-дистилляты, часть моделей во vLLM
        "thinking": True,
        # Qwen3, QwQ и похожие модели
        "enable_thinking": True,
        "reasoning_effort": effort,
    }
    return {"chat_template_kwargs": chat_template_kwargs}


def reasoning_system_prompt_hint(effort: Optional[str]) -> Optional[str]:
    """Часть системного промпта для моделей семейства Llama Nemotron, которые
    управляют reasoning исключительно через текст системного сообщения
    (это не параметр запроса, поэтому обрабатывается отдельно от extra_body).

    Если effort пуст или "off" — ничего не добавляем в системный промпт вообще
    (возвращаем None). Раньше здесь всегда возвращалась строка "detailed
    thinking off", но у части моделей, которые НЕ поддерживают такой
    переключатель, наличие в системном промпте фразы про "detailed thinking"
    может сбивать модель с толку или неожиданно менять её поведение — а раз
    пользователь явно выключил reasoning, самое безопасное — просто не
    упоминать эту тему в промпте вовсе.
    """
    if not effort or effort == REASONING_EFFORT_OFF:
        return None
    return "detailed thinking on"

PROVIDER_NVIDIA_CLOUD = "nvidia_cloud"
PROVIDER_NVIDIA_NIM = "nvidia_nim"

PROVIDER_LABELS = {
    PROVIDER_NVIDIA_CLOUD: "NVIDIA Cloud API (integrate.api.nvidia.com)",
    PROVIDER_NVIDIA_NIM: "NVIDIA NIM (self-hosted)",
}

DEFAULT_BASE_URLS = {
    PROVIDER_NVIDIA_CLOUD: "https://integrate.api.nvidia.com/v1",
    PROVIDER_NVIDIA_NIM: "http://localhost:8000/v1",
}

DEFAULT_MODELS = {
    PROVIDER_NVIDIA_CLOUD: "meta/llama-3.1-70b-instruct",
    PROVIDER_NVIDIA_NIM: "meta/llama3-8b-instruct",
}


@dataclass
class ChatChunk:
    delta: str = ""
    reasoning_delta: str = ""   # часть "цепочки рассуждений" (reasoning_content), если модель её отдаёт
    finished: bool = False
    error: Optional[str] = None


def build_client(key: ApiKey, timeout: float) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=key.api_key or "not-needed",
        base_url=key.base_url,
        timeout=timeout,
    )


async def list_models(key: ApiKey, timeout: float) -> list[str]:
    """Возвращает список доступных моделей у провайдера (для NIM особенно полезно)."""
    client = build_client(key, timeout)
    try:
        page = await client.models.list()
        return sorted(m.id for m in page.data)
    finally:
        await client.close()


async def stream_chat_completion(
    key: ApiKey,
    model: str,
    messages: list[dict],
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout: float,
    reasoning_effort: Optional[str] = None,
) -> AsyncIterator[ChatChunk]:
    """Стримит ответ модели чанками. Гарантированно закрывает клиент по завершении.

    Если весь max_tokens ушёл на reasoning_content и ни одного символа
    настоящего content так и не появилось (finish_reason == "length") —
    в конце потока отдаётся понятное объяснение вместо тихого пустого ответа.
    """
    client = build_client(key, timeout)
    got_content = False
    got_reasoning = False
    finish_reason = None
    try:
        extra_body = _reasoning_extra_body(reasoning_effort)
        stream = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            stream=True,
            extra_body=extra_body,
        )
        async for event in stream:
            if not event.choices:
                continue
            choice = event.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = choice.delta
            content = getattr(delta, "content", None)
            # reasoning_content — нестандартное поле, которое возвращают многие
            # reasoning-модели через NVIDIA API/NIM (DeepSeek-R1, Qwen3 и др.).
            reasoning_content = getattr(delta, "reasoning_content", None)
            if content:
                got_content = True
            if reasoning_content:
                got_reasoning = True
            if content or reasoning_content:
                yield ChatChunk(delta=content or "", reasoning_delta=reasoning_content or "")

        if not got_content and got_reasoning and finish_reason == "length":
            yield ChatChunk(
                delta=(
                    "\n\n⚠️ Модель не успела дать ответ — весь лимит max_tokens был "
                    "исчерпан на рассуждения. Увеличьте «📏 Max tokens» в настройках "
                    "или снизьте «💭 Уровень мышления»."
                )
            )
        yield ChatChunk(finished=True)
    except AuthenticationError as e:
        yield ChatChunk(error=f"Ошибка аутентификации у провайдера: {e}")
    except APIError as e:
        yield ChatChunk(error=f"Ошибка API провайдера: {e}")
    except Exception as e:  # noqa: BLE001
        yield ChatChunk(error=f"Непредвиденная ошибка: {e}")
    finally:
        await client.close()


async def simple_chat_completion(
    key: ApiKey,
    model: str,
    messages: list[dict],
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout: float,
    reasoning_effort: Optional[str] = None,
) -> str:
    """Нестриминговый вызов (fallback, если провайдер не поддерживает streaming).

    Если content пуст, но модель явно упёрлась в лимит max_tokens
    (finish_reason == "length") — это почти всегда значит, что весь бюджет
    токенов ушёл на "размышления" (reasoning_content) у reasoning-моделей,
    а на сам ответ токенов не осталось. В этом случае возвращаем понятное
    объяснение вместо тихого пустого ответа.
    """
    client = build_client(key, timeout)
    try:
        extra_body = _reasoning_extra_body(reasoning_effort)
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            stream=False,
            extra_body=extra_body,
        )
        choice = resp.choices[0]
        content = choice.message.content or ""
        if not content and choice.finish_reason == "length":
            reasoning_len = len(getattr(choice.message, "reasoning_content", "") or "")
            hint = (
                f" (модель потратила ~{reasoning_len} символов на рассуждения)"
                if reasoning_len else ""
            )
            return (
                "⚠️ Модель не успела дать ответ — весь лимит max_tokens был исчерпан "
                f"до появления текста ответа{hint}. Увеличьте «📏 Max tokens» в "
                "настройках или снизьте «💭 Уровень мышления»."
            )
        return content
    finally:
        await client.close()


# ------------------------------------------------------------------ распознавание речи (ASR)


async def transcribe_audio(
    key: ApiKey,
    audio_path: str,
    timeout: float,
    language: Optional[str] = None,
) -> str:
    """Распознаёт речь в аудиофайле через OpenAI-совместимый эндпоинт
    `/v1/audio/transcriptions` (формат Whisper API).

    Работает с любым ключом, помеченным ролью 'asr' — это может быть:
      - self-hosted NVIDIA Speech NIM (Parakeet/Canary), который отдаёт
        именно такой HTTP-эндпоинт "из коробки";
      - любой другой Whisper-совместимый сервис (например, whisper.cpp
        server с флагом --inference-path /v1/audio/transcriptions,
        или сторонний облачный Whisper API).

    Модель ключа (`key.model`) передаётся как имя модели транскрипции —
    для self-hosted NIM обычно можно оставить пустым или указать имя,
    которое ожидает конкретный сервис (уточняется в его документации).
    """
    client = build_client(key, timeout)
    try:
        with open(audio_path, "rb") as f:
            kwargs = {"file": f, "model": key.model or "whisper-1"}
            if language:
                kwargs["language"] = language
            result = await client.audio.transcriptions.create(**kwargs)
        # SDK может вернуть либо объект с .text, либо голую строку — в
        # зависимости от response_format, поддерживаемого конкретным сервисом.
        return getattr(result, "text", None) or (result if isinstance(result, str) else "")
    finally:
        await client.close()


# ------------------------------------------------------------------ генерация изображений


async def generate_image(
    key: ApiKey,
    prompt: str,
    timeout: float,
    size: str = "1024x1024",
) -> bytes:
    """Генерирует изображение через OpenAI-совместимый эндпоинт `/v1/images/generations`
    (NVIDIA NIM Visual GenAI — Stable Diffusion, FLUX и т.п. в OpenAI-режиме).

    Возвращает сырые байты изображения (PNG/JPEG). Требует ключ с ролью
    'image_gen' и моделью, поддерживающей генерацию изображений
    (например, "stabilityai/stable-diffusion-3.5-large" или модель FLUX).
    """
    import base64 as _base64

    client = build_client(key, timeout)
    try:
        resp = await client.images.generate(
            model=key.model,
            prompt=prompt,
            n=1,
            size=size,
            response_format="b64_json",
        )
        b64_data = resp.data[0].b64_json
        if not b64_data:
            raise RuntimeError("Провайдер не вернул данные изображения (b64_json пуст).")
        return _base64.b64decode(b64_data)
    finally:
        await client.close()
