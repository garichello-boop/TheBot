"""
broker/broker_factory.py — создание брокера по конфигу.

BrokerFactory.create() читает BROKER_TYPE из AppSettings и возвращает
BrokerBundle — пару (broker, tracker). BotLoop работает с BrokerBundle,
не зная о конкретных типах брокера.

Поддерживаемые значения BROKER_TYPE:
  paper          — PaperBroker (симуляция, без сети)
  bybit          — BybitBroker, прод-биржа (BYBIT_API_KEY / BYBIT_API_SECRET)
  bybit_testnet  — BybitBroker, тестовая среда Bybit
                   (BYBIT_TESTNET_API_KEY / BYBIT_TESTNET_API_SECRET)

Использование в точке входа бота:

    settings = AppSettings()
    km = KeyManager(); km.load(master_password)
    emitter = setup_observability(...)
    trade_repo = TradeRepository(db_pool)

    bundle = BrokerFactory.create(
        settings=settings,
        key_manager=km,
        emitter=emitter,
        trade_repo=trade_repo,
        bot_id="btc_mean_rev",
    )
    bundle.start()   # запускает WS-трекер если Bybit / Bybit Testnet
    try:
        bot_loop.run(bundle.broker, bundle.tracker)
    finally:
        bundle.stop()
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from broker.broker import IBroker
from broker.bybit_broker import BybitBroker
from broker.order_tracker import BybitOrderTracker, OrderTracker
from broker.paper_broker import PaperBroker

logger = logging.getLogger(__name__)

_BROKER_PAPER          = "paper"
_BROKER_BYBIT          = "bybit"
_BROKER_BYBIT_TESTNET  = "bybit_testnet"

_VALID_BROKER_TYPES = f"{_BROKER_PAPER}, {_BROKER_BYBIT}, {_BROKER_BYBIT_TESTNET}"


@dataclass
class BrokerBundle:
    """
    Пара (broker, tracker) — единый объект жизненного цикла для BotLoop.

    broker:  IBroker — все торговые операции (REST)
    tracker: Optional[OrderTracker] — WS-уведомления об исполнении ордеров.
             None для PaperBroker (fills приходят через process_market_tick).

    BotLoop в начале каждого тика:

        if bundle.tracker:                              # LIVE / TESTNET режим
            fills = bundle.tracker.pop_recent_fills()
        elif isinstance(bundle.broker, PaperBroker):   # PAPER режим
            fills = bundle.broker.process_market_tick(bid, ask)
    """
    broker: IBroker
    tracker: Optional[OrderTracker]

    def start(self) -> None:
        """
        Запустить подсистемы. Вызывается один раз при старте бота,
        до начала tick-loop.
        Для Bybit / Bybit Testnet: подключает BybitOrderTracker к приватному WS.
        Для Paper: ничего не делает (PaperBroker готов после __init__).
        """
        if self.tracker is not None:
            self.tracker.start()
            logger.info("BrokerBundle: OrderTracker запущен")
        logger.info("BrokerBundle: режим=%s", self.broker.get_mode().value)

    def stop(self) -> None:
        """
        Остановить подсистемы. Вызывается в finally-блоке BotLoop.
        Для Bybit / Bybit Testnet: закрывает WS-соединение трекера.
        """
        if self.tracker is not None:
            self.tracker.stop()
            logger.info("BrokerBundle: OrderTracker остановлен")


class BrokerFactory:
    """
    Статическая фабрика. Создаёт BrokerBundle по BROKER_TYPE.

    Единственное место в системе где известны конкретные типы брокеров.
    BotLoop знает только IBroker + проверяет isinstance(broker, PaperBroker)
    для вызова process_market_tick — и больше нигде.
    """

    @staticmethod
    def create(
        settings,     # config.settings.AppSettings
        key_manager,  # keys.key_manager.KeyManager
        emitter,      # observability.emitter.EventEmitter
        trade_repo,   # observability.trade_repository.TradeRepository
        bot_id: str,
        **kwargs,
    ) -> BrokerBundle:
        """
        Создать BrokerBundle по BROKER_TYPE из settings.broker.

        Args:
            settings:    AppSettings с секцией broker (BrokerSettings).
            key_manager: KeyManager для API-ключей Bybit.
            emitter:     EventEmitter для событий ORDER_FILLED и др.
            trade_repo:  TradeRepository для записи сделок (PaperBroker).
            bot_id:      ID бота для маркировки событий и сделок.

        Raises:
            ValueError:  неизвестный BROKER_TYPE.
            ImportError: pybit не установлен при BROKER_TYPE=bybit / bybit_testnet.
        """
        broker_type = settings.broker.broker_type.value.lower()

        if broker_type == _BROKER_PAPER:
            return BrokerFactory._create_paper(settings, emitter, trade_repo, bot_id)

        if broker_type == _BROKER_BYBIT:
            return BrokerFactory._create_bybit(
                settings, key_manager, emitter, testnet=False
            )

        if broker_type == _BROKER_BYBIT_TESTNET:
            return BrokerFactory._create_bybit(
                settings, key_manager, emitter, testnet=True
            )

        raise ValueError(
            f"BrokerFactory: неизвестный BROKER_TYPE='{broker_type}'. "
            f"Допустимые значения: {_VALID_BROKER_TYPES}"
        )

    @staticmethod
    def _create_paper(settings, emitter, trade_repo, bot_id: str) -> BrokerBundle:
        bs = settings.broker

        # PAPER_COMMISSION_PCT / PAPER_SLIPPAGE_PCT хранятся как проценты
        # (0.1 = 0.1%). PaperBroker ожидает десятичный множитель (0.001).
        commission_pct  = Decimal(str(bs.paper_commission_pct)) / Decimal("100")
        slippage_pct    = Decimal(str(bs.paper_slippage_pct))   / Decimal("100")
        initial_balance = Decimal(str(bs.paper_initial_balance))

        broker = PaperBroker(
            initial_balance=initial_balance,
            commission_pct=commission_pct,
            slippage_pct=slippage_pct,
            emitter=emitter,
            trade_repo=trade_repo,
            bot_id=bot_id,
        )

        logger.info(
            "BrokerFactory: PaperBroker | баланс=%.2f USDT | "
            "комиссия=%.4f%% | slippage=%.4f%%",
            float(initial_balance),
            float(commission_pct * 100),
            float(slippage_pct * 100),
        )
        return BrokerBundle(broker=broker, tracker=None)

    @staticmethod
    def _create_bybit(
        settings,
        key_manager,
        emitter,
        testnet: bool,
    ) -> BrokerBundle:
        """
        Создать BybitBroker + BybitOrderTracker.

        testnet=False → прод-биржа, ключи BYBIT_API_KEY / BYBIT_API_SECRET.
        testnet=True  → testnet.bybit.com, ключи BYBIT_TESTNET_API_KEY /
                        BYBIT_TESTNET_API_SECRET.

        Ключи для live и testnet разделены намеренно — исключает случайное
        использование прод-ключей на тестовой среде и наоборот.
        """
        bs = settings.broker

        if testnet:
            api_key    = key_manager.get("BYBIT_TESTNET_API_KEY")
            api_secret = key_manager.get("BYBIT_TESTNET_API_SECRET")
        else:
            api_key    = key_manager.get("BYBIT_API_KEY")
            api_secret = key_manager.get("BYBIT_API_SECRET")

        tracker = BybitOrderTracker(
            api_key=api_key,
            api_secret=api_secret,
            emitter=emitter,
            testnet=testnet,
        )

        broker = BybitBroker(
            api_key=api_key,
            api_secret=api_secret,
            emitter=emitter,
            testnet=testnet,
            request_timeout_sec=float(bs.request_timeout_sec),
            retry_delay_sec=float(bs.retry_delay_sec),
            max_retries=int(bs.max_retries),
        )

        logger.info(
            "BrokerFactory: BybitBroker | testnet=%s | "
            "timeout=%.1fs | max_retries=%d",
            testnet,
            float(bs.request_timeout_sec),
            int(bs.max_retries),
        )
        return BrokerBundle(broker=broker, tracker=tracker)
