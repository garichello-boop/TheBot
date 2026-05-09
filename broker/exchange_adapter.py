"""
broker/exchange_adapter.py — изоляция биржевой специфики client_order_id.

Разные биржи называют поле идемпотентности по-разному:
    Bybit:   orderLinkId
    Binance: newClientOrderId
    OKX:     clOrdId

OrderManager работает только с внутренним client_order_id (UUID).
ExchangeAdapter знает как передать его на конкретную биржу и как
извлечь обратно из ответа. Переключение биржи = смена адаптера,
торговая логика не меняется.

Использование внутри BybitBroker:

    adapter = BybitExchangeAdapter()

    # Перед отправкой ордера:
    params = adapter.inject(base_params, order.client_order_id)

    # При разборе ответа:
    client_id = adapter.extract(response)

    # При reconciliation — знаем какое поле проверять:
    print(adapter.field_name)  # "orderLinkId"
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class ExchangeAdapter(ABC):
    """
    Абстрактный адаптер для маппинга client_order_id.

    Каждая биржа получает свою реализацию. Логика создания ордеров,
    reconciliation и retry в OrderManager/BybitBroker не зависит от
    конкретного поля — только от этого адаптера.
    """

    @property
    @abstractmethod
    def field_name(self) -> str:
        """
        Название поля client_order_id на этой бирже.
        Используется при reconciliation для явного запроса по полю.
        """

    @abstractmethod
    def inject(self, params: dict, client_order_id: str) -> dict:
        """
        Добавить client_order_id в params запроса под именем биржи.

        Не мутирует исходный dict — возвращает новый.
        Если биржа не поддерживает поле (NoClientOrderIdAdapter) — params
        возвращается без изменений.
        """

    @abstractmethod
    def extract(self, response: dict) -> Optional[str]:
        """
        Извлечь client_order_id из ответа биржи.

        Возвращает None если:
        - поле отсутствует в ответе
        - биржа не поддерживает client_order_id

        При None OrderManager знает что нужно reconciliation по тикеру/объёму.
        """

    @property
    def supports_client_order_id(self) -> bool:
        """
        True если биржа поддерживает идемпотентность по client_order_id.

        При False OrderManager обязан выполнять reconciliation перед повторной
        отправкой ордера: проверить наличие открытого ордера по тикеру и объёму
        прежде чем создавать новый. Иначе возможен дублирующий ордер.
        """
        return True


class BybitExchangeAdapter(ExchangeAdapter):
    """
    Адаптер для Bybit.
    client_order_id ↔ orderLinkId (до 36 символов, UUID подходит).
    """

    _FIELD = "orderLinkId"

    @property
    def field_name(self) -> str:
        return self._FIELD

    def inject(self, params: dict, client_order_id: str) -> dict:
        return {**params, self._FIELD: client_order_id}

    def extract(self, response: dict) -> Optional[str]:
        # Bybit оборачивает данные в result: {"retCode": 0, "result": {...}}
        # Но также встречается плоский формат в WS-апдейтах
        result = response.get("result", response)
        value = result.get(self._FIELD) or response.get(self._FIELD)
        return value if value else None


class BinanceExchangeAdapter(ExchangeAdapter):
    """
    Адаптер для Binance.
    client_order_id ↔ newClientOrderId (до 36 символов).
    В ответах Binance возвращает поле как clientOrderId (без 'new').
    """

    _INJECT_FIELD = "newClientOrderId"
    _RESPONSE_FIELD = "clientOrderId"

    @property
    def field_name(self) -> str:
        return self._INJECT_FIELD

    def inject(self, params: dict, client_order_id: str) -> dict:
        return {**params, self._INJECT_FIELD: client_order_id}

    def extract(self, response: dict) -> Optional[str]:
        # В ответах Binance поле называется clientOrderId
        value = response.get(self._RESPONSE_FIELD) or response.get(self._INJECT_FIELD)
        return value if value else None


class OKXExchangeAdapter(ExchangeAdapter):
    """
    Адаптер для OKX.
    client_order_id ↔ clOrdId (до 32 символов — UUID без дефисов).
    Добавляется при интеграции OKX без изменений в торговой логике.
    """

    _FIELD = "clOrdId"

    @property
    def field_name(self) -> str:
        return self._FIELD

    def inject(self, params: dict, client_order_id: str) -> dict:
        # OKX: максимум 32 символа — убираем дефисы из UUID
        okx_id = client_order_id.replace("-", "")[:32]
        return {**params, self._FIELD: okx_id}

    def extract(self, response: dict) -> Optional[str]:
        # OKX возвращает clOrdId в data[0].clOrdId или напрямую
        data = response.get("data", [])
        if data and isinstance(data, list):
            value = data[0].get(self._FIELD)
        else:
            value = response.get(self._FIELD)
        return value if value else None


class NoClientOrderIdAdapter(ExchangeAdapter):
    """
    Заглушка для бирж без поддержки client_order_id.

    supports_client_order_id = False сигнализирует OrderManager:
    перед повторной отправкой ордера сделать reconciliation по тикеру
    и объёму чтобы не создать дублирующий ордер.
    """

    @property
    def field_name(self) -> str:
        return ""

    @property
    def supports_client_order_id(self) -> bool:
        return False

    def inject(self, params: dict, client_order_id: str) -> dict:
        # Поле не добавляем — биржа его не поддерживает
        return params

    def extract(self, response: dict) -> Optional[str]:
        return None
