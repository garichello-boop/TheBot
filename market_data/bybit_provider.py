"""
Пункт 2: Провайдер рыночных данных Bybit.

MVP: REST-only реализация.
WebSocket будет добавлен перед переходом на Live-торговлю.

Текущий режим: постоянный FALLBACK_REST.
Фоновый поток опрашивает Bybit V5 REST каждые poll_interval_sec.
get_price() возвращает последнее закэшированное значение.

Публичный Bybit V5 endpoint (без аутентификации):
  GET https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT
"""

import logging
import threading
import time
from decimal import Decimal
from typing import Dict, Optional

import requests

from market_data.fallback import FallbackManager
from market_data.market_data import (
    MarketDataProvider,
    MarketDataUnavailable,
    PriceData,
    PriceSource,
    ProviderNotStarted,
    ProviderStatus,
    TickerNotSubscribed,
)
from market_data.validator import PriceValidator
from market_data.watchdog import WatchdogTimer

logger = logging.getLogger(__name__)

# Bybit V5 публичный REST endpoint
_BYBIT_REST_BASE = "https://api.bybit.com"
_TICKER_PATH     = "/v5/market/tickers"

# Категория инструмента: spot / linear (USDT-перп) / inverse
# Для бумажной торговли используем spot — меняется через конфиг в будущем
_DEFAULT_CATEGORY = "spot"


class BybitProvider(MarketDataProvider):
    """
    Провайдер рыночных данных для Bybit.

    Параметры (из MarketSettings):
        poll_interval_sec    — интервал REST-опроса (дефолт 10 сек).
        stale_threshold_sec  — порог устаревания для WatchdogTimer (дефолт 30 сек).
        reconnect_delay_sec  — начальная задержка реконнекта WS (дефолт 1 сек).
        max_reconnect_sec    — максимальная задержка реконнекта (дефолт 30 сек).
        spike_threshold_pct  — порог аномального скачка для PriceValidator (дефолт 10%).
        max_spread_pct       — порог широкого спреда (дефолт 1%).
        request_timeout_sec  — таймаут HTTP-запроса (дефолт 5 сек).
        category             — категория Bybit: spot / linear / inverse (дефолт spot).

    Использование:
        provider = BybitProvider(
            poll_interval_sec=10,
            stale_threshold_sec=30,
            ...
        )
        provider.start()
        provider.subscribe("BTCUSDT")

        price = provider.get_price("BTCUSDT")
        # price.ask — цена для покупки
        # price.bid — цена для продажи

        provider.stop()
    """

    def __init__(
        self,
        poll_interval_sec:   int   = 10,
        stale_threshold_sec: int   = 30,
        reconnect_delay_sec: float = 1.0,
        max_reconnect_sec:   float = 30.0,
        spike_threshold_pct: float = 10.0,
        max_spread_pct:      float = 1.0,
        request_timeout_sec: float = 5.0,
        category:            str   = _DEFAULT_CATEGORY,
    ) -> None:
        self._poll_interval_sec  = poll_interval_sec
        self._request_timeout    = request_timeout_sec
        self._category           = category

        # Кэш последних цен: ticker → PriceData
        self._prices:     Dict[str, PriceData] = {}
        self._prices_lock = threading.Lock()

        # Подписанные тикеры
        self._subscriptions:     set  = set()
        self._subscriptions_lock = threading.Lock()

        # Флаги жизненного цикла
        self._started  = False
        self._status   = ProviderStatus.FALLBACK_REST  # MVP: всегда REST
        self._status_lock = threading.Lock()

        # Фоновый поток REST-опроса
        self._stop_event  = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None

        # Валидатор цен
        self._validator = PriceValidator(
            spike_threshold_pct=spike_threshold_pct,
            max_spread_pct=max_spread_pct,
            stale_threshold_sec=stale_threshold_sec,
            rest_fetcher=self._fetch_price_via_rest,
        )

        # Watchdog: следит за свежестью данных
        self._watchdog = WatchdogTimer(
            stale_threshold_sec=stale_threshold_sec,
            on_stale=self._on_stale,
            on_recovered=self._on_recovered,
            name=f"Watchdog-Bybit",
        )

        # FallbackManager: управляет реконнектом WS
        # MVP: ws_teardown и ws_connect — заглушки (WS не реализован)
        self._fallback = FallbackManager(
            reconnect_delay_sec=reconnect_delay_sec,
            max_reconnect_sec=max_reconnect_sec,
            ws_teardown=self._ws_teardown_stub,
            ws_connect=self._ws_connect_stub,
            on_status_change=self._set_status,
        )

    # ------------------------------------------------------------------
    # MarketDataProvider interface
    # ------------------------------------------------------------------

    def subscribe(self, ticker: str) -> None:
        """Подписаться на тикер. Следующий poll-цикл начнёт его опрашивать."""
        if not self._started:
            raise ProviderNotStarted("Вызовите start() перед subscribe().")
        with self._subscriptions_lock:
            self._subscriptions.add(ticker.upper())
        logger.info("BybitProvider: РїРѕРґРїРёСЃРєР° РЅР° %s.", ticker)

    def unsubscribe(self, ticker: str) -> None:
        """Отписаться от тикера и удалить кэшированную цену."""
        ticker = ticker.upper()
        with self._subscriptions_lock:
            self._subscriptions.discard(ticker)
        with self._prices_lock:
            self._prices.pop(ticker, None)
        logger.info("BybitProvider: отписка от %s.", ticker)

    def get_price(self, ticker: str) -> PriceData:
        """
        Вернуть последнюю закэшированную цену.

        Raises:
            ProviderNotStarted:    start() не вызывался.
            TickerNotSubscribed:   тикер не подписан.
            MarketDataUnavailable: данных нет (ещё не получены или провайдер в FAILED).
        """
        if not self._started:
            raise ProviderNotStarted("Вызовите start() перед get_price().")

        ticker = ticker.upper()
        with self._subscriptions_lock:
            if ticker not in self._subscriptions:
                raise TickerNotSubscribed(ticker)

        with self._prices_lock:
            price = self._prices.get(ticker)

        if price is None:
            raise MarketDataUnavailable(
                ticker=ticker,
                status=self.get_status(),
                reason="Данные ещё не получены — дождитесь первого poll-цикла.",
            )

        status = self.get_status()
        if status == ProviderStatus.FAILED:
            raise MarketDataUnavailable(
                ticker=ticker,
                status=status,
                reason="Провайдер в статусе FAILED.",
            )

        return price

    def get_status(self) -> ProviderStatus:
        with self._status_lock:
            return self._status

    def start(self) -> None:
        """Запустить провайдер: Watchdog + фоновый REST-опрос."""
        if self._started:
            logger.debug("BybitProvider: уже запущен, пропускаем.")
            return

        self._started = True
        self._stop_event.clear()
        self._set_status(ProviderStatus.FALLBACK_REST)

        self._watchdog.start()

        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name="BybitProvider-Poll",
            daemon=True,
        )
        self._poll_thread.start()

        logger.info(
            "BybitProvider: запущен (REST, poll_interval=%ds).",
            self._poll_interval_sec,
        )

    def stop(self) -> None:
        """Корректно остановить провайдер."""
        if not self._started:
            return

        self._started = False
        self._stop_event.set()

        self._watchdog.stop()
        self._fallback.stop()

        if self._poll_thread is not None:
            self._poll_thread.join(timeout=self._request_timeout + 2.0)
            self._poll_thread = None

        logger.info("BybitProvider: остановлен.")

    def is_healthy(self) -> bool:
        return self.get_status() in (
            ProviderStatus.CONNECTED,
            ProviderStatus.FALLBACK_REST,
        )

    # ------------------------------------------------------------------
    # REST polling loop (фоновый поток)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Historical data (optional — used for OHLCV playback on PaperBroker restart)
    # ------------------------------------------------------------------

    def get_klines(
        self,
        ticker: str,
        interval_min: int,
        start_ms: int,
        end_ms: int,
    ) -> list:
        """
        Fetch historical OHLCV klines from Bybit V5 REST.

        Args:
            ticker:       instrument symbol, e.g. "BTCUSDT".
            interval_min: candle interval in minutes (1 = 1-minute candles).
            start_ms:     start of period in milliseconds (UTC).
            end_ms:       end of period in milliseconds (UTC).

        Returns:
            List of Kline objects ordered oldest-first.

        Raises:
            MarketDataUnavailable: Bybit returned a non-zero retCode.
            requests.RequestException: HTTP-level error.

        Used by StateRecovery._try_ohlcv_tp_playback() to check if the
        TP price was hit while PaperBroker was offline.
        """
        from market_data.market_data import Kline  # noqa: PLC0415
        from decimal import Decimal as _D            # noqa: PLC0415
        import requests as _req                      # noqa: PLC0415

        url = f"{_BYBIT_REST_BASE}/v5/market/kline"
        params = {
            "category": self._category,
            "symbol":   ticker.upper(),
            "interval": str(interval_min),
            "start":    start_ms,
            "end":      end_ms,
            "limit":    1000,
        }
        resp = _req.get(url, params=params, timeout=self._request_timeout)
        resp.raise_for_status()
        data = resp.json()

        if data.get("retCode") != 0:
            raise MarketDataUnavailable(
                ticker=ticker,
                status=self.get_status(),
                reason=f"kline API error: {data.get('retMsg', 'unknown')}",
            )

        raw = data.get("result", {}).get("list", [])
        klines = []
        for row in raw:
            # Bybit V5 kline row: [startTime, open, high, low, close, volume, turnover]
            klines.append(Kline(
                timestamp_ms=int(row[0]),
                open_price=_D(row[1]),
                high_price=_D(row[2]),
                low_price=_D(row[3]),
                close_price=_D(row[4]),
                volume=_D(row[5]),
            ))

        # Bybit returns newest-first; reverse to get chronological order.
        klines.reverse()

        logger.debug(
            "BybitProvider.get_klines: %s interval=%dm fetched %d candles "
            "[%d ms .. %d ms]",
            ticker, interval_min, len(klines), start_ms, end_ms,
        )
        return klines


    def _poll_loop(self) -> None:
        """Опрашивает REST каждые poll_interval_sec для всех подписанных тикеров."""
        logger.debug("BybitProvider: poll-цикл запущен.")

        while not self._stop_event.is_set():
            with self._subscriptions_lock:
                tickers = list(self._subscriptions)

            for ticker in tickers:
                if self._stop_event.is_set():
                    break
                self._poll_ticker(ticker)

            self._stop_event.wait(timeout=self._poll_interval_sec)

        logger.debug("BybitProvider: poll-цикл завершён.")

    def _poll_ticker(self, ticker: str) -> None:
        """Один REST-запрос по тикеру + валидация + кэширование."""
        try:
            new_price = self._fetch_price_via_rest(ticker)
        except Exception as exc:
            logger.error("BybitProvider: REST-запрос для %s завершился ошибкой: %s", ticker, exc)
            self._set_status(ProviderStatus.STALE)
            return

        if new_price is None:
            logger.warning("BybitProvider: REST вернул None для %s.", ticker)
            return

        with self._prices_lock:
            last_price = self._prices.get(ticker)

        result = self._validator.validate(
            price=new_price,
            last_price=last_price,
            now_ts=time.time(),
        )

        if not result.accepted:
            logger.warning(
                "BybitProvider: цена %s отклонена валидатором: %s",
                ticker, result.reason,
            )
            return

        with self._prices_lock:
            self._prices[ticker] = new_price

        # Обновить Watchdog — данные свежие
        self._watchdog.update()

        if result.wide_spread:
            logger.warning(
                "BybitProvider: широкий спред для %s (%.4f%%).",
                ticker, float(new_price.spread_pct),
            )

        if result.spike_detected:
            logger.warning(
                "BybitProvider: скачок %.2f%% для %s (outcome=%s).",
                result.spike_pct, ticker, result.outcome.value,
            )

        logger.debug(
            "BybitProvider: %s bid=%.6f ask=%.6f last=%.6f",
            ticker, new_price.bid, new_price.ask, new_price.last,
        )

    # ------------------------------------------------------------------
    # REST fetch (используется и poll-циклом, и PriceValidator)
    # ------------------------------------------------------------------

    def _fetch_price_via_rest(self, ticker: str) -> Optional[PriceData]:
        """
        Запросить цену тикера через Bybit V5 REST API.

        Возвращает PriceData или None при ошибке.
        Бросает requests.RequestException при сетевой проблеме.
        """
        url    = f"{_BYBIT_REST_BASE}{_TICKER_PATH}"
        params = {"category": self._category, "symbol": ticker.upper()}

        response = requests.get(url, params=params, timeout=self._request_timeout)
        response.raise_for_status()

        data = response.json()

        ret_code = data.get("retCode", -1)
        if ret_code != 0:
            logger.warning(
                "BybitProvider: REST вернул retCode=%d, msg=%s",
                ret_code, data.get("retMsg", ""),
            )
            return None

        items = data.get("result", {}).get("list", [])
        if not items:
            logger.warning("BybitProvider: пустой список тикеров для %s.", ticker)
            return None

        item = items[0]

        bid  = Decimal(str(item.get("bid1Price", "0")))
        ask  = Decimal(str(item.get("ask1Price", "0")))
        last = Decimal(str(item.get("lastPrice",  "0")))

        if bid <= 0 or ask <= 0 or last <= 0:
            logger.warning(
                "BybitProvider: некорректные цены для %s: bid=%s ask=%s last=%s",
                ticker, bid, ask, last,
            )
            return None

        price = PriceData(
            ticker=ticker.upper(),
            bid=bid,
            ask=ask,
            last=last,
            timestamp=time.time(),
            source=PriceSource.REST,
        )

        # Проверяем спред (wide_spread нужен уже здесь — в TickContext)
        spread_wide = float(price.spread_pct) > self._validator._max_spread_pct
        if spread_wide:
            # Пересоздаём с флагом (frozen dataclass)
            price = PriceData(
                ticker=price.ticker,
                bid=price.bid,
                ask=price.ask,
                last=price.last,
                timestamp=price.timestamp,
                source=price.source,
                wide_spread=True,
            )

        return price

    # ------------------------------------------------------------------
    # WS заглушки (TODO: реализовать перед Live)
    # ------------------------------------------------------------------

    def _ws_connect_stub(self) -> None:
        """
        TODO: реализовать WebSocket-подключение перед переходом на Live.

        Должно:
          1. Открыть WS-соединение к Bybit (wss://stream.bybit.com/v5/public/spot).
          2. Подписаться на тикеры из self._subscriptions.
          3. Запустить обработчик сообщений (вызывать _on_ws_message на каждый тик).
          4. При успехе — self._set_status(ProviderStatus.CONNECTED).

        Пример подписки Bybit WS:
          {"op": "subscribe", "args": ["tickers.BTCUSDT"]}
        """
        raise NotImplementedError(
            "WebSocket не реализован в MVP. "
            "Провайдер работает в режиме REST (FALLBACK_REST). "
            "Реализуйте _ws_connect_stub() перед переходом на Live."
        )

    def _ws_teardown_stub(self) -> None:
        """
        TODO: реализовать teardown WS-соединения перед Live.

        Должно корректно закрыть WS-соединение, остановить обработчик сообщений
        и освободить файловые дескрипторы (защита от зомби-сокетов).
        """
        logger.debug("BybitProvider: WS teardown — нет активного соединения (MVP REST-only).")

    # ------------------------------------------------------------------
    # Callbacks (для WatchdogTimer и FallbackManager)
    # ------------------------------------------------------------------

    def _on_stale(self) -> None:
        """WatchdogTimer: данные устарели."""
        logger.warning("BybitProvider: данные устарели → запрашиваем реконнект.")
        self._fallback.handle_stale()

    def _on_recovered(self) -> None:
        """WatchdogTimer: данные восстановились."""
        logger.info("BybitProvider: данные восстановились.")
        self._fallback.handle_ws_recovered()

    def _set_status(self, status: ProviderStatus) -> None:
        with self._status_lock:
            if self._status == status:
                return
            old = self._status
            self._status = status
        logger.info("BybitProvider: статус %s → %s.", old.value, status.value)