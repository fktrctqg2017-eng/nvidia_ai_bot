"""Бизнес-логика промокодов: активация, проверка условий, выдача награды.

Правила:
    - Создавать/включать/отключать/удалять/редактировать промокоды может
      ТОЛЬКО владелец (проверяется в handlers/admin.py через is_owner).
    - Администраторы и владелец САМИ промокоды активировать не могут
      (это инструмент привлечения обычных пользователей, не для служебных
      аккаунтов) — проверяется здесь через is_admin.
    - Один и тот же пользователь может активировать ОДИН И ТОТ ЖЕ промокод
      только ОДИН РАЗ (см. database.promo_code_activations, UNIQUE-констрейнт).
    - "Количество" промокода — это max_activations: сколько РАЗНЫХ
      пользователей суммарно могут его активировать.
    - target_audience='subscribers_only' — активировать может только тот, у
      кого уже ЕСТЬ активная подписка (любого типа) на момент активации;
      'all' — доступно всем, включая тех, у кого нет подписки.
    - Награда (reward_type='plan') выдаёт обычную (plans) подписку;
      (reward_type='custom') выдаёт персональную (custom_subscriptions)
      подписку — оба варианта используют db.grant_subscription /
      db.grant_custom_subscription, поэтому взаимоисключающее правило
      "кастомная вытесняет обычную и наоборот" продолжает работать как и
      всюду в проекте.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from database import Database, PromoCode


@dataclass
class PromoRedeemResult:
    ok: bool
    reason: str = ""
    reward_name: str = ""
    duration_days: Optional[int] = None


async def redeem_promo_code(
    db: Database, telegram_id: int, code: str, is_admin: bool
) -> PromoRedeemResult:
    """Пытается активировать промокод для пользователя. При успехе СРАЗУ
    выдаёт награду (подписку) и записывает активацию — вызывать функцию
    нужно ровно один раз на попытку ввода кода."""
    if is_admin:
        return PromoRedeemResult(False, "🚫 Администраторы и владелец не могут использовать промокоды.")

    code = (code or "").strip().upper()
    if not code:
        return PromoRedeemResult(False, "Промокод не может быть пустым.")

    promo = await db.get_promo_code(code)
    if promo is None:
        return PromoRedeemResult(False, "❗ Промокод не найден. Проверьте правильность ввода.")

    if not promo.is_active:
        return PromoRedeemResult(False, "❗ Этот промокод отключен.")

    if not promo.is_time_valid():
        return PromoRedeemResult(False, "❗ Срок действия этого промокода истёк или ещё не наступил.")

    if await db.has_user_activated_promo(code, telegram_id):
        return PromoRedeemResult(False, "❗ Вы уже использовали этот промокод ранее.")

    if not promo.has_activations_left():
        return PromoRedeemResult(False, "❗ Лимит активаций этого промокода уже исчерпан.")

    if promo.target_audience == "subscribers_only":
        # Импортируем здесь, чтобы избежать циклического импорта на уровне модуля.
        import subscriptions as sub_logic

        plan = await sub_logic.get_user_plan(db, telegram_id, is_admin=False)
        if plan is None:
            return PromoRedeemResult(
                False,
                "❗ Этот промокод доступен только пользователям с активной подпиской.",
            )

    # Всё ок — выдаём награду.
    if promo.reward_type == "plan":
        plan = await db.get_plan(promo.plan_id) if promo.plan_id else None
        if plan is None:
            return PromoRedeemResult(False, "⚠️ Тариф, привязанный к промокоду, больше не существует.")
        await db.grant_subscription(telegram_id, plan.id, promo.created_by or telegram_id, promo.duration_days)
        reward_name = plan.name
    else:  # 'custom'
        await db.grant_custom_subscription(
            telegram_id,
            name=promo.custom_name or f"Промокод {code}",
            allowed_key_ids=promo.custom_allowed_key_ids,
            rpm_limit=promo.custom_rpm_limit,
            rph_limit=promo.custom_rph_limit,
            rpd_limit=promo.custom_rpd_limit,
            granted_by=promo.created_by or telegram_id,
            duration_days=promo.duration_days,
        )
        reward_name = promo.custom_name or code

    await db.record_promo_activation(code, telegram_id)

    return PromoRedeemResult(True, reward_name=reward_name, duration_days=promo.duration_days)


def promo_summary_text(promo: PromoCode, plan_name: Optional[str], key_names: dict[int, str]) -> str:
    """Формирует текстовое описание промокода для админ-панели (владельца)."""
    from text_utils import escape_html

    lines = [f"🎟 <b>{escape_html(promo.code)}</b>"]
    lines.append(f"Статус: {'🟢 активен' if promo.is_active else '🔴 отключен'}")

    if promo.reward_type == "plan":
        lines.append(f"Награда: тариф «{escape_html(plan_name or '?')}»")
    else:
        lines.append(f"Награда: кастомная подписка «{escape_html(promo.custom_name or '?')}»")
        if promo.custom_allowed_key_ids:
            names = [key_names.get(kid, f"#{kid}") for kid in promo.custom_allowed_key_ids]
            lines.append("  Модели: " + ", ".join(names))
        limits = []
        if promo.custom_rpm_limit:
            limits.append(f"{promo.custom_rpm_limit}/мин")
        if promo.custom_rph_limit:
            limits.append(f"{promo.custom_rph_limit}/час")
        if promo.custom_rpd_limit:
            limits.append(f"{promo.custom_rpd_limit}/сутки")
        lines.append("  Лимиты: " + (", ".join(limits) if limits else "без ограничений"))

    lines.append(f"Срок выдаваемой подписки: {'бессрочно' if promo.duration_days is None else f'{promo.duration_days} дн.'}")

    max_act = "без ограничения" if promo.max_activations is None else str(promo.max_activations)
    lines.append(f"Активаций: {promo.used_count} / {max_act}")

    audience = "все пользователи" if promo.target_audience == "all" else "только с активной подпиской"
    lines.append(f"Кому доступен: {audience}")

    import time as _time

    if promo.valid_from:
        lines.append(f"Действует с: {_time.strftime('%Y-%m-%d %H:%M UTC', _time.gmtime(promo.valid_from))}")
    if promo.valid_until:
        lines.append(f"Действует до: {_time.strftime('%Y-%m-%d %H:%M UTC', _time.gmtime(promo.valid_until))}")
    if not promo.valid_from and not promo.valid_until:
        lines.append("Срок действия кода: не ограничен")

    return "\n".join(lines)
