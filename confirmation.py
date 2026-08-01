"""Механизм подтверждения выполнения кода пользователем через inline-кнопки.

Как это работает:
    ReAct-цикл агента (llm_agent.run_agent_turn) выполняется в одной корутине,
    а нажатие пользователем inline-кнопки "Выполнить"/"Отклонить" обрабатывается
    aiogram в СОВЕРШЕННО ДРУГОЙ корутине (отдельный апдейт от Telegram). Чтобы
    "приостановить" цикл агента и дождаться решения пользователя, используется
    классический паттерн ожидания через `asyncio.Future`:

    1. Перед выполнением кода агент регистрирует новый "запрос на подтверждение"
       (`create_pending_confirmation`) — получает уникальный `confirmation_id`
       и связанный с ним `asyncio.Future`.
    2. Агент отправляет пользователю сообщение с кодом и кнопками, где
       callback_data содержит этот `confirmation_id`.
    3. Агент делает `await` на Future (с таймаутом) — корутина "засыпает",
       не блокируя при этом обработку других апдейтов бота (это же asyncio).
    4. Когда пользователь нажимает кнопку — отдельный хендлер в handlers/chat.py
       находит Future по `confirmation_id` и вызывает `resolve_confirmation(...)`,
       что "будит" ждущую корутину агента с результатом (True/False).
    5. Если пользователь не нажал ничего за отведённое время — Future истекает
       по таймауту, и это трактуется как отказ (безопасное поведение по
       умолчанию — код не выполняется, если нет явного согласия).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass


@dataclass
class PendingConfirmation:
    confirmation_id: str
    telegram_id: int
    future: "asyncio.Future[bool]"


class ConfirmationStore:
    def __init__(self) -> None:
        self._pending: dict[str, PendingConfirmation] = {}

    def create(self, telegram_id: int) -> PendingConfirmation:
        confirmation_id = uuid.uuid4().hex[:12]
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        pending = PendingConfirmation(
            confirmation_id=confirmation_id, telegram_id=telegram_id, future=future
        )
        self._pending[confirmation_id] = pending
        return pending

    def resolve(self, confirmation_id: str, approved: bool, resolver_telegram_id: int) -> bool:
        """Пытается разрешить ожидающее подтверждение. Возвращает True, если
        подтверждение было найдено и относилось именно к этому пользователю
        (защита от того, чтобы один пользователь не мог подтвердить/отклонить
        код, сгенерированный для другого)."""
        pending = self._pending.get(confirmation_id)
        if pending is None:
            return False
        if pending.telegram_id != resolver_telegram_id:
            return False
        if not pending.future.done():
            pending.future.set_result(approved)
        self._pending.pop(confirmation_id, None)
        return True

    def discard(self, confirmation_id: str) -> None:
        self._pending.pop(confirmation_id, None)


# Единый экземпляр на процесс бота
confirmation_store = ConfirmationStore()
