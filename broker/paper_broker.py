"""
broker/paper_broker.py — PaperBroker: бумажная торговля.

Симулирует реальную биржу без реальных денег. Специально настроен
пессимистичнее реальности — чтобы реальные результаты были не хуже Paper.

Цены исполнения:
    BUY  MARKET → ask + slippage   (платим больше рыночного — пессимистично)
    SELL MARKET → bid - slippage   (получаем меньше — пессимистично)
    BUY  LIMIT  → limit_price      (pending до достижения цены)
    SELL LIMIT  → limit_price      (pending до достижения цены)

Жизненный цикл ордера в двух тиках:

    Тик N:
        1. process_market_tick(bid, ask) → обновить цену, заполнить LIMIT
        2. DecisionEngine решает BUY → create_order(MARKET BUY)
           → PaperBroker: исполнить немедленно, сохранить fill во внутренней очереди
           → вернуть OrderCreated(PENDING)  # per IBroker контракт
        3. commit + emit (без знания о fill — как на реальной бирже)

    Тик N+1:
        1. process_market_tick(bid, ask) → вернуть накопленные fills (включая fill из тика N)
        2. TickContext видит fill → FSM переход ENTERING → IN_POSITION

    Это поведение идентично реальной бирже где WS-подтверждение приходит
    асинхронно после выставления ордера. Bot Loop не требует изменений.

Интеграция в tick-loop (BotLoop):

    # В начале каждого тика, до сборки TickContext:
    if isinstance(broker, PaperBroker):
        recent_fills = broker.process_market_tick(price_data.bid, price_data.ask)
    else:
        recent_fills = order_tracker.pop_recent_fills()
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from broker.broker import (
    BrokerError,
    BrokerRejected,
    IBroker,
    InsufficientFundsError,
    OrderNotFoundError,
)
from broker.models import (
    Balance,
    BrokerMode,
    MarketInfo,
    OpenOrder,
    OrderCreated,
    OrderFill,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)

logger = logging.getLogger(__name__)

# (OrderRequest, locked_usdt) — locked_usdt > 0 только для BUY LIMIT
_PendingEntry = Tuple[OrderRequest, Decimal]


class PaperBroker(IBroker):
    """
    Бумажный брокер. Дефолтный режим при старте бота (BROKER_TYPE=paper).

    Создаётся через BrokerFactory. После 2-4 недель успешной бумажной
    торговли переключить на BybitBroker (BROKER_TYPE=bybit).

    Зависимости передаются через конструктор:
        emitter    — EventEmitter из observability (для ORDER_FILLED)
        trade_repo — TradeRepository из observability (для записи в БД)
    """

    def __init__(
        self,
        initial_balance: Decimal,
        commission_pct: Decimal,
        slippage_pct: Decimal,
        emitter,       # observability.EventEmitter
        trade_repo,    # observability.TradeRepository
        bot_id: str,
    ) -> None:
        self._commission_pct = commission_pct
        self._slippage_pct = slippage_pct
        self._emitter = emitter
        self._trade_repo = trade_repo
        self._bot_id = bot_id

        # Виртуальный баланс в USDT
        self._free_usdt: Decimal = initial_balance
        self._locked_usdt: Decimal = Decimal("0")

        # Pending LIMIT ордера: exchange_order_id → (request, locked_usdt)
        self._pending: Dict[str, _PendingEntry] = {}

        # Очередь fills ожидающих забора через process_market_tick()
        # Сюда попадают: MARKET fills из текущих тиков + LIMIT fills при срабатывании
        self._fill_queue: List[OrderFill] = []

        # Текущая цена (обновляется через process_market_tick)
        self._last_bid: Optional[Decimal] = None
        self._last_ask: Optional[Decimal] = None

        # Кэш market info (задаётся через set_market_info)
        self._market_info_cache: Dict[str, MarketInfo] = {}

        logger.info(
            "PaperBroker запущен | баланс=%.2f USDT | комиссия=%.4f%% | slippage=%.4f%%",
            initial_balance,
            float(commission_pct * 100),
            float(slippage_pct * 100),
        )

    # ------------------------------------------------------------------
    # IBroker — публичный интерфейс
    # ------------------------------------------------------------------

    def create_order(self, order: OrderRequest) -> OrderCreated:
        """
        Создать ордер.

        MARKET → исполняется немедленно внутри этого вызова.
                 Fill сохраняется в очереди и возвращается на следующем
                 вызове process_market_tick(). Эмитируется ORDER_FILLED.

        LIMIT  → сохраняется как pending. Исполнится в process_market_tick()
                 когда цена достигнет уровня ордера.
        """
        if order.order_type == OrderType.MARKET:
            return self._fill_market_order(order)
        return self._place_limit_order(order)

    def cancel_order(self, order_id: str) -> bool:
        """
        Отменить ордер.

        Если ордер в pending — удаляет и разблокирует USDT.
        Если ордера нет (уже исполнен или не существует) — возвращает True
        (идемпотентно, как реальная биржа).
        """
        if order_id not in self._pending:
            logger.debug(
                "PaperBroker cancel_order: %s не найден в pending (idempotent ok)",
                order_id,
            )
            return True

        request, locked = self._pending.pop(order_id)
        if locked > Decimal("0"):
            self._unlock_usdt(locked)

        logger.info(
            "PaperBroker: отменён %s %s (order_id=%s, разблокировано=%.4f USDT)",
            request.side.value, request.ticker, order_id, float(locked),
        )
        return True

    def get_order_status(self, order_id: str) -> OrderStatus:
        """
        Статус ордера. Используется только при reconciliation на старте.
        В рабочем режиме fills приходят через process_market_tick().
        """
        if order_id in self._pending:
            return OrderStatus.PENDING
        raise OrderNotFoundError(
            f"PaperBroker: ордер {order_id} не найден. "
            f"Возможно уже исполнен или отменён."
        )

    def get_balance(self) -> Balance:
        """Текущий виртуальный баланс бота в USDT."""
        return Balance(free=self._free_usdt, locked=self._locked_usdt)

    def get_market_info(self, ticker: str) -> MarketInfo:
        """
        Торговые ограничения инструмента.

        Приоритет: set_market_info() → дефолт для крипто.
        Для точной симуляции задать через set_market_info() при старте бота.
        """
        if ticker in self._market_info_cache:
            return self._market_info_cache[ticker]

        logger.warning(
            "PaperBroker: MarketInfo для %s не задан — используем дефолт. "
            "Вызовите set_market_info() при старте для точной симуляции.",
            ticker,
        )
        return MarketInfo(
            ticker=ticker,
            min_qty=Decimal("0.001"),
            step_size=Decimal("0.001"),
            min_notional=Decimal("5"),
            price_precision=2,
            tick_size=Decimal("0.01"),
        )

    def get_open_orders(self, ticker: Optional[str] = None) -> List[OpenOrder]:
        """Список активных (pending) ордеров. Используется при reconciliation."""
        result = []
        for order_id, (request, _locked) in self._pending.items():
            if ticker is not None and request.ticker != ticker:
                continue
            result.append(OpenOrder(
                exchange_order_id=order_id,
                client_order_id=request.client_order_id,
                ticker=request.ticker,
                side=request.side,
                order_type=request.order_type,
                quantity=request.quantity,
                filled_qty=Decimal("0"),
                price=request.price,
                status=OrderStatus.PENDING,
                mode=BrokerMode.PAPER,
            ))
        return result

    def get_mode(self) -> BrokerMode:
        return BrokerMode.PAPER

    # ------------------------------------------------------------------
    # Paper-specific API — вызывается из BotLoop
    # ------------------------------------------------------------------

    def process_market_tick(self, bid: Decimal, ask: Decimal) -> List[OrderFill]:
        """
        Симулирует получение рыночных данных и WS-апдейтов от биржи.

        Выполняет две задачи:
        1. Обновляет текущую цену (bid/ask) для MARKET-ордеров
        2. Проверяет pending LIMIT ордера: исполняет те что достигли уровня

        Возвращает все накопленные fills с прошлого вызова:
        — LIMIT fills которые сработали на этом тике
        — MARKET fills из ордеров размещённых на предыдущем тике

        Вызывается из BotLoop в начале каждого тика ДО сборки TickContext:

            if isinstance(broker, PaperBroker):
                recent_fills = broker.process_market_tick(
                    price_data.bid, price_data.ask
                )
        """
        self._last_bid = bid
        self._last_ask = ask

        # Найти LIMIT ордера которые исполнились на этой цене
        triggered: List[Tuple[str, OrderRequest, Decimal, Decimal]] = []
        for order_id, (request, locked) in self._pending.items():
            fill_price = self._check_limit_trigger(request, bid, ask)
            if fill_price is not None:
                triggered.append((order_id, request, locked, fill_price))

        # Исполнить найденные (отдельным проходом чтобы не мутировать dict в итерации)
        for order_id, request, locked, fill_price in triggered:
            del self._pending[order_id]
            self._execute_fill(
                order_id=order_id,
                request=request,
                execution_price=fill_price,
                locked_to_release=locked,
            )

        # Вернуть и очистить очередь fills
        fills = list(self._fill_queue)
        self._fill_queue.clear()
        return fills

    def set_market_info(self, info: MarketInfo) -> None:
        """
        Задать торговые ограничения инструмента.
        Вызывать при старте бота после получения реальных ограничений с биржи:

            real_info = bybit_http.get_market_info("BTCUSDT")
            paper_broker.set_market_info(real_info)
        """
        self._market_info_cache[info.ticker] = info
        logger.debug(
            "PaperBroker: MarketInfo задан для %s "
            "(min_qty=%s, step_size=%s, min_notional=%s)",
            info.ticker, info.min_qty, info.step_size, info.min_notional,
        )

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _fill_market_order(self, order: OrderRequest) -> OrderCreated:
        """Исполнить MARKET ордер немедленно по ask/bid ± slippage."""
        if self._last_ask is None or self._last_bid is None:
            raise BrokerError(
                "PaperBroker: нет текущей цены для MARKET ордера. "
                "process_market_tick() должен быть вызван до create_order()."
            )

        execution_price = self._market_execution_price(order.side)
        order_id = f"paper_{uuid4().hex[:12]}"

        if order.side == OrderSide.BUY:
            total_cost = order.quantity * execution_price * (1 + self._commission_pct)
            if total_cost > self._free_usdt:
                raise InsufficientFundsError(
                    f"PaperBroker: недостаточно USDT для BUY MARKET. "
                    f"Нужно {float(total_cost):.4f}, доступно {float(self._free_usdt):.4f}"
                )

        self._execute_fill(
            order_id=order_id,
            request=order,
            execution_price=execution_price,
            locked_to_release=Decimal("0"),
        )

        return OrderCreated(
            exchange_order_id=order_id,
            client_order_id=order.client_order_id,
            status=OrderStatus.PENDING,  # IBroker контракт: всегда PENDING
            mode=BrokerMode.PAPER,
        )

    def _place_limit_order(self, order: OrderRequest) -> OrderCreated:
        """Поставить LIMIT ордер в очередь pending."""
        if order.price is None:
            raise BrokerRejected(
                f"PaperBroker: LIMIT ордер {order.client_order_id} без цены"
            )

        order_id = f"paper_{uuid4().hex[:12]}"
        lock_amount = Decimal("0")

        if order.side == OrderSide.BUY:
            # Блокируем максимальную стоимость включая комиссию
            lock_amount = order.quantity * order.price * (1 + self._commission_pct)
            if lock_amount > self._free_usdt:
                raise InsufficientFundsError(
                    f"PaperBroker: недостаточно USDT для BUY LIMIT. "
                    f"Нужно {float(lock_amount):.4f}, доступно {float(self._free_usdt):.4f}"
                )
            self._lock_usdt(lock_amount)

        self._pending[order_id] = (order, lock_amount)

        logger.debug(
            "PaperBroker LIMIT pending: %s %s qty=%s @ %s "
            "(order_id=%s, locked=%.4f USDT)",
            order.side.value, order.ticker,
            order.quantity, order.price,
            order_id, float(lock_amount),
        )

        return OrderCreated(
            exchange_order_id=order_id,
            client_order_id=order.client_order_id,
            status=OrderStatus.PENDING,
            mode=BrokerMode.PAPER,
        )

    def _check_limit_trigger(
        self,
        request: OrderRequest,
        bid: Decimal,
        ask: Decimal,
    ) -> Optional[Decimal]:
        """
        Проверить достигнут ли уровень LIMIT ордера при текущих ценах.

        BUY  LIMIT: исполняется если ask <= limit_price
                    (продавцы опустились до нашей цены покупки)

        SELL LIMIT: исполняется если bid >= limit_price
                    (покупатели поднялись до нашей цены продажи)

        Возвращает цену исполнения (= limit_price) или None.
        """
        if request.price is None:
            return None

        if request.side == OrderSide.BUY and ask <= request.price:
            return request.price
        if request.side == OrderSide.SELL and bid >= request.price:
            return request.price

        return None

    def _execute_fill(
        self,
        order_id: str,
        request: OrderRequest,
        execution_price: Decimal,
        locked_to_release: Decimal,
    ) -> None:
        """
        Применить исполнение ордера:
        1. Обновить виртуальный баланс
        2. Записать сделку через TradeRepository
        3. Эмитировать ORDER_FILLED
        4. Добавить OrderFill в очередь для TickContext
        """
        commission = request.quantity * execution_price * self._commission_pct
        ts = time.time()

        # --- Обновить баланс ---
        if locked_to_release > Decimal("0"):
            self._unlock_usdt(locked_to_release)

        if request.side == OrderSide.BUY:
            total_cost = request.quantity * execution_price + commission
            self._free_usdt -= total_cost
        else:  # SELL
            proceeds = request.quantity * execution_price - commission
            self._free_usdt += proceeds

        # --- Сформировать fill ---
        fill = OrderFill(
            exchange_order_id=order_id,
            client_order_id=request.client_order_id,
            ticker=request.ticker,
            side=request.side,
            filled_qty=request.quantity,
            avg_price=execution_price,
            commission=commission,
            mode=BrokerMode.PAPER,
            timestamp=ts,
            is_partial=False,
        )

        # --- Сохранить в БД ---
        try:
            self._trade_repo.save_fill(fill, bot_id=self._bot_id)
        except Exception as exc:
            # Ошибка записи не останавливает торговлю — событие STATE_SAVE_FAILED
            # будет обработано самим trade_repo или выше по стеку
            logger.error(
                "PaperBroker: ошибка сохранения fill в БД (order_id=%s): %s",
                order_id, exc,
            )

        # --- Эмитировать ORDER_FILLED ---
        self._emitter.emit(
            event_type="ORDER_FILLED",
            level="INFO",
            message=(
                f"[PAPER] {request.side.value} {request.quantity} {request.ticker} "
                f"@ {execution_price:.4f} | комиссия={commission:.4f}"
            ),
            payload={
                "exchange_order_id": order_id,
                "client_order_id": request.client_order_id,
                "side": request.side.value,
                "filled_qty": str(request.quantity),
                "avg_price": str(execution_price),
                "commission": str(commission),
                "mode": "PAPER",
                "cycle_id": request.cycle_id,
                "bot_id": request.bot_id,
            },
        )

        # --- Добавить в очередь для TickContext ---
        self._fill_queue.append(fill)

        logger.info(
            "PaperBroker fill: %s %s qty=%s @ %s | комиссия=%s | "
            "баланс=%.4f free / %.4f locked",
            request.side.value, request.ticker,
            request.quantity, execution_price, commission,
            float(self._free_usdt), float(self._locked_usdt),
        )

    def _market_execution_price(self, side: OrderSide) -> Decimal:
        """
        Цена исполнения MARKET ордера с пессимистичным slippage.

        BUY:  ask * (1 + slippage_pct) — платим выше рынка
        SELL: bid * (1 - slippage_pct) — получаем ниже рынка

        Пессимизм намеренный: реальные результаты должны быть не хуже Paper.
        """
        if side == OrderSide.BUY:
            return self._last_ask * (1 + self._slippage_pct)
        return self._last_bid * (1 - self._slippage_pct)

    def _lock_usdt(self, amount: Decimal) -> None:
        """Перевести amount из free в locked."""
        self._free_usdt -= amount
        self._locked_usdt += amount

    def _unlock_usdt(self, amount: Decimal) -> None:
        """Вернуть amount из locked в free."""
        self._locked_usdt -= amount
        self._free_usdt += amount
