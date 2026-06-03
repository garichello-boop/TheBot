-- =============================================================================
-- Создание второго бота: btc_dca_01 (стратегия MartingaleDCA)
-- =============================================================================
-- Запускать в pgAdmin или psql подключившись к БД бота.
-- Предварительно убедись что пользователь igor и тикер BTCUSDT уже существуют
-- в bot_configs (btc_paper_01 должен быть живым — это только INSERT новой строки).
-- =============================================================================

-- Шаг 1: bot_configs
INSERT INTO bot_configs (
    user_id,
    bot_id,
    ticker,
    exchange,
    strategy_name,
    strategy_params,
    virtual_balance,
    status,
    config_version
)
VALUES (
    'igor',
    'btc_dca_01',
    'BTCUSDT',
    'bybit',
    'MartingaleDCA',
    '{
        "INVEST_AMOUNT":     50.0,
        "DROP_TRIGGER_PCT":  3.0,
        "PROFIT_TARGET_PCT": 5.0,
        "MAX_DCA_LEVELS":    3,
        "DCA_MULTIPLIER":    1.5,
        "SL_ENABLED":        false
    }'::jsonb,
    1000.00,
    'ACTIVE',
    1
);

-- Шаг 2: bot_state
-- virtual_balance_free инициализируем равным virtual_balance из bot_configs.
-- Все остальные поля — нули/NULL (чистый старт, IDLE).
INSERT INTO bot_state (
    user_id,
    bot_id,
    version,
    cycle_status,
    virtual_balance_free,
    virtual_balance_locked,
    position_qty,
    position_avg_price,
    dca_count,
    quote_spent,
    quote_received,
    active_entry_order_id,
    active_tp_order_id,
    active_dca_order_ids,
    pending_client_order_id,
    entered_at,
    last_order_at,
    updated_at
)
VALUES (
    'igor',
    'btc_dca_01',
    0,
    'IDLE',
    1000.00,   -- равно virtual_balance в bot_configs
    0.00,
    0.00000000,
    NULL,
    0,
    0.00000000,
    0.00000000,
    NULL,
    NULL,
    '{}',
    NULL,
    NULL,
    NULL,
    NOW()
);

-- Шаг 3: bot_registry (необходим для advisory lock при старте бота)
-- Бот должен видеть operational_status='STOPPED' до первого запуска.
INSERT INTO bot_registry (
    user_id,
    bot_id,
    operational_status,
    last_heartbeat,
    pid,
    started_at,
    stopped_at,
    error_message
)
VALUES (
    'igor',
    'btc_dca_01',
    'STOPPED',
    NULL,
    NULL,
    NULL,
    NULL,
    NULL
);

-- =============================================================================
-- Проверка
-- =============================================================================

SELECT
    c.bot_id,
    c.strategy_name,
    c.status                                  AS config_status,
    c.virtual_balance,
    c.strategy_params->>'INVEST_AMOUNT'       AS invest_usdt,
    c.strategy_params->>'DROP_TRIGGER_PCT'    AS drop_pct,
    c.strategy_params->>'PROFIT_TARGET_PCT'   AS profit_pct,
    c.strategy_params->>'MAX_DCA_LEVELS'      AS max_levels,
    c.strategy_params->>'DCA_MULTIPLIER'      AS multiplier,
    s.cycle_status,
    r.operational_status
FROM bot_configs c
JOIN bot_state    s ON s.user_id = c.user_id AND s.bot_id = c.bot_id
JOIN bot_registry r ON r.user_id = c.user_id AND r.bot_id = c.bot_id
WHERE c.user_id = 'igor'
ORDER BY c.bot_id;
