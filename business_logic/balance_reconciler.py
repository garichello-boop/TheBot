"""
BalanceReconciler — периодическая сверка виртуального баланса с реальным.

Из ТЗ 7:
  Каждые N тиков сверяет real_balance с virtual_balance_free.
  Расхождение > BALANCE_DRIFT_PCT → WARNING + Telegram-алерт.

  virtual_balance — расчётная модель, НЕ источник истины при конфликте.
  При конфликте бот не принимает торговых решений до reconciliation
  (это в StateRecovery при старте). BalanceReconciler только алертит.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from observability import EventEmitter
    from .tick_context import TickContext

logger = logging.getLogger(__name__)


class BalanceReconciler:
    """
    Периодически сверяет виртуальный баланс с реальным балансом биржи.

    Не блокирует торговлю — только алертит. Блокировка при конфликте
    происходит в StateRecovery при старте бота.
    """

    def __init__(
        self,
        emitter: "EventEmitter",
        *,
        check_interval_ticks: int = 10,
        balance_drift_pct: Decimal = Decimal("5"),
        quote_asset: str = "USDT",
        paper_mode: bool = False,
    ) -> None:
        self._emitter              = emitter
        self._check_interval_ticks = check_interval_ticks
        self._balance_drift_pct    = balance_drift_pct
        self._quote_asset          = quote_asset
        self._paper_mode           = paper_mode

    def maybe_check(self, ctx: "TickContext") -> None:
        """
        Вызывается каждый тик. Фактически проверяет только каждые
        check_interval_ticks тиков.
        """
        if ctx.tick_number % self._check_interval_ticks != 0:
            return

        self._check(ctx)

    def _check(self, ctx: "TickContext") -> None:
        # В paper-режиме реальный Bybit-баланс несопоставим с виртуальным —
        # сравнение бессмысленно и генерирует ложные алерты.
        if self._paper_mode:
            logger.debug("Balance reconciliation skipped (PAPER mode)")
            return

        real_free = ctx.balance.free.get(self._quote_asset, Decimal(0))
        virtual_free = ctx.bot_state.virtual_balance_free

        if virtual_free == 0:
            # Нет базы для сравнения — пропускаем
            return

        drift_pct = abs(real_free - virtual_free) / virtual_free * 100

        if drift_pct > self._balance_drift_pct:
            logger.warning(
                "Расхождение баланса: virtual_free=%s, real_free=%s, "
                "drift=%.2f%% > порог %.2f%%",
                virtual_free, real_free,
                float(drift_pct), float(self._balance_drift_pct),
            )
            self._emitter.emit(
                event_type="RECONCILIATION_ERROR",
                level="WARNING",
                message=(
                    f"Расхождение виртуального и реального баланса: "
                    f"{drift_pct:.1f}% > {self._balance_drift_pct}%"
                ),
                payload={
                    "conflict_type":  "balance_drift",
                    "db_value":       str(virtual_free),
                    "exchange_value": str(real_free),
                    "drift_pct":      str(drift_pct),
                    "asset":          self._quote_asset,
                },
            )
        else:
            logger.debug(
                "Balance check OK: virtual=%s, real=%s, drift=%.2f%%",
                virtual_free, real_free, float(drift_pct),
            )
