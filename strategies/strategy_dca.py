from strategies.strategy_base import StrategyBase


class SimpleDCA(StrategyBase):
    def __init__(self, drop_trigger=0.02, profit_target=0.05):
        self.drop_trigger = drop_trigger  # Покупаем, если упало на 2%
        self.profit_target = profit_target  # Продаем, если выросло на 5%

    def on_candle(self, candle, portfolio):
        current_price = candle['close']

        # Если позиция пустая — заходим первым ордером
        if portfolio.asset_qty == 0:
            return "BUY"

        # Если цена ниже средней на 2% — усредняемся
        price_change = (current_price - portfolio.avg_price) / portfolio.avg_price

        if price_change <= -self.drop_trigger:
            return "BUY"

        # Если цена выше средней на 5% — забираем профит
        if price_change >= self.profit_target:
            return "SELL"

        return None