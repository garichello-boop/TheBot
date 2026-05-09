"""
TelegramSink — асинхронная отправка событий в Telegram через фоновый поток.

Архитектурные решения:
- Фоновый поток с очередью — торговый цикл не блокируется сетевым запросом.
- Ограниченная очередь (maxsize=200): drop policy — удаляются старые INFO,
  ERROR и CRITICAL сохраняются всегда.
- Дедупликация: повторы одной ошибки подавляются на dedup_window_sec (300 сек).
  Итоговое сообщение: "Ошибка повторилась N раз за 5 минут".
- Rate limit: не более max_per_minute сообщений в минуту (дефолт 20).
- Таймауты: connect 3 сек, read 5 сек.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import defaultdict
from typing import Dict, Optional, Tuple
from urllib import request as urllib_request
from urllib.error import URLError
import json

from observability.events import BotEvent
from observability.sinks.base import AbstractSink

_LOG = logging.getLogger(__name__)

_PRIORITY_LEVELS = frozenset({"ERROR", "CRITICAL"})
_CONNECT_TIMEOUT = 3
_READ_TIMEOUT = 5


class TelegramSink(AbstractSink):

    def __init__(
        self,
        token: str,
        chat_id: str,
        max_per_minute: int = 20,
        dedup_window_sec: int = 300,
        queue_maxsize: int = 200,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._max_per_minute = max_per_minute
        self._dedup_window_sec = dedup_window_sec

        self._queue: queue.Queue = queue.Queue(maxsize=queue_maxsize)
        self._stop_event = threading.Event()

        # Rate limiting
        self._sent_this_minute: int = 0
        self._minute_start: float = time.monotonic()

        # Дедупликация: event_type → (first_ts, count)
        self._dedup: Dict[str, Tuple[float, int]] = {}
        self._dedup_lock = threading.Lock()

        self._thread = threading.Thread(
            target=self._worker,
            name="telegram-sink",
            daemon=True,
        )
        self._thread.start()

    # ------------------------------------------------------------------
    # AbstractSink
    # ------------------------------------------------------------------

    def handle(self, event: BotEvent) -> None:
        try:
            self._enqueue(event)
        except Exception:
            pass

    def flush(self, timeout: float = 10.0) -> None:
        """Дождаться опустошения очереди."""
        try:
            self._queue.join()
        except Exception:
            pass

    def close(self) -> None:
        self._stop_event.set()
        # Sentinel для разблокировки worker-а
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=15)

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _enqueue(self, event: BotEvent) -> None:
        """
        Поставить событие в очередь.
        Drop policy при переполнении: выбрасываем старые INFO, сохраняем ERROR/CRITICAL.
        """
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            if event.level in _PRIORITY_LEVELS:
                # Пытаемся освободить место убрав INFO
                self._drop_info_from_queue()
                try:
                    self._queue.put_nowait(event)
                except queue.Full:
                    _LOG.error(
                        f"TelegramSink: очередь полна, событие потеряно: {event.event_type}"
                    )
            # INFO/WARNING при полной очереди — тихо отбрасываем

    def _drop_info_from_queue(self) -> None:
        """Попытаться убрать одно INFO-сообщение из очереди."""
        temp = []
        dropped = False
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                self._queue.task_done()
                if item is None:
                    temp.append(item)
                    break
                if not dropped and item.level not in _PRIORITY_LEVELS:
                    dropped = True  # Выбрасываем это
                else:
                    temp.append(item)
            except queue.Empty:
                break
        for item in temp:
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                break

    def _worker(self) -> None:
        """Фоновый поток: читает очередь и отправляет в Telegram."""
        while not self._stop_event.is_set():
            try:
                event = self._queue.get(timeout=1.0)
                if event is None:
                    self._queue.task_done()
                    break
                self._process(event)
                self._queue.task_done()
            except queue.Empty:
                continue
            except Exception as exc:
                _LOG.error(f"TelegramSink worker error: {exc}")

    def _process(self, event: BotEvent) -> None:
        """Дедупликация + rate limiting + отправка."""
        text = self._deduplicate(event)
        if text is None:
            return
        self._rate_limit_send(text)

    def _deduplicate(self, event: BotEvent) -> Optional[str]:
        """
        Подавляет повторы одного event_type в течение dedup_window_sec.
        Возвращает текст для отправки или None если нужно подавить.
        По истечении окна — отправляет итоговое сообщение с количеством повторов.
        """
        key = event.event_type
        now = time.monotonic()

        with self._dedup_lock:
            if key in self._dedup:
                first_ts, count = self._dedup[key]
                if now - first_ts < self._dedup_window_sec:
                    self._dedup[key] = (first_ts, count + 1)
                    return None  # Подавляем
                else:
                    # Окно истекло — сначала отправим итог если были повторы
                    if count > 1:
                        summary = (
                            f"⚠️ [{event.bot_id}] {key} повторилось {count} раз "
                            f"за {self._dedup_window_sec // 60} мин"
                        )
                        self._dedup[key] = (now, 1)
                        self._rate_limit_send(summary)
                    else:
                        self._dedup[key] = (now, 1)
            else:
                self._dedup[key] = (now, 1)

        return self._format(event)

    def _format(self, event: BotEvent) -> str:
        emoji = {"DEBUG": "🔍", "INFO": "ℹ️", "WARNING": "⚠️", "ERROR": "❌", "CRITICAL": "🚨"}.get(
            event.level, "•"
        )
        parts = [f"{emoji} <b>[{event.bot_id}]</b> {event.event_type}"]
        if event.ticker:
            parts.append(f"Тикер: {event.ticker}")
        if event.cycle_id:
            parts.append(f"Цикл: {event.cycle_id}")
        parts.append(event.message)
        return "\n".join(parts)

    def _rate_limit_send(self, text: str) -> None:
        """Соблюдает лимит max_per_minute сообщений в минуту."""
        now = time.monotonic()
        if now - self._minute_start >= 60:
            self._sent_this_minute = 0
            self._minute_start = now

        if self._sent_this_minute >= self._max_per_minute:
            # Ждём конца текущей минуты
            wait = 60 - (now - self._minute_start)
            if wait > 0:
                time.sleep(wait)
            self._sent_this_minute = 0
            self._minute_start = time.monotonic()

        self._send(text)
        self._sent_this_minute += 1

    def _send(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = json.dumps({
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
        }).encode("utf-8")

        req = urllib_request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=_CONNECT_TIMEOUT + _READ_TIMEOUT) as resp:
                resp.read()
        except URLError as exc:
            _LOG.warning(f"TelegramSink: не удалось отправить: {exc}")
        except Exception as exc:
            _LOG.warning(f"TelegramSink: ошибка отправки: {exc}")
