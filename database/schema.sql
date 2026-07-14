-- Trading agent SQLite schema

CREATE TABLE IF NOT EXISTS strategy_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT UNIQUE NOT NULL,               -- UUID format: "run_20260714_091500_abc123"
    strategy_name TEXT NOT NULL,
    strategy_code TEXT NOT NULL,
    market TEXT NOT NULL,
    data_mode TEXT NOT NULL,                   -- 'paper', 'live', 'historical_replay', 'backtest'
    start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    end_time TIMESTAMP,
    status TEXT DEFAULT 'running',              -- 'running', 'stopped', 'completed', 'error'
    parameters TEXT NOT NULL,                   -- JSON blob
    backtest_start_date TEXT,
    backtest_end_date TEXT,
    backtest_speed REAL DEFAULT 1.0,
    total_trades INTEGER DEFAULT 0,
    winning_trades INTEGER DEFAULT 0,
    losing_trades INTEGER DEFAULT 0,
    gross_pnl REAL DEFAULT 0.0,
    net_pnl REAL DEFAULT 0.0,
    win_rate REAL DEFAULT 0.0,
    max_drawdown REAL DEFAULT 0.0,
    sharpe_ratio REAL,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,                       -- FK to strategy_runs
    trade_id TEXT UNIQUE NOT NULL,               -- "RUN-001-TRADE-003"
    symbol TEXT NOT NULL,
    market TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    strategy_code TEXT NOT NULL,
    side TEXT NOT NULL,                          -- 'BUY', 'SELL'
    entry_price REAL NOT NULL,
    exit_price REAL,
    quantity INTEGER NOT NULL,
    entry_time TIMESTAMP NOT NULL,
    exit_time TIMESTAMP,
    pnl REAL,
    pnl_pct REAL,
    status TEXT DEFAULT 'OPEN',                  -- 'OPEN', 'CLOSED', 'CANCELLED'
    stop_loss REAL,
    take_profit REAL,
    duration_minutes INTEGER,
    parameters TEXT NOT NULL,                    -- JSON blob
    exit_reason TEXT,                             -- 'SL_HIT', 'TP_HIT', 'MARKET_CLOSE', 'MANUAL', 'STRATEGY'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES strategy_runs(run_id)
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    trade_id TEXT NOT NULL UNIQUE,                -- FK to trades
    symbol TEXT NOT NULL,
    market TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    entry_price REAL NOT NULL,
    current_price REAL,
    quantity INTEGER NOT NULL,
    unrealized_pnl REAL DEFAULT 0.0,
    unrealized_pnl_pct REAL DEFAULT 0.0,
    stop_loss REAL,
    take_profit REAL,
    entry_time TIMESTAMP NOT NULL,
    last_update TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (trade_id) REFERENCES trades(trade_id),
    FOREIGN KEY (run_id) REFERENCES strategy_runs(run_id)
);

CREATE TABLE IF NOT EXISTS daily_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    run_id TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    strategy_code TEXT NOT NULL,
    market TEXT NOT NULL,
    data_mode TEXT NOT NULL,
    total_trades INTEGER DEFAULT 0,
    winning_trades INTEGER DEFAULT 0,
    losing_trades INTEGER DEFAULT 0,
    gross_pnl REAL DEFAULT 0.0,
    net_pnl REAL DEFAULT 0.0,
    win_rate REAL DEFAULT 0.0,
    max_drawdown REAL DEFAULT 0.0,
    starting_balance REAL,
    ending_balance REAL,
    UNIQUE(date, run_id)
);

CREATE TABLE IF NOT EXISTS backtest_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,                  -- FK to strategy_runs
    strategy_name TEXT NOT NULL,
    strategy_code TEXT NOT NULL,
    market TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    total_trades INTEGER,
    winning_trades INTEGER,
    losing_trades INTEGER,
    gross_pnl REAL,
    net_pnl REAL,
    win_rate REAL,
    max_drawdown REAL,
    sharpe_ratio REAL,
    sortino_ratio REAL,
    profit_factor REAL,
    avg_win REAL,
    avg_loss REAL,
    largest_win REAL,
    largest_loss REAL,
    parameters TEXT NOT NULL,                     -- JSON
    equity_curve TEXT,                             -- JSON array
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES strategy_runs(run_id)
);

CREATE TABLE IF NOT EXISTS market_data_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TIMESTAMP NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume INTEGER NOT NULL,
    UNIQUE(symbol, timeframe, timestamp)
);

CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    event_type TEXT NOT NULL,                      -- 'SIGNAL', 'ORDER', 'FILL', 'ERROR', 'KILL_SWITCH', 'MARKET_CLOSE'
    symbol TEXT,
    message TEXT NOT NULL,
    data TEXT,                                       -- JSON blob
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cache_lookup ON market_data_cache(symbol, timeframe, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_run ON event_log(run_id);
CREATE INDEX IF NOT EXISTS idx_events_time ON event_log(timestamp);
