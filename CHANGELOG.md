# CHANGELOG — TheBot

Формат: одна запись на сессию разработки.
Статус: paper trading, pre-live (PaperBroker, BTCUSDT, MeanReversion).

---

## [Unreleased] — 2026-05-17 · Session 2: PnL fix, Telegram dedup

### Fixed

**PnL считался неверно (~78 USDT вместо ~-0.10 USDT)**

Корневая причина: `quote_received` аккумулировался дважды.
`partial_fill.handle_tp_fill()` применял fill и выставлял `last_applied_trade_id`.
Затем `close_protocol._step_5_7_apply_fills()` применял тот же fill повторно,
удваивая `quote_received`. Итоговая формула давала `2*QR - QS` вместо `QR - QS`.

- `business_logic/close_protocol.py` · `_step_5_7_apply_fills`:
  добавлена проверка `if fill.exchange_order_id == state.last_applied_trade_id: continue` —
  уже применённые fills пропускаются.

- `business_logic/close_protocol.py` · `_step_11_13_finalize`:
  `log_quote_received` и `log_quote_spent` захватываются до `commit` со сбросом в 0.
  Ранее payload `CYCLE_CLOSED` всегда содержал `quote_spent=0, quote_received=0`
  (читался post-reset state).

**Дублирующий emit `ORDER_FILLED` вызывал спам в Telegram**

PaperBroker эмитит `ORDER_FILLED` при исполнении ордера.
`partial_fill` дополнительно эмитил второй `ORDER_FILLED` при применении fill к state.
TelegramSink с ключом дедупликации только по `event_type` видел второй emit
как дубль и копил счётчик — при следующем цикле отправлял "ORDER_FILLED повторилось 2 раз за 5 мин".

- `business_logic/partial_fill.py` · `_accept_entry`, `handle_tp_fill`, `handle_dca_fill`:
  `event_type="ORDER_FILLED"` → `"TRADE_APPLIED"`. Применение fill к state —
  это внутреннее событие, не торговое. `TRADE_APPLIED` есть в реестре событий (ТЗ-3),
  в Telegram не роутится, в лог и NDJSON идёт.

- `observability/telegram_sink.py` · `_deduplicate`:
  ключ изменён с `event.event_type` на `f"{event_type}:{exchange_order_id}"`.
  Каждый fill уникален по order_id — дедупликация теперь работает корректно.

---

## [Unreleased] — 2026-05-10 · Session 1: initial working bot

Первый полный цикл: `ORDER_CREATED → ORDER_FILLED → CYCLE_STARTED → TP_CREATED → TP_FILLED → CYCLE_CLOSED`.

### Fixed

**LIMIT-ордера никогда не исполнялись**

- `business_logic/tick_context.py`:
  добавлен вызов `broker.process_market_tick(price_data.bid, price_data.ask)`
  между шагом получения цены и шагом получения open_orders.
  Без этого PaperBroker не обрабатывал тик и fills не появлялись.

**TP никогда не выставлялся после входа**

- `business_logic/decision.py` · `_decide_in_position`:
  добавлена проверка в начало метода:
  `if state.active_tp_order_id is None and not ctx.fills_for_tp → return PLACE_TP`.

- `business_logic/bot_loop.py` · `_execute_place_tp`:
  если `signal.tp_price is None` — пересчитывает через `strategy.evaluate()`
  вместо silent skip.

- `strategies/mean_reversion.py`:
  при открытой позиции (`position_qty > 0`) всегда возвращает
  `tp_price=Decimal(str(ma))`. Ранее возвращал `None`.

**`process_market_tick` не дренировал fill_queue**

- `broker/paper_broker.py` · `process_market_tick`:
  убран дренаж `_fill_queue` из метода — fills накапливаются для `get_pending_fills()`.

**`AttributeError: 'str' object has no attribute 'value'`**

- `broker/paper_broker.py` · `_execute_fill`:
  нормализация side: `side_str = request.side if isinstance(request.side, str) else request.side.value`.

**Тикер "UNKNOWN" в Telegram**

- `observability/emitter.py`:
  добавлен метод `set_ticker(ticker: str)` по аналогии с `set_cycle_id()`.

- `bot.py`:
  `emitter.set_ticker(ticker)` вызывается сразу после загрузки `bot_config`.

**Бот не обновлял bot_registry при остановке через NSSM**

- `bot.py` · блок `finally`:
  добавлен `registry_repo.update_status(user_id, bot_id, operational_status="STOPPED")`.

**Автозапуск через NSSM не подхватывал мастер-пароль**

- `bot.py`:
  `os.environ.get("BOT_MASTER_PASSWORD") or getpass.getpass(...)` —
  пароль читается из системной переменной окружения, fallback на интерактивный ввод.

**Отрицательный heartbeat из-за timezone-конфликта**

- `db/connection.py`:
  добавлен `options="-c timezone=UTC"` в `ThreadedConnectionPool`.

**Диагностика PnL**

- `business_logic/close_protocol.py`:
  добавлен диагностический лог `PnL breakdown: quote_received=X, quote_spent=Y, pnl=Z`
  (выявил баг двойного счёта, исправлен в Session 2).
