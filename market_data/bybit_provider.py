"""
РџСѓРЅРєС‚ 2: РџСЂРѕРІР°Р№РґРµСЂ СЂС‹РЅРѕС‡РЅС‹С… РґР°РЅРЅС‹С… Bybit.

MVP: REST-only СЂРµР°Р»РёР·Р°С†РёСЏ.
WebSocket Р±СѓРґРµС‚ РґРѕР±Р°РІР»РµРЅ РїРµСЂРµРґ РїРµСЂРµС…РѕРґРѕРј РЅР° Live-С‚РѕСЂРіРѕРІР»СЋ.

РўРµРєСѓС‰РёР№ СЂРµР¶РёРј: РїРѕСЃС‚РѕСЏРЅРЅС‹Р№ FALLBACK_REST.
Р¤РѕРЅРѕРІС‹Р№ РїРѕС‚РѕРє РѕРїСЂР°С€РёРІР°РµС‚ Bybit V5 REST РєР°Р¶РґС‹Рµ poll_interval_sec.
get_price() РІРѕР·РІСЂР°С‰Р°РµС‚ РїРѕСЃР»РµРґРЅРµРµ Р·Р°РєСЌС€РёСЂРѕРІР°РЅРЅРѕРµ Р·РЅР°С‡РµРЅРёРµ.

РџСѓР±Р»РёС‡РЅС‹Р№ Bybit V5 endpoint (Р±РµР· Р°СѓС‚РµРЅС‚РёС„РёРєР°С†РёРё):
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

# Bybit V5 РїСѓР±Р»РёС‡РЅС‹Р№ REST endpoint
_BYBIT_REST_BASE = "https://api.bybit.com"
_TICKER_PATH     = "/v5/market/tickers"

# РљР°С‚РµРіРѕСЂРёСЏ РёРЅСЃС‚СЂСѓРјРµРЅС‚Р°: spot / linear (USDT-РїРµСЂРї) / inverse
# Р”Р»СЏ Р±СѓРјР°Р¶РЅРѕР№ С‚РѕСЂРіРѕРІР»Рё РёСЃРїРѕР»СЊР·СѓРµРј spot вЂ” РјРµРЅСЏРµС‚СЃСЏ С‡РµСЂРµР· РєРѕРЅС„РёРі РІ Р±СѓРґСѓС‰РµРј
_DEFAULT_CATEGORY = "spot"


class BybitProvider(MarketDataProvider):
    """
    РџСЂРѕРІР°Р№РґРµСЂ СЂС‹РЅРѕС‡РЅС‹С… РґР°РЅРЅС‹С… РґР»СЏ Bybit.

    РџР°СЂР°РјРµС‚СЂС‹ (РёР· MarketSettings):
        poll_interval_sec    вЂ” РёРЅС‚РµСЂРІР°Р» REST-РѕРїСЂРѕСЃР° (РґРµС„РѕР»С‚ 10 СЃРµРє).
        stale_threshold_sec  вЂ” РїРѕСЂРѕРі СѓСЃС‚Р°СЂРµРІР°РЅРёСЏ РґР»СЏ WatchdogTimer (РґРµС„РѕР»С‚ 30 СЃРµРє).
        reconnect_delay_sec  вЂ” РЅР°С‡Р°Р»СЊРЅР°СЏ Р·Р°РґРµСЂР¶РєР° СЂРµРєРѕРЅРЅРµРєС‚Р° WS (РґРµС„РѕР»С‚ 1 СЃРµРє).
        max_reconnect_sec    вЂ” РјР°РєСЃРёРјР°Р»СЊРЅР°СЏ Р·Р°РґРµСЂР¶РєР° СЂРµРєРѕРЅРЅРµРєС‚Р° (РґРµС„РѕР»С‚ 30 СЃРµРє).
        spike_threshold_pct  вЂ” РїРѕСЂРѕРі Р°РЅРѕРјР°Р»СЊРЅРѕРіРѕ СЃРєР°С‡РєР° РґР»СЏ PriceValidator (РґРµС„РѕР»С‚ 10%).
        max_spread_pct       вЂ” РїРѕСЂРѕРі С€РёСЂРѕРєРѕРіРѕ СЃРїСЂРµРґР° (РґРµС„РѕР»С‚ 1%).
        request_timeout_sec  вЂ” С‚Р°Р№РјР°СѓС‚ HTTP-Р·Р°РїСЂРѕСЃР° (РґРµС„РѕР»С‚ 5 СЃРµРє).
        category             вЂ” РєР°С‚РµРіРѕСЂРёСЏ Bybit: spot / linear / inverse (РґРµС„РѕР»С‚ spot).

    РСЃРїРѕР»СЊР·РѕРІР°РЅРёРµ:
        provider = BybitProvider(
            poll_interval_sec=10,
            stale_threshold_sec=30,
            ...
        )
        provider.start()
        provider.subscribe("BTCUSDT")

        price = provider.get_price("BTCUSDT")
        # price.ask вЂ” С†РµРЅР° РґР»СЏ РїРѕРєСѓРїРєРё
        # price.bid вЂ” С†РµРЅР° РґР»СЏ РїСЂРѕРґР°Р¶Рё

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

        # РљСЌС€ РїРѕСЃР»РµРґРЅРёС… С†РµРЅ: ticker в†’ PriceData
        self._prices:     Dict[str, PriceData] = {}
        self._prices_lock = threading.Lock()

        # РџРѕРґРїРёСЃР°РЅРЅС‹Рµ С‚РёРєРµСЂС‹
        self._subscriptions:     set  = set()
        self._subscriptions_lock = threading.Lock()

        # Р¤Р»Р°РіРё Р¶РёР·РЅРµРЅРЅРѕРіРѕ С†РёРєР»Р°
        self._started  = False
        self._status   = ProviderStatus.FALLBACK_REST  # MVP: РІСЃРµРіРґР° REST
        self._status_lock = threading.Lock()

        # Р¤РѕРЅРѕРІС‹Р№ РїРѕС‚РѕРє REST-РѕРїСЂРѕСЃР°
        self._stop_event  = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None

        # Р’Р°Р»РёРґР°С‚РѕСЂ С†РµРЅ
        self._validator = PriceValidator(
            spike_threshold_pct=spike_threshold_pct,
            max_spread_pct=max_spread_pct,
            stale_threshold_sec=stale_threshold_sec,
            rest_fetcher=self._fetch_price_via_rest,
        )

        # Watchdog: СЃР»РµРґРёС‚ Р·Р° СЃРІРµР¶РµСЃС‚СЊСЋ РґР°РЅРЅС‹С…
        self._watchdog = WatchdogTimer(
            stale_threshold_sec=stale_threshold_sec,
            on_stale=self._on_stale,
            on_recovered=self._on_recovered,
            name=f"Watchdog-Bybit",
        )

        # FallbackManager: СѓРїСЂР°РІР»СЏРµС‚ СЂРµРєРѕРЅРЅРµРєС‚РѕРј WS
        # MVP: ws_teardown Рё ws_connect вЂ” Р·Р°РіР»СѓС€РєРё (WS РЅРµ СЂРµР°Р»РёР·РѕРІР°РЅ)
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
        """РџРѕРґРїРёСЃР°С‚СЊСЃСЏ РЅР° С‚РёРєРµСЂ. РЎР»РµРґСѓСЋС‰РёР№ poll-С†РёРєР» РЅР°С‡РЅС‘С‚ РµРіРѕ РѕРїСЂР°С€РёРІР°С‚СЊ."""
        if not self._started:
            raise ProviderNotStarted("Р’С‹Р·РѕРІРёС‚Рµ start() РїРµСЂРµРґ subscribe().")
        with self._subscriptions_lock:
            self._subscriptions.add(ticker.upper())
        logger.info("BybitProvider: РїРѕРґРїРёСЃРєР° РЅР° %s.", ticker)

    def unsubscribe(self, ticker: str) -> None:
        """РћС‚РїРёСЃР°С‚СЊСЃСЏ РѕС‚ С‚РёРєРµСЂР° Рё СѓРґР°Р»РёС‚СЊ РєСЌС€РёСЂРѕРІР°РЅРЅСѓСЋ С†РµРЅСѓ."""
        ticker = ticker.upper()
        with self._subscriptions_lock:
            self._subscriptions.discard(ticker)
        with self._prices_lock:
            self._prices.pop(ticker, None)
        logger.info("BybitProvider: РѕС‚РїРёСЃРєР° РѕС‚ %s.", ticker)

    def get_price(self, ticker: str) -> PriceData:
        """
        Р’РµСЂРЅСѓС‚СЊ РїРѕСЃР»РµРґРЅСЋСЋ Р·Р°РєСЌС€РёСЂРѕРІР°РЅРЅСѓСЋ С†РµРЅСѓ.

        Raises:
            ProviderNotStarted:    start() РЅРµ РІС‹Р·С‹РІР°Р»СЃСЏ.
            TickerNotSubscribed:   С‚РёРєРµСЂ РЅРµ РїРѕРґРїРёСЃР°РЅ.
            MarketDataUnavailable: РґР°РЅРЅС‹С… РЅРµС‚ (РµС‰С‘ РЅРµ РїРѕР»СѓС‡РµРЅС‹ РёР»Рё РїСЂРѕРІР°Р№РґРµСЂ РІ FAILED).
        """
        if not self._started:
            raise ProviderNotStarted("Р’С‹Р·РѕРІРёС‚Рµ start() РїРµСЂРµРґ get_price().")

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
                reason="Р”Р°РЅРЅС‹Рµ РµС‰С‘ РЅРµ РїРѕР»СѓС‡РµРЅС‹ вЂ” РґРѕР¶РґРёС‚РµСЃСЊ РїРµСЂРІРѕРіРѕ poll-С†РёРєР»Р°.",
            )

        status = self.get_status()
        if status == ProviderStatus.FAILED:
            raise MarketDataUnavailable(
                ticker=ticker,
                status=status,
                reason="РџСЂРѕРІР°Р№РґРµСЂ РІ СЃС‚Р°С‚СѓСЃРµ FAILED.",
            )

        return price

    def get_status(self) -> ProviderStatus:
        with self._status_lock:
            return self._status

    def start(self) -> None:
        """Р—Р°РїСѓСЃС‚РёС‚СЊ РїСЂРѕРІР°Р№РґРµСЂ: Watchdog + С„РѕРЅРѕРІС‹Р№ REST-РѕРїСЂРѕСЃ."""
        if self._started:
            logger.debug("BybitProvider: СѓР¶Рµ Р·Р°РїСѓС‰РµРЅ, РїСЂРѕРїСѓСЃРєР°РµРј.")
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
            "BybitProvider: Р·Р°РїСѓС‰РµРЅ (REST, poll_interval=%ds).",
            self._poll_interval_sec,
        )

    def stop(self) -> None:
        """РљРѕСЂСЂРµРєС‚РЅРѕ РѕСЃС‚Р°РЅРѕРІРёС‚СЊ РїСЂРѕРІР°Р№РґРµСЂ."""
        if not self._started:
            return

        self._started = False
        self._stop_event.set()

        self._watchdog.stop()
        self._fallback.stop()

        if self._poll_thread is not None:
            self._poll_thread.join(timeout=self._request_timeout + 2.0)
            self._poll_thread = None

        logger.info("BybitProvider: РѕСЃС‚Р°РЅРѕРІР»РµРЅ.")

    def is_healthy(self) -> bool:
        return self.get_status() in (
            ProviderStatus.CONNECTED,
            ProviderStatus.FALLBACK_REST,
        )

    # ------------------------------------------------------------------
    # REST polling loop (С„РѕРЅРѕРІС‹Р№ РїРѕС‚РѕРє)
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
        """РћРїСЂР°С€РёРІР°РµС‚ REST РєР°Р¶РґС‹Рµ poll_interval_sec РґР»СЏ РІСЃРµС… РїРѕРґРїРёСЃР°РЅРЅС‹С… С‚РёРєРµСЂРѕРІ."""
        logger.debug("BybitProvider: poll-С†РёРєР» Р·Р°РїСѓС‰РµРЅ.")

        while not self._stop_event.is_set():
            with self._subscriptions_lock:
                tickers = list(self._subscriptions)

            for ticker in tickers:
                if self._stop_event.is_set():
                    break
                self._poll_ticker(ticker)

            self._stop_event.wait(timeout=self._poll_interval_sec)

        logger.debug("BybitProvider: poll-С†РёРєР» Р·Р°РІРµСЂС€С‘РЅ.")

    def _poll_ticker(self, ticker: str) -> None:
        """РћРґРёРЅ REST-Р·Р°РїСЂРѕСЃ РїРѕ С‚РёРєРµСЂСѓ + РІР°Р»РёРґР°С†РёСЏ + РєСЌС€РёСЂРѕРІР°РЅРёРµ."""
        try:
            new_price = self._fetch_price_via_rest(ticker)
        except Exception as exc:
            logger.error("BybitProvider: REST-Р·Р°РїСЂРѕСЃ РґР»СЏ %s Р·Р°РІРµСЂС€РёР»СЃСЏ РѕС€РёР±РєРѕР№: %s", ticker, exc)
            self._set_status(ProviderStatus.STALE)
            return

        if new_price is None:
            logger.warning("BybitProvider: REST РІРµСЂРЅСѓР» None РґР»СЏ %s.", ticker)
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
                "BybitProvider: С†РµРЅР° %s РѕС‚РєР»РѕРЅРµРЅР° РІР°Р»РёРґР°С‚РѕСЂРѕРј: %s",
                ticker, result.reason,
            )
            return

        with self._prices_lock:
            self._prices[ticker] = new_price

        # РћР±РЅРѕРІРёС‚СЊ Watchdog вЂ” РґР°РЅРЅС‹Рµ СЃРІРµР¶РёРµ
        self._watchdog.update()

        if result.wide_spread:
            logger.warning(
                "BybitProvider: С€РёСЂРѕРєРёР№ СЃРїСЂРµРґ РґР»СЏ %s (%.4f%%).",
                ticker, float(new_price.spread_pct),
            )

        if result.spike_detected:
            logger.warning(
                "BybitProvider: СЃРєР°С‡РѕРє %.2f%% РґР»СЏ %s (outcome=%s).",
                result.spike_pct, ticker, result.outcome.value,
            )

        logger.debug(
            "BybitProvider: %s bid=%.6f ask=%.6f last=%.6f",
            ticker, new_price.bid, new_price.ask, new_price.last,
        )

    # ------------------------------------------------------------------
    # REST fetch (РёСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ Рё poll-С†РёРєР»РѕРј, Рё PriceValidator)
    # ------------------------------------------------------------------

    def _fetch_price_via_rest(self, ticker: str) -> Optional[PriceData]:
        """
        Р—Р°РїСЂРѕСЃРёС‚СЊ С†РµРЅСѓ С‚РёРєРµСЂР° С‡РµСЂРµР· Bybit V5 REST API.

        Р’РѕР·РІСЂР°С‰Р°РµС‚ PriceData РёР»Рё None РїСЂРё РѕС€РёР±РєРµ.
        Р‘СЂРѕСЃР°РµС‚ requests.RequestException РїСЂРё СЃРµС‚РµРІРѕР№ РїСЂРѕР±Р»РµРјРµ.
        """
        url    = f"{_BYBIT_REST_BASE}{_TICKER_PATH}"
        params = {"category": self._category, "symbol": ticker.upper()}

        response = requests.get(url, params=params, timeout=self._request_timeout)
        response.raise_for_status()

        data = response.json()

        ret_code = data.get("retCode", -1)
        if ret_code != 0:
            logger.warning(
                "BybitProvider: REST РІРµСЂРЅСѓР» retCode=%d, msg=%s",
                ret_code, data.get("retMsg", ""),
            )
            return None

        items = data.get("result", {}).get("list", [])
        if not items:
            logger.warning("BybitProvider: РїСѓСЃС‚РѕР№ СЃРїРёСЃРѕРє С‚РёРєРµСЂРѕРІ РґР»СЏ %s.", ticker)
            return None

        item = items[0]

        bid  = Decimal(str(item.get("bid1Price", "0")))
        ask  = Decimal(str(item.get("ask1Price", "0")))
        last = Decimal(str(item.get("lastPrice",  "0")))

        if bid <= 0 or ask <= 0 or last <= 0:
            logger.warning(
                "BybitProvider: РЅРµРєРѕСЂСЂРµРєС‚РЅС‹Рµ С†РµРЅС‹ РґР»СЏ %s: bid=%s ask=%s last=%s",
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

        # РџСЂРѕРІРµСЂСЏРµРј СЃРїСЂРµРґ (wide_spread РЅСѓР¶РµРЅ СѓР¶Рµ Р·РґРµСЃСЊ вЂ” РІ TickContext)
        spread_wide = float(price.spread_pct) > self._validator._max_spread_pct
        if spread_wide:
            # РџРµСЂРµСЃРѕР·РґР°С‘Рј СЃ С„Р»Р°РіРѕРј (frozen dataclass)
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
    # WS Р·Р°РіР»СѓС€РєРё (TODO: СЂРµР°Р»РёР·РѕРІР°С‚СЊ РїРµСЂРµРґ Live)
    # ------------------------------------------------------------------

    def _ws_connect_stub(self) -> None:
        """
        TODO: СЂРµР°Р»РёР·РѕРІР°С‚СЊ WebSocket-РїРѕРґРєР»СЋС‡РµРЅРёРµ РїРµСЂРµРґ РїРµСЂРµС…РѕРґРѕРј РЅР° Live.

        Р”РѕР»Р¶РЅРѕ:
          1. РћС‚РєСЂС‹С‚СЊ WS-СЃРѕРµРґРёРЅРµРЅРёРµ Рє Bybit (wss://stream.bybit.com/v5/public/spot).
          2. РџРѕРґРїРёСЃР°С‚СЊСЃСЏ РЅР° С‚РёРєРµСЂС‹ РёР· self._subscriptions.
          3. Р—Р°РїСѓСЃС‚РёС‚СЊ РѕР±СЂР°Р±РѕС‚С‡РёРє СЃРѕРѕР±С‰РµРЅРёР№ (РІС‹Р·С‹РІР°С‚СЊ _on_ws_message РЅР° РєР°Р¶РґС‹Р№ С‚РёРє).
          4. РџСЂРё СѓСЃРїРµС…Рµ вЂ” self._set_status(ProviderStatus.CONNECTED).

        РџСЂРёРјРµСЂ РїРѕРґРїРёСЃРєРё Bybit WS:
          {"op": "subscribe", "args": ["tickers.BTCUSDT"]}
        """
        raise NotImplementedError(
            "WebSocket РЅРµ СЂРµР°Р»РёР·РѕРІР°РЅ РІ MVP. "
            "РџСЂРѕРІР°Р№РґРµСЂ СЂР°Р±РѕС‚Р°РµС‚ РІ СЂРµР¶РёРјРµ REST (FALLBACK_REST). "
            "Р РµР°Р»РёР·СѓР№С‚Рµ _ws_connect_stub() РїРµСЂРµРґ РїРµСЂРµС…РѕРґРѕРј РЅР° Live."
        )

    def _ws_teardown_stub(self) -> None:
        """
        TODO: СЂРµР°Р»РёР·РѕРІР°С‚СЊ teardown WS-СЃРѕРµРґРёРЅРµРЅРёСЏ РїРµСЂРµРґ Live.

        Р”РѕР»Р¶РЅРѕ РєРѕСЂСЂРµРєС‚РЅРѕ Р·Р°РєСЂС‹С‚СЊ WS-СЃРѕРµРґРёРЅРµРЅРёРµ, РѕСЃС‚Р°РЅРѕРІРёС‚СЊ РѕР±СЂР°Р±РѕС‚С‡РёРє СЃРѕРѕР±С‰РµРЅРёР№
        Рё РѕСЃРІРѕР±РѕРґРёС‚СЊ С„Р°Р№Р»РѕРІС‹Рµ РґРµСЃРєСЂРёРїС‚РѕСЂС‹ (Р·Р°С‰РёС‚Р° РѕС‚ Р·РѕРјР±Рё-СЃРѕРєРµС‚РѕРІ).
        """
        logger.debug("BybitProvider: WS teardown вЂ” РЅРµС‚ Р°РєС‚РёРІРЅРѕРіРѕ СЃРѕРµРґРёРЅРµРЅРёСЏ (MVP REST-only).")

    # ------------------------------------------------------------------
    # Callbacks (РґР»СЏ WatchdogTimer Рё FallbackManager)
    # ------------------------------------------------------------------

    def _on_stale(self) -> None:
        """WatchdogTimer: РґР°РЅРЅС‹Рµ СѓСЃС‚Р°СЂРµР»Рё."""
        logger.warning("BybitProvider: РґР°РЅРЅС‹Рµ СѓСЃС‚Р°СЂРµР»Рё в†’ Р·Р°РїСЂР°С€РёРІР°РµРј СЂРµРєРѕРЅРЅРµРєС‚.")
        self._fallback.handle_stale()

    def _on_recovered(self) -> None:
        """WatchdogTimer: РґР°РЅРЅС‹Рµ РІРѕСЃСЃС‚Р°РЅРѕРІРёР»РёСЃСЊ."""
        logger.info("BybitProvider: РґР°РЅРЅС‹Рµ РІРѕСЃСЃС‚Р°РЅРѕРІРёР»РёСЃСЊ.")
        self._fallback.handle_ws_recovered()

    def _set_status(self, status: ProviderStatus) -> None:
        with self._status_lock:
            if self._status == status:
                return
            old = self._status
            self._status = status
        logger.info("BybitProvider: СЃС‚Р°С‚СѓСЃ %s в†’ %s.", old.value, status.value)