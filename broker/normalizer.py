"""
broker/normalizer.py — нормализация ордеров перед отправкой на биржу.

Strict Mode — фундаментальное правило:
нормализатор НИКОГДА не увеличивает риск сверх расчётного.
Если ордер нельзя исполнить точно в соответствии с расчётами стратегии —
он пропускается (SKIP). Не раздувается до минимума, не урезается до остатка
баланса, не корректируется «на авось».

Типичный вызов из DecisionEngine:

    info = broker.get_market_info(order.ticker)
    balance = broker.get_balance()

    result = OrderNormalizer.normalize(
        order=raw_order,
        info=info,
        free_balance=balance.free,
        estimated_price=price_data.ask,  # для MARKET BUY
    )

    if result.is_skip:
        emitter.emit(event_type=result.skip_event_type, ...)
        return

    created = broker.create_order(result.order)
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from broker.models import (
    MarketInfo,
    NormalizeResult,
    OrderRequest,
    SkipReason,
)

logger = logging.getLogger(__name__)


class OrderNormalizer:
    """
    Статический нормализатор ордеров. Не хранит состояния.

    Последовательность проверок:
    1. Квантование qty ВНИЗ по step_size
    2. qty == 0 после квантования → SKIP QTY_BECAME_ZERO
    3. qty < min_qty → SKIP BELOW_MIN_QTY
    4. Квантование price ВНИЗ по tick_size (только LIMIT)
    5. qty * price < min_notional → SKIP BELOW_MIN_QTY
    6. qty * price > free_balance → SKIP INSUFFICIENT_FUNDS
    7. Всё прошло → NormalizeResult.ok(нормализованный ордер)

    Шаги 5 и 6 выполняются только если известна цена (LIMIT или
    estimated_price для MARKET). Без цены баланс не проверяется —
    ответственность за MARKET-баланс лежит на стратегии.
    """

    @staticmethod
    def normalize(
        order: OrderRequest,
        info: MarketInfo,
        free_balance: Decimal,
        estimated_price: Optional[Decimal] = None,
    ) -> NormalizeResult:
        """
        Нормализовать ордер под ограничения биржи.

        Args:
            order:           Исходный ордер от DecisionEngine (до нормализации).
            info:            Торговые ограничения инструмента от broker.get_market_info().
            free_balance:    Доступный баланс из broker.get_balance().free.
            estimated_price: Оценочная цена для MARKET-ордеров (обычно ask для BUY,
                             bid для SELL). Если не передана — проверки на notional
                             и баланс для MARKET-ордеров пропускаются.

        Returns:
            NormalizeResult.ok(order)   — ордер нормализован, готов к отправке.
            NormalizeResult.skip(...)   — ордер пропускается, причина в skip_reason.
        """
        qty = order.quantity
        price = order.price

        # ------------------------------------------------------------------
        # 1. Квантование объёма ВНИЗ до кратного step_size
        # ------------------------------------------------------------------
        qty = OrderNormalizer._floor_to_step(qty, info.step_size)

        # ------------------------------------------------------------------
        # 2. qty == 0 после квантования
        # ------------------------------------------------------------------
        if qty == Decimal("0"):
            logger.debug(
                "OrderNormalizer SKIP [%s %s]: qty стал 0 после квантования "
                "(исходный=%.8f, step_size=%.8f, ticker=%s)",
                order.side, order.order_type,
                order.quantity, info.step_size, order.ticker,
            )
            return NormalizeResult.skip(SkipReason.QTY_BECAME_ZERO)

        # ------------------------------------------------------------------
        # 3. qty < min_qty
        # Проверяем ПОСЛЕ квантования: стратегия могла рассчитать qty чуть
        # выше min_qty, но после округления вниз оказалось меньше.
        # ------------------------------------------------------------------
        if qty < info.min_qty:
            logger.debug(
                "OrderNormalizer SKIP [%s %s]: qty %.8f < min_qty %.8f (ticker=%s)",
                order.side, order.order_type,
                qty, info.min_qty, order.ticker,
            )
            return NormalizeResult.skip(SkipReason.BELOW_MIN_QTY)

        # ------------------------------------------------------------------
        # 4. Квантование цены ВНИЗ по tick_size (только LIMIT-ордера)
        # Направление: ВНИЗ для обоих сторон — предсказуемое поведение.
        # BUY LIMIT вниз → платим меньше или равно расчёту (безопаснее).
        # SELL LIMIT вниз → получаем чуть меньше, но биржа примет ордер.
        # ------------------------------------------------------------------
        if price is not None:
            price = OrderNormalizer._floor_to_step(price, info.tick_size)

        # ------------------------------------------------------------------
        # Эффективная цена для проверок notional и баланса
        # ------------------------------------------------------------------
        effective_price = price if price is not None else estimated_price

        if effective_price is not None and effective_price > Decimal("0"):
            notional = qty * effective_price

            # --------------------------------------------------------------
            # 5. Проверка min_notional
            # Биржа отклонит ордер если сумма сделки меньше минимума.
            # Strict Mode: не увеличиваем qty чтобы добить до min_notional.
            # Treat как BELOW_MIN_QTY — экономически та же ситуация (слишком мало).
            # --------------------------------------------------------------
            if notional < info.min_notional:
                logger.debug(
                    "OrderNormalizer SKIP [%s %s]: notional %.4f < min_notional %.4f "
                    "(qty=%.8f, price=%.8f, ticker=%s)",
                    order.side, order.order_type,
                    notional, info.min_notional,
                    qty, effective_price, order.ticker,
                )
                return NormalizeResult.skip(SkipReason.BELOW_MIN_QTY)

            # --------------------------------------------------------------
            # 6. Проверка свободного баланса
            # Если не хватает — SKIP INSUFFICIENT_FUNDS.
            # Strict Mode: не урезаем до остатка баланса.
            # Отличие от InsufficientFundsError брокера: тот срабатывает
            # после отправки на биржу по реальному балансу. Этот — до,
            # по виртуальному балансу бота.
            # --------------------------------------------------------------
            if notional > free_balance:
                logger.debug(
                    "OrderNormalizer SKIP [%s %s]: notional %.4f > free_balance %.4f "
                    "(qty=%.8f, price=%.8f, ticker=%s)",
                    order.side, order.order_type,
                    notional, free_balance,
                    qty, effective_price, order.ticker,
                )
                return NormalizeResult.skip(SkipReason.INSUFFICIENT_FUNDS)

        # ------------------------------------------------------------------
        # 7. Все проверки пройдены — создаём нормализованный ордер
        # ------------------------------------------------------------------
        normalized = OrderRequest(
            ticker=order.ticker,
            side=order.side,
            order_type=order.order_type,
            quantity=qty,
            price=price,
            client_order_id=order.client_order_id,
            bot_id=order.bot_id,
            cycle_id=order.cycle_id,
        )

        logger.debug(
            "OrderNormalizer OK [%s %s]: qty %.8f → %.8f, price=%s (ticker=%s)",
            order.side, order.order_type,
            order.quantity, qty,
            price, order.ticker,
        )
        return NormalizeResult.ok(normalized)

    @staticmethod
    def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
        """
        Округлить value ВНИЗ до ближайшего кратного step.

        Использует целочисленное деление Decimal (//) которое всегда
        выполняет floor-division для положительных чисел.

        Примеры:
            _floor_to_step(Decimal("3.7"), Decimal("1.0"))    == Decimal("3.0")
            _floor_to_step(Decimal("0.00375"), Decimal("0.001")) == Decimal("0.003")
            _floor_to_step(Decimal("3200.1234"), Decimal("0.01")) == Decimal("3200.12")
        """
        return (value // step) * step
