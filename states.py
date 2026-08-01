from aiogram.fsm.state import State, StatesGroup


class AddApiKeyStates(StatesGroup):
    waiting_name = State()
    waiting_provider = State()
    waiting_base_url = State()
    waiting_api_key = State()
    waiting_model_mode = State()  # выбор: указать модель вручную ИЛИ дать доступ ко всем моделям ключа
    waiting_model = State()


class EditKeyModelStates(StatesGroup):
    """Изменение модели по умолчанию у уже существующего ключа (ручной режим)."""
    waiting_model = State()


class WhitelistStates(StatesGroup):
    waiting_add_id = State()
    waiting_remove_id = State()


class SettingsStates(StatesGroup):
    waiting_system_prompt = State()
    waiting_temperature = State()
    waiting_top_p = State()
    waiting_max_tokens = State()
    waiting_model_override = State()


class PlanStates(StatesGroup):
    """Создание/редактирование тарифа администратором."""
    waiting_name = State()
    waiting_description = State()
    waiting_price = State()
    waiting_rpm = State()
    waiting_rph = State()
    waiting_rpd = State()
    # редактирование одного конкретного поля уже существующего тарифа
    editing_field = State()


class GrantSubscriptionStates(StatesGroup):
    """Выдача/изменение подписки конкретному пользователю администратором."""
    waiting_telegram_id = State()
    waiting_plan_choice = State()
    waiting_custom_duration = State()
    waiting_limit_value = State()


class PaymentContactStates(StatesGroup):
    waiting_contact = State()


class CustomSubscriptionStates(StatesGroup):
    """Выдача кастомной (персональной) подписки: выбор моделей происходит через\n    инлайн-кнопки без привязки к состоянию (данные копятся в FSMContext.data),\n    а название и срок действия запрашиваются текстом."""
    waiting_name = State()
    waiting_custom_duration = State()


class CustomSubscriptionEditStates(StatesGroup):
    """Редактирование отдельных полей УЖЕ ВЫДАННОЙ кастомной подписки
    (полноценное управление, аналогичное управлению обычным тарифом)."""
    waiting_name = State()
    waiting_limit_value = State()
    waiting_duration = State()


class BroadcastStates(StatesGroup):
    waiting_text = State()


class ManageAdminsStates(StatesGroup):
    waiting_add_id = State()


class BanUserStates(StatesGroup):
    waiting_reason = State()


class PromoRedeemStates(StatesGroup):
    """Ввод промокода обычным пользователем."""
    waiting_code = State()


class PromoCreateStates(StatesGroup):
    """Создание нового промокода владельцем. Выбор типа награды/моделей —
    через инлайн-кнопки (данные копятся в FSMContext.data), остальное — текстом."""
    waiting_code_text = State()
    waiting_custom_name = State()
    waiting_custom_rpm = State()
    waiting_custom_rph = State()
    waiting_custom_rpd = State()
    waiting_custom_duration = State()
    waiting_max_activations = State()


class PromoEditStates(StatesGroup):
    """Редактирование одного конкретного поля уже существующего промокода."""
    editing_field = State()
