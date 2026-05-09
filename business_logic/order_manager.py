"""
OrderManager — жизненный цикл ордеров.

Принципы из ТЗ:
  - Работает только с client_order_id (UUID) внутри бота.
  - Маппинг в поле конкретной биржи (orderLinkId / newClientOrderId)
    делает ExchangeAdapter — OrderManager об этом не знает.
  - pending_client_order_id сохраняется в bot_state ДО отправки на биржу.
    Это позволяет reconciliation найти ордер при рестарте даже если
    бот упал между send и save.
  - Таймаут create_order = BROKER_REQUEST_TIMEOUT_SEC (5 сек).
    При таймауте — немедленно StopCraneError (исход неизвестен, не повторять).
  - Retry при явных сетевых ошибках (не таймаут): экспоненциальная задержка
    с jitter (1→2→4с, max BROKER_MAX_RETRIES).
  - Cancel ордеров — НИКОГДА не StopCraneError. Retry до CANCEL_MAX_RETRIES,
    после → StopCraneError (нельзя перейти в IDLE с грязной позицией).

Зависимости:
  IBroker.create_order(request)    → OrderCreated
  IBroker.cancel_order(order_id)   → CancelResult
  StateManager.commit(old, new)    → BotState  (two-phase commit)
"""
from __future__ import annotations

import logging
import random
import time
import uuid
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING

from .errors import StopCraneError, InsufficientFundsError
from .types import CancelResult, OrderType

if TYPE_CHECKING:
    from broker import IBroker, OrderRequest, OrderCreated
    from bot_state import BotState, StateManager
    from observability import EventEmitter

logger = logging.getLogger(__name__)


class OrderManager:
    """
    Управляет постановкой и отменой ордеров.

    Каждый метод:
      1. Генерирует client_order_id (UUID).
      2. Сохраняет pending_client_order_id в bot_state через StateManager.commit().
      3. Вызывает broker.create_order() / cancel_order().
      4. При успехе — сохраняет exchange_order_id, очищает pending.
      5. При таймауте create_order — StopCraneError (без retry).
      6. При сетевой ошибке — экспоненциальный retry с jitter.
    """

    # INSUFFICIENT_FUNDS коды от известных бирж (пополняется при интеграции)
    _INSUFFICIENT_FUNDS_REASONS = frozenset({
        "insufficient_balance",
        "insufficient_fund",
        "not enough balance",
        "account balance not enough",
        "insufficient_margin",
        "order cost not available",
    })

    def __init__(
        self,
        broker: "IBroker",
        state_manager: "StateManager",
        emitter: "EventEmitter",
        *,
        broker_request_timeout_sec: float = 5.0,
        broker_retry_delay_sec: float = 1.0,
        broker_max_retries: int = 3,
        cancel_max_retries: int = 5,
    ) -> None:
        self._broker                   = broker
        self._state_manager            = state_manager
        self._emitter                  = emitter
        self._request_timeout          = broker_request_timeout_sec
        self._retry_delay              = broker_retry_delay_sec
        self._max_retries              = broker_max_retries
        self._cancel_max_retries       = cancel_max_retries

    # ------------------------------------------------------------------
    # Постановка ордеров
    # ------------------------------------------------------------------

    def place_entry_order(
        self,
        state: "BotState",
        *,
        qty: Decimal,
        price: Decimal | None,    # None → MARKET
        ticker: str,
        cycle_id: str,
    ) -> tuple["OrderCreated", "BotState"]:
        """
        Выставить ордер на вход в позицию.

        Returns:
          (OrderCreated, new_state) — new_state содержит сохранённый
          exchange_order_id в active_entry_order_id.

        Raises:
          StopCraneError          — таймаут (исход неизвестен).
          InsufficientFundsError  — нехватка средств.
        """
        client_id = self._generate_client_id()
        order_type = "LIMIT" if price is not None else "MARKET"

        # Phase 1: сохраняем pending_client_order_id ДО отправки
        new_state = self._state_manager.commit(
            state,
            replace(state, pending_client_order_id=client_id),
        )

        # Строим OrderRequest (интерфейс из П4)
        request = self._build_request(
            ticker=ticker,
            side="BUY",
            order_type=order_type,
            quantity=qty,
            price=price,
            client_order_id=client_id,
            bot_id=state.bot_id,
            cycle_id=cycle_id,
        )

        created = self._send_order(request, order_role=OrderType.ENTRY)

        # Phase 2: сохраняем exchange_order_id
        new_state = self._state_manager.commit(
            new_state,
            replace(
                new_state,
                active_entry_order_id=created.exchange_order_id,
                pending_client_order_id=None,
            ),
        )

        self._emitter.emit(
            event_type="ORDER_CREATED",
            level="INFO",
            message=f"Entry order размещён: {qty} {ticker} @ {price or 'MARKET'}",
            payload={
                "order_id": created.exchange_order_id,
                "client_order_id": client_id,
                "side": "BUY",
                "qty": str(qty),
                "price": str(price) if price else "MARKET",
                "order_type": order_type,
                "role": "ENTRY",
            },
        )
        return created, new_state

    def place_tp_order(
        self,
        state: "BotState",
        *,
        qty: Decimal,
        price: Decimal,
        ticker: str,
        cycle_id: str,
    ) -> tuple["OrderCreated", "BotState"]:
        """
        Выставить TP-ордер (LIMIT SELL).

        Перед постановкой проверяет нет ли уже активного TP по тикеру
        — защита от дублирования при рестарте.
        """
        if state.active_tp_order_id is not None:
            logger.warning(
                "place_tp_order: active_tp_order_id=%s уже существует. "
                "Пропускаем постановку дубля.",
                state.active_tp_order_id,
            )
            # Возвращаем заглушку — вызывающий код должен проверять
            from broker import OrderCreated  # noqa: PLC0415
            stub = OrderCreated(
                exchange_order_id=state.active_tp_order_id,
                client_order_id="",
                status="PENDING",
                mode=self._broker.get_mode(),
            )
            return stub, state

        client_id = self._generate_client_id()

        new_state = self._state_manager.commit(
            state,
            replace(state, pending_client_order_id=client_id),
        )

        request = self._build_request(
            ticker=ticker,
            side="SELL",
            order_type="LIMIT",
            quantity=qty,
            price=price,
            client_order_id=client_id,
            bot_id=state.bot_id,
            cycle_id=cycle_id,
        )

        created = self._send_order(request, order_role=OrderType.TP)

        new_state = self._state_manager.commit(
            new_state,
            replace(
                new_state,
                active_tp_order_id=created.exchange_order_id,
                pending_client_order_id=None,
            ),
        )

        self._emitter.emit(
            event_type="TP_CREATED",
            level="INFO",
            message=f"TP выставлен: {qty} {ticker} @ {price}",
            payload={
                "order_id": created.exchange_order_id,
                "client_order_id": client_id,
                "qty": str(qty),
                "price": str(price),
            },
        )
        return created, new_state

    def place_dca_order(
        self,
        state: "BotState",
        *,
        qty: Decimal,
        price: Decimal | None,
        ticker: str,
        cycle_id: str,
    ) -> tuple["OrderCreated", "BotState"]:
        """Выставить DCA-ордер (BUY LIMIT или MARKET)."""
        client_id = self._generate_client_id()
        order_type = "LIMIT" if price is not None else "MARKET"

        new_state = self._state_manager.commit(
            state,
            replace(state, pending_client_order_id=client_id),
        )

        request = self._build_request(
            ticker=ticker,
            side="BUY",
            order_type=order_type,
            quantity=qty,
            price=price,
            client_order_id=client_id,
            bot_id=state.bot_id,
            cycle_id=cycle_id,
        )

        created = self._send_order(request, order_role=OrderType.DCA)

        # Добавляем к списку активных DCA-ордеров (для EAGER-режима)
        updated_dca_ids = (*state.active_dca_order_ids, created.exchange_order_id)

        new_state = self._state_manager.commit(
            new_state,
            replace(
                new_state,
                active_dca_order_ids=updated_dca_ids,
                pending_client_order_id=None,
            ),
        )

        self._emitter.emit(
            event_type="ORDER_CREATED",
            level="INFO",
            message=f"DCA order: {qty} {ticker} @ {price or 'MARKET'}",
            payload={
                "order_id": created.exchange_order_id,
                "client_order_id": client_id,
                "qty": str(qty),
                "price": str(price) if price else "MARKET",
                "role": "DCA",
                "dca_count_after": state.dca_count + 1,
            },
        )
        return created, new_state

    # ------------------------------------------------------------------
    # Отмена ордеров
    # ------------------------------------------------------------------

    def cancel_order(
        self,
        order_id: str,
        *,
        order_role: str = "UNKNOWN",
    ) -> CancelResult:
        """
        Отменить ордер. Retry до cancel_max_retries с экспоненциальной задержкой.

        Raises:
          StopCraneError — исчерпаны все попытки (нельзя перейти в IDLE).
        """
        from .errors import StopCraneError as _StopCraneError  # noqa: PLC0415

        last_error: Exception | None = None

        for attempt in range(self._cancel_max_retries):
            try:
                result: CancelResult = self._broker.cancel_order(order_id)
                if result.confirmed:
                    self._emitter.emit(
                        event_type="ORDER_CANCELLED",
                        level="WARNING",
                        message=f"Ордер {order_id} ({order_role}) отменён ботом",
                        payload={
                            "order_id": order_id,
                            "initiated_by": "bot",
                            "role": order_role,
                            "attempt": attempt + 1,
                        },
                    )
                    return result

                # Биржа вернула not-confirmed — retry
                logger.warning(
                    "cancel_order не подтверждён биржей (попытка %d/%d): %s",
                    attempt + 1, self._cancel_max_retries, order_id,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Ошибка при cancel_order (попытка %d/%d): %s — %s",
                    attempt + 1, self._cancel_max_retries, order_id, exc,
                )

            if attempt < self._cancel_max_retries - 1:
                delay = self._retry_delay * (2 ** attempt) + random.uniform(0, 0.3)
                time.sleep(delay)

        raise _StopCraneError(
            f"Не удалось подтвердить отмену ордера {order_id} за "
            f"{self._cancel_max_retries} попыток",
            invariant="cancel_confirmed_before_idle",
            expected={"order_id": order_id, "status": "CANCELLED"},
            actually_found={"confirmed": False, "error": str(last_error)},
            db_state={"order_id": order_id, "role": order_role},
        )

    def cancel_all_dca(
        self,
        state: "BotState",
        cycle_id: str,
    ) -> "BotState":
        """
        Mass-cancel всех активных DCA-ордеров текущего цикла.

        Используется при переходе IN_POSITION → CLOSING (TP исполнился).
        Без mass cancel DCA откроют новую позицию.

        Вызывает cancel_order() для каждого — при первой неудаче через
        CANCEL_MAX_RETRIES бросает StopCraneError.
        """
        dca_ids = list(state.active_dca_order_ids)

        if not dca_ids:
            return state

        logger.info("Mass cancel %d DCA-ордеров для цикла %s", len(dca_ids), cycle_id)

        cancelled = []
        for order_id in dca_ids:
            self.cancel_order(order_id, order_role="DCA")  # StopCraneError при неудаче
            cancelled.append(order_id)

        # Очищаем список активных DCA в состоянии
        new_state = self._state_manager.commit(
            state,
            replace(state, active_dca_order_ids=()),
        )
        return new_state

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _send_order(
        self,
        request: "OrderRequest",
        order_role: OrderType,
    ) -> "OrderCreated":
        """
        Отправить ордер на биржу.

        Таймаут = немедленно StopCraneError (исход неизвестен).
        Сетевая ошибка (не таймаут) = retry с экспоненциальной задержкой.
        INSUFFICIENT_FUNDS = InsufficientFundsError.
        """
        from .errors import StopCraneError as _StopCraneError  # noqa: PLC0415
        import socket  # noqa: PLC0415

        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                created = self._broker.create_order(request)
                return created

            except TimeoutError as exc:
                # Таймаут create_order = STOP_CRANE немедленно (ТЗ 4 и ТЗ 7)
                raise _StopCraneError(
                    f"Таймаут create_order ({self._request_timeout}с) — "
                    f"исход ордера неизвестен. Требуется ручная проверка.",
                    invariant="create_order_outcome_known",
                    expected={
                        "client_order_id": request.client_order_id,
                        "status": "PENDING или FILLED",
                    },
                    actually_found=None,
                    db_state={
                        "client_order_id": request.client_order_id,
                        "role": order_role.value,
                    },
                ) from exc

            except Exception as exc:
                exc_str = str(exc).lower()

                # Проверяем нехватку средств
                if any(s in exc_str for s in self._INSUFFICIENT_FUNDS_REASONS):
                    raise InsufficientFundsError(
                        f"Нехватка средств при постановке {order_role.value}: {exc}",
                        required=str(request.quantity * (request.price or Decimal(0))),
                        available="unknown",  # реальный баланс в TickContext
                    ) from exc

                # Сетевая ошибка — retry
                last_error = exc
                if attempt < self._max_retries:
                    delay = (
                        self._retry_delay * (2 ** attempt)
                        + random.uniform(0, self._retry_delay * 0.3)
                    )
                    logger.warning(
                        "Сетевая ошибка при create_order (попытка %d/%d), "
                        "retry через %.1fс: %s",
                        attempt + 1, self._max_retries + 1, delay, exc,
                    )
                    time.sleep(delay)
                else:
                    raise

        raise RuntimeError(f"Неожиданный выход из retry-цикла: {last_error}")

    @staticmethod
    def _generate_client_id() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def _build_request(
        *,
        ticker: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None,
        client_order_id: str,
        bot_id: str,
        cycle_id: str,
    ) -> "OrderRequest":
        """Собрать OrderRequest из П4."""
        from broker import OrderRequest  # noqa: PLC0415
        return OrderRequest(
            ticker=ticker,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            client_order_id=client_order_id,
            bot_id=bot_id,
            cycle_id=cycle_id,
        )
