from aiogram import Router

from . import admin, browser, chat, common, files, keys, media, models, profile, promo, settings, subscription


def get_root_router() -> Router:
    root = Router(name="root")
    # Порядок важен: сначала общие команды и колбэки меню, затем узкоспециализированные,
    # текстовый чат-хендлер регистрируем последним, чтобы не перехватывать служебные тексты
    # в состояниях FSM (FSM-хендлеры выше по регистрации всегда матчатся первыми в aiogram).
    root.include_router(common.router)
    root.include_router(admin.router)
    root.include_router(profile.router)       # "👤 Профиль"
    root.include_router(promo.router)         # промокоды (активация + админка владельца)
    root.include_router(subscription.router)  # витрина тарифов, доступна всем
    root.include_router(models.router)        # "Мои модели" для подписчиков
    root.include_router(keys.router)          # "API-ключи" для владельца
    root.include_router(settings.router)
    root.include_router(browser.router)  # просмотр файлов загруженных архивов
    root.include_router(files.router)    # обработка документов-архивов (архивы + одиночные документы)
    root.include_router(media.router)    # фото (vision) и голосовые сообщения (ASR)
    root.include_router(chat.router)     # обычный текстовый чат — регистрируем последним
    return root
