"""
TickContext — неизменяемый снапшот всех данных на начало тика.

Правила:
  - Собирается один раз в начале тика методом collect().
  - DecisionEngine работает только с этим объектом — без дополнительных
    запросов к бирже или БД внутри тика.
  - Все внешние вызовы (market, broker, state_repo) — только в collect().
  - Frozen dataclass: поля не изменяются после создания.

Зависимости (интерфейсы из П2 / П4 / П5 / П6):
  MarketDataProvider.get_price(ticker) → PriceData | raises MarketDataUnavailable
  IBroker.get_open_orders()            → list[dict]
  IBroker.get_pending_fills()          → list[FillEvent]
  IBroker.get_balance()                → Balance
  StateRepository.load(user_id,bot_id) → BotState | None
  ConfigWatcher.get_config()           → BotConfig
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .errors import CriticalError, RecoverableError
from .types import FillEvent

if TYPE_CHECKING:
    from broker import IBroker, Balance
    from market_data import MarketDataProvider, PriceData
    from bot_state import BotState, StateRepository
    from bot_config import BotConfig, ConfigWatcher


@dataclass(frozen=True)
class TickContext:
    """
    Снапшот всех данных на начало тика.

    Поля:
      price_data      — текущая цена (bid/ask/last) от MarketDataProvider.
      open_orders     — активные ордера на бирже (для reconciliation).
      order_events    — события исполнения / отмены с последнего тика.
                        Дренируются из внутренней очереди брокера.
      balance         — баланс (free/locked/total) от брокера.
      bot_state       — актуальное состояние из PostgreSQL.
      bot_config      — актуальный конфиг бота из ConfigWatcher.
      tick_start_mono — time.monotonic() в момент начала сбора. Используется
                        для Watchdog и sleep_until.
      tick_number     — монотонный счётчик тиков с момента старта бота.
    """

    price_data:       "PriceData"
    open_orders:      tuple[dict, ...]
    order_events:     tuple[FillEvent, ...]
    balance:          "Balance"
    bot_state:        "BotState"
    bot_config:       "BotConfig"
    tick_start_mono:  float
    tick_number:      int

    # ------------------------------------------------------------------
    # Удобные свойства для частых проверок
    # ------------------------------------------------------------------

    @property
    def cycle_status(self) -> str:
        return self.bot_state.cycle_status

    @property
    def bot_status(self) -> str:
        """Статус из bot_configs: ACTIVE / CLOSE_ONLY / STOPPED / FORCE_CLOSE."""
        return self.bot_config.status

    @property
    def ticker(self) -> str:
        return self.bot_config.ticker

    @property
    def user_id(self) -> str:
        return self.bot_config.user_id

    @property
    def bot_id(self) -> str:
        return self.bot_config.bot_id

    @property
    def has_open_position(self) -> bool:
        from decimal import Decimal
        return self.bot_state.position_qty > Decimal(0)

    @property
    def has_active_entry_order(self) -> bool:
        return self.bot_state.active_entry_order_id is not None

    @property
    def has_active_tp_order(self) -> bool:
        return self.bot_state.active_tp_order_id is not None

    @property
    def fills_for_entry(self) -> tuple[FillEvent, ...]:
        """События только по ордеру на вход."""
        from .types import OrderType
        oid = self.bot_state.active_entry_order_id
        return tuple(
            e for e in self.order_events
            if e.order_type == OrderType.ENTRY
            and (oid is None or e.exchange_order_id == oid)
        )

    @property
    def fills_for_tp(self) -> tuple[FillEvent, ...]:
        """События только по TP-ордеру."""
        from .types import OrderType
        oid = self.bot_state.active_tp_order_id
        return tuple(
            e for e in self.order_events
            if e.order_type == OrderType.TP
            and (oid is None or e.exchange_order_id == oid)
        )

    @property
    def fills_for_dca(self) -> tuple[FillEvent, ...]:
        """События по любому из активных DCA-ордеров."""
        from .types import OrderType
        dca_ids = set(self.bot_state.active_dca_order_ids)
        return tuple(
            e for e in self.order_events
            if e.order_type == OrderType.DCA
            and (not dca_ids or e.exchange_order_id in dca_ids)
        )

    # ------------------------------------------------------------------
    # Фабричный метод
    # ------------------------------------------------------------------

    @classmethod
    def collect(
        cls,
        market: "MarketDataProvider",
        broker: "IBroker",
        state_repo: "StateRepository",
        config_watcher: "ConfigWatcher",
        tick_number: int,
    ) -> "TickContext":
        """
        Собирает снапшот. Единственное место с внешними вызовами в тике.

        Порядок вызовов фиксирован — менять нельзя:
          1. tick_start_mono — до всего, чтобы Watchdog мерил полный тик.
          2. bot_config      — нужен ticker для price_data и state.
          3. price_data      — если MarketDataUnavailable → RecoverableError.
          3.5 process_market_tick — передать свежую цену в PaperBroker ДО
                               дренажа fills. Без этого LIMIT ордера не
                               исполняются: _check_and_execute_limits()
                               работает с устаревшим _last_bid/_last_ask.
                               hasattr-проверка: BybitBroker метод не имеет,
                               вызов безопасен для любого брокера.
          4. open_orders     — для reconciliation.
          5. order_events    — дренируем очередь до balance (атомарность).
          6. balance         — текущий баланс.
          7. bot_state       — последнее persisted состояние из БД.

        При любой ошибке загрузки state → CriticalError (бот не может
        торговать без знания своего состояния).
        """
        tick_start_mono = time.monotonic()

        # --- 2. Config ---------------------------------------------------
        try:
            bot_config = config_watcher.get_config()
        except Exception as exc:
            raise CriticalError(
                f"Не удалось загрузить конфиг бота: {exc}"
            ) from exc

        # --- 3. Price ----------------------------------------------------
        # MarketDataUnavailable — подкласс Exception, не RecoverableError.
        # Оборачиваем, чтобы BotLoop правильно обработал.
        try:
            price_data = market.get_price(bot_config.ticker)
        except Exception as exc:
            raise RecoverableError(
                f"Рыночные данные недоступны: {exc}"
            ) from exc

        # --- 3.5. Обновить PaperBroker актуальной ценой -----------------
        # Необходимо чтобы LIMIT ордера проверялись против свежей цены
        # перед дренажом fills на шаге 5.
        # BybitBroker этот метод не имеет — hasattr делает вызов безопасным.
        if hasattr(broker, 'process_market_tick'):
            broker.process_market_tick(price_data.bid, price_data.ask)

        # --- 4. Open orders ----------------------------------------------
        try:
            open_orders = tuple(broker.get_open_orders())
        except Exception as exc:
            raise RecoverableError(
                f"Не удалось получить открытые ордера: {exc}"
            ) from exc

        # --- 5. Pending fills (дренаж очереди) --------------------------
        try:
            order_events = tuple(broker.get_pending_fills())
        except Exception as exc:
            raise RecoverableError(
                f"Не удалось получить события ордеров: {exc}"
            ) from exc

        # --- 6. Balance --------------------------------------------------
        try:
            balance = broker.get_balance()
        except Exception as exc:
            raise RecoverableError(
                f"Не удалось получить баланс: {exc}"
            ) from exc

        # --- 7. State from DB -------------------------------------------
        try:
            bot_state = state_repo.load(bot_config.user_id, bot_config.bot_id)
        except Exception as exc:
            raise CriticalError(
                f"Не удалось загрузить bot_state из БД: {exc}"
            ) from exc

        if bot_state is None:
            raise CriticalError(
                f"bot_state не найден в БД для "
                f"{bot_config.user_id}/{bot_config.bot_id}. "
                f"Запустите инициализацию перед стартом бота."
            )

        return cls(
            price_data=price_data,
            open_orders=open_orders,
            order_events=order_events,
            balance=balance,
            bot_state=bot_state,
            bot_config=bot_config,
            tick_start_mono=tick_start_mono,
            tick_number=tick_number,
        )
