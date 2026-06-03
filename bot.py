"""
bot.py — точка входа торгового бота.

Запуск:
  python bot.py --user alex --bot-id phor_dca
  python bot.py --user alex --bot-id btc_mean_rev

Startup sequence:
  1. Парсинг аргументов.
  2. Загрузка AppSettings (Pydantic, из .env / ENV).
  3. Расшифровка секретов через KeyManager (мастер-пароль вводится один раз).
  4. Настройка Observability (EventEmitter, все sinks).
  5. Инициализация подсистем (broker, market, state).
  6. Проверка bot_registry — защита от двойного запуска.
  7. StateRecovery.reconcile() — сверка с биржей после любого рестарта.
  8. BotLoop.run() — бесконечный tick-loop.

При Ctrl+C — корректная остановка с emit BOT_STOPPED.
При любом необработанном исключении — emit BOT_CRASHED, exit(1).

Предположения об интерфейсах П6 (сверить при интеграции):
  - StateRecovery.startup(user_id, bot_id, broker) в†' BotState
  - StateRepository(db_pool) в†' repo
  - RegistryRepository(db_pool) в†' repo
  - StateManager(db_pool, emitter) в†' manager

Предположения об интерфейсах П5:
  - ConfigRepository(db_pool) в†' repo
  - ConfigWatcher(repo, user_id, bot_id) в†' watcher
  - ConfigWatcher.create_snapshot() в†' CycleSnapshot
  - ConfigWatcher.set_close_only(user_id, bot_id) в†' None
  - ConfigWatcher.get_config() в†' BotConfig

Предположения об интерфейсах П3:
  - setup_observability(settings, bot_id, ticker, tg_token, tg_chat_id) в†' EventEmitter

Предположения об интерфейсах П2:
  - ProviderFactory.create(settings.market) в†' MarketDataProvider

Предположения об интерфейсах П4:
  - BrokerFactory.create(settings.broker, token=...) в†' IBroker
"""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys

logger = logging.getLogger(__name__)

from dotenv import load_dotenv
load_dotenv()

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Алготрейдинговый бот",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  python bot.py --user alex --bot-id phor_dca\n"
            "  python bot.py --user alex --bot-id btc_mean_rev\n"
        ),
    )
    parser.add_argument(
        "--user",
        required=True,
        help="Идентификатор пользователя (совпадает с user_id в bot_configs).",
    )
    parser.add_argument(
        "--bot-id",
        required=True,
        dest="bot_id",
        help="Идентификатор бота (совпадает с bot_id в bot_configs).",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="Переопределить уровень логирования из конфига (DEBUG/INFO/WARNING).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    user_id = args.user
    bot_id  = args.bot_id

    # ------------------------------------------------------------------
    # 1. Базовый logging до инициализации Observability
    #    (чтобы видеть ошибки до старта EventEmitter)
    # ------------------------------------------------------------------
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1),
    )

    logger.info("Старт бота: user=%s, bot_id=%s", user_id, bot_id)

    # ------------------------------------------------------------------
    # 2. AppSettings (Рџ0)
    # ------------------------------------------------------------------
    try:
        from config.settings import AppSettings          # noqa: PLC0415
        from config.bot_loop_settings import BotLoopSettings  # noqa: PLC0415
        settings          = AppSettings()
        bot_loop_settings = BotLoopSettings()
    except Exception as exc:
        logger.critical("Не удалось загрузить конфиг: %s", exc)
        sys.exit(1)

    # Переопределение уровня логирования из аргумента CLI
    if args.log_level:
        logging.getLogger().setLevel(args.log_level.upper())

    # ------------------------------------------------------------------
    # 3. KeyManager (П1) — мастер-пароль вводится один раз
    # ------------------------------------------------------------------
    try:
        from keys.key_manager import KeyManager  # noqa: PLC0415
        km = KeyManager()
        master_password = os.environ.get("BOT_MASTER_PASSWORD") or getpass.getpass("Введите мастер-пароль: ")
        km.load(master_password)
        del master_password  # не держать в памяти дольше чем нужно
    except Exception as exc:
        logger.critical("Ошибка загрузки ключей: %s", exc)
        sys.exit(1)

    # ------------------------------------------------------------------
    # 4. Observability (Рџ3)
    #    Эмиттер стартует рано — все ошибки старта идут в Telegram.
    #    Тикер уточняется сразу после загрузки BotConfig (шаг 6).
    # ------------------------------------------------------------------
    try:
        from observability import setup_observability  # noqa: PLC0415

        emitter = setup_observability(
            settings=settings,
            bot_id=bot_id,
            ticker="",   # уточняется в шаге 6 через emitter.set_ticker()
            tg_token=km.get("TG_BOT_TOKEN"),
            tg_chat_id=km.get("TG_CHAT_ID"),
        )
    except Exception as exc:
        logger.critical("Не удалось инициализировать Observability: %s", exc)
        sys.exit(1)

    # ------------------------------------------------------------------
    # 5. База данных — пул соединений
    # ------------------------------------------------------------------
    try:
        from db import init_pool as create_pool  # noqa: PLC0415
        db_pool = create_pool(settings.database.url)
    except Exception as exc:
        logger.critical("Не удалось подключиться к PostgreSQL: %s", exc)
        emitter.emit(
            event_type="PG_CONNECTION_FAILED",
            level="CRITICAL",
            message=f"PostgreSQL недоступен при старте: {exc}",
            payload={"error": str(exc)},
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # 6. Конфигурация бота (П5)
    # ------------------------------------------------------------------
    try:
        from bot_config import ConfigRepository, ConfigWatcher  # noqa: PLC0415
        config_repo    = ConfigRepository(db_pool)
        config_watcher = ConfigWatcher(config_repo, user_id=user_id, bot_id=bot_id)

        # Первичная загрузка конфига — SELECT FOR UPDATE (защита от двойного запуска)
        bot_config = config_watcher.get_config()
        ticker     = bot_config.ticker

        # Обновляем тикер в эмиттере — все последующие события будут с правильным тикером
        emitter.set_ticker(ticker)

        logger.info(
            "Конфиг загружен: ticker=%s, strategy=%s, status=%s, version=%d",
            ticker, bot_config.strategy_name, bot_config.status, bot_config.config_version,
        )
    except Exception as exc:
        logger.critical("Не удалось загрузить конфиг бота: %s", exc)
        emitter.emit(
            event_type="CONFIG_VALIDATION_FAILED",
            level="CRITICAL",
            message=f"Ошибка загрузки bot_config: {exc}",
            payload={"user_id": user_id, "bot_id": bot_id, "error": str(exc)},
        )
        sys.exit(1)

    # Проверяем статус — если STOPPED, не стартуем
    if bot_config.status == "STOPPED":
        logger.info("bot_configs.status=STOPPED — бот не запускается")
        sys.exit(0)

    # ------------------------------------------------------------------
    # 7. Брокер (П4)
    # ------------------------------------------------------------------
    try:
        from broker.broker_factory import BrokerFactory
        bundle = BrokerFactory.create(
            settings=settings,
            key_manager=km,
            emitter=emitter,
            trade_repo=None,
            bot_id=bot_id,
        )
        broker = bundle.broker
        tracker = bundle.tracker
        bundle.start()
        logger.info("Брокер инициализирован: %s", settings.broker.broker_type)
    except Exception as exc:
        logger.critical("Не удалось инициализировать брокер: %s", exc)
        emitter.emit(
            event_type="CREDENTIALS_MISSING",
            level="CRITICAL",
            message=f"Ошибка инициализации брокера: {exc}",
            payload={"broker_type": settings.broker.broker_type, "error": str(exc)},
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # 8. Рыночные данные (П2)
    # ------------------------------------------------------------------
    try:
        from market_data import ProviderFactory  # noqa: PLC0415
        market = ProviderFactory.create(settings.market)
        market.start()
        market.subscribe(ticker)
        logger.info("MarketDataProvider запущен: ticker=%s", ticker)
    except Exception as exc:
        logger.critical("Не удалось запустить MarketDataProvider: %s", exc)
        emitter.emit(
            event_type="MARKET_WATCHER_ERROR",
            level="CRITICAL",
            message=f"Ошибка запуска рыночных данных: {exc}",
            payload={"ticker": ticker, "error": str(exc)},
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # 9. State (Рџ6): registry, repository, manager
    # ------------------------------------------------------------------
    try:
        from bot_state import (  # noqa: PLC0415
            StateRepository,
            StateManager,
            RegistryRepository,
            StateRecovery,
        )
        state_repo    = StateRepository(db_pool)
        state_manager = StateManager(db_pool, emitter=emitter)
        registry_repo = RegistryRepository(db_pool)
    except Exception as exc:
        logger.critical("Не удалось инициализировать State-подсистему: %s", exc)
        sys.exit(1)

    # ------------------------------------------------------------------
    # 10. Проверка bot_registry + StateRecovery.reconcile() (П6)
    # ------------------------------------------------------------------
    emitter.emit(
        event_type="BOT_STARTING",
        level="INFO",
        message=f"Запуск бота: {bot_id}",
        payload={"user_id": user_id, "bot_id": bot_id, "ticker": ticker},
    )

    try:
        # StateRecovery выполняет:
        #   - проверку bot_registry (защита от двойного запуска)
        #   - восстановление bot_state из PostgreSQL
        #   - reconciliation с биржей (активные ордера, fills)
        #   - 8-шаговый Startup/Restart Recovery из ТЗ 7
        # После этого вызова бот может торговать.
        StateRecovery.startup(
            user_id=user_id,
            bot_id=bot_id,
            ticker=ticker,
            broker=broker,
            state_repo=state_repo,
            state_manager=state_manager,
            registry_repo=registry_repo,
            emitter=emitter,
            virtual_balance=bot_config.virtual_balance,
            market=market,  # for OHLCV playback on PaperBroker restart
        )

        logger.info("StateRecovery завершён — готов к торговле")
    except RuntimeError as exc:
        # RuntimeError от StateRecovery — например, бот уже запущен
        logger.critical("StateRecovery отказал: %s", exc)
        emitter.emit(
            event_type="BOT_CRASHED",
            level="CRITICAL",
            message=str(exc),
            payload={"user_id": user_id, "bot_id": bot_id},
        )
        sys.exit(1)
    except Exception as exc:
        logger.critical("Ошибка при восстановлении состояния: %s", exc)
        emitter.emit(
            event_type="BOT_CRASHED",
            level="CRITICAL",
            message=f"StateRecovery failed: {exc}",
            payload={"user_id": user_id, "bot_id": bot_id, "error": str(exc)},
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # 11. Стратегия
    # ------------------------------------------------------------------
    try:
        from business_logic import create_strategy  # noqa: PLC0415
        strategy = create_strategy(bot_config.strategy_name)
        logger.info("Стратегия: %s", strategy.name())
    except Exception as exc:
        logger.critical("Не удалось создать стратегию '%s': %s", bot_config.strategy_name, exc)
        sys.exit(1)

    # ------------------------------------------------------------------
    # 12. BotLoop
    # ------------------------------------------------------------------
    from business_logic import BotLoop  # noqa: PLC0415

    bot = BotLoop(
        market=market,
        broker=broker,
        state_manager=state_manager,
        state_repo=state_repo,
        registry_repo=registry_repo,
        config_watcher=config_watcher,
        strategy=strategy,
        emitter=emitter,
        settings=bot_loop_settings,
        bot_id=bot_id,
        user_id=user_id,
    )

    # ------------------------------------------------------------------
    # 13. Запуск — блокирует поток до остановки
    # ------------------------------------------------------------------
    try:
        bot.run()
    except KeyboardInterrupt:
        logger.info("Получен Ctrl+C — корректная остановка")
        emitter.emit(
            event_type="BOT_STOPPED",
            level="INFO",
            message="Остановлен оператором (Ctrl+C)",
            payload={"bot_id": bot_id},
        )
    except Exception as exc:
        logger.critical("Необработанное исключение в BotLoop: %s", exc, exc_info=True)
        emitter.emit(
            event_type="BOT_CRASHED",
            level="CRITICAL",
            message=f"Необработанное исключение: {exc}",
            payload={"error": str(exc), "bot_id": bot_id},
        )
        sys.exit(1)
    finally:
        # Корректное завершение подсистем
        try:
            registry_repo.update_status(user_id, bot_id, operational_status="STOPPED")
        except Exception:
            pass
        try:
            market.stop()
        except Exception:
            pass
        try:
            db_pool.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()