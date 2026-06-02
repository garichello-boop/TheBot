from collections import deque
import math
from decimal import Decimal

# Импортируем базовый класс из вашего реального файла
from business_logic.strategy import BaseStrategy, StrategySignal

class MeanReversionStrategy(BaseStrategy):
    def __init__(
        self,
        bb_period=20,
        bb_mult=2.0,
        invest_share=0.20,
        stop_loss=0.10,
        take_profit=0.02,
        max_entries=2,
        regime_params=None,
    ):
        # Параметры из вашего файла
        self.bb_period = bb_period
        self.bb_mult = bb_mult
        self.invest_share = Decimal(str(invest_share))
        self.stop_loss = Decimal(str(stop_loss))
        self.take_profit = Decimal(str(take_profit))
        self.max_entries = max_entries
        
        # Буфер для расчета индикаторов
        self.prices = deque(maxlen=max(bb_period + 10, 60))

    def evaluate(self, price_data, snapshot, position_qty) -> StrategySignal:
        """
        Основной метод, который требует ваш BotLoop (из файла strategy.py)
        """
        close = Decimal(str(price_data.last))
        self.prices.append(float(close))

        # Расчет полос Боллинджера
        if len(self.prices) < self.bb_period:
            return StrategySignal(should_enter=False, target_qty=None, target_avg_price=None, tp_price=None, reason="loading_data")

        window = list(self.prices)[-self.bb_period:]
        ma = sum(window) / self.bb_period
        variance = sum((x - ma) ** 2 for x in window) / self.bb_period
        std = math.sqrt(variance)
        lower_band = Decimal(str(ma - self.bb_mult * std))
        upper_band = Decimal(str(ma + self.bb_mult * std))

        # ЛОГИКА СИГНАЛОВ
        # 1. Если позиции нет — ищем вход
        if position_qty == 0:
            if close <= lower_band:
                # Пример расчета target_qty (нужно адаптировать под ваш баланс)
                return StrategySignal(
                    should_enter=True,
                    target_qty=Decimal("0.001"), # Тестовый объем
                    target_avg_price=close,
                    tp_price=Decimal(str(ma)),
                    reason="mr_entry"
                )
        
        # 2. Если позиция есть — TP всегда на MA, ищем выход
        else:
            tp = Decimal(str(ma))  # TP всегда на средней линии Боллинджера
            if close >= tp:
                return StrategySignal(
                    should_enter=False,
                    target_qty=Decimal("0"),
                    target_avg_price=None,
                    tp_price=tp,
                    reason="mr_exit_at_ma"
                )
            return StrategySignal(
                should_enter=False,
                target_qty=position_qty,
                target_avg_price=None,
                tp_price=tp,  # всегда передаём TP цену боту
                reason="wait"
            )

        return StrategySignal(should_enter=False, target_qty=position_qty, target_avg_price=None, tp_price=None, reason="wait")

    def name(self) -> str:
        return "MeanReversion"