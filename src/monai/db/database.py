"""SQLite database layer for monAI."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from monai.config import DB_PATH

SCHEMA = """
-- Strategies the system is running
CREATE TABLE IF NOT EXISTS strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    category TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active',  -- active, paused, stopped
    allocated_budget REAL NOT NULL DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Leads and clients
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    email TEXT,
    company TEXT,
    platform TEXT,          -- upwork, fiverr, email, linkedin, etc.
    platform_id TEXT,       -- their ID on that platform
    stage TEXT NOT NULL DEFAULT 'lead',  -- lead, prospect, contacted, negotiating, client, churned
    source_strategy TEXT,   -- which strategy found them
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Projects / deals
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER REFERENCES contacts(id),
    strategy_id INTEGER REFERENCES strategies(id),
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'proposed',  -- proposed, accepted, in_progress, delivered, paid, cancelled
    quoted_amount REAL,
    paid_amount REAL DEFAULT 0.0,
    currency TEXT DEFAULT 'USD',
    due_date TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- All financial transactions (both expenses and revenue)
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER REFERENCES strategies(id),
    project_id INTEGER REFERENCES projects(id),
    type TEXT NOT NULL,         -- expense, revenue
    category TEXT NOT NULL,     -- api_cost, platform_fee, tool, payment, etc.
    amount REAL NOT NULL,
    currency TEXT DEFAULT 'USD',
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Communication log
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER REFERENCES contacts(id),
    project_id INTEGER REFERENCES projects(id),
    direction TEXT NOT NULL,    -- inbound, outbound
    channel TEXT NOT NULL,      -- email, upwork, fiverr, linkedin, etc.
    subject TEXT,
    body TEXT NOT NULL,
    status TEXT DEFAULT 'sent', -- draft, sent, delivered, read, replied, failed
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Strategy performance snapshots (daily rollups)
CREATE TABLE IF NOT EXISTS performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER REFERENCES strategies(id),
    date TEXT NOT NULL,
    total_revenue REAL DEFAULT 0.0,
    total_expenses REAL DEFAULT 0.0,
    net_profit REAL DEFAULT 0.0,
    active_projects INTEGER DEFAULT 0,
    active_leads INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(strategy_id, date)
);

-- Agent action log (audit trail)
CREATE TABLE IF NOT EXISTS agent_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT,
    result TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Strategy health tracking for self-healing (proxy failures, auto-pause/unpause)
CREATE TABLE IF NOT EXISTS strategy_health (
    strategy_name TEXT PRIMARY KEY,
    consecutive_proxy_failures INTEGER DEFAULT 0,
    total_proxy_failures INTEGER DEFAULT 0,
    total_successes INTEGER DEFAULT 0,
    last_failure_reason TEXT,
    last_failure_at REAL,
    last_success_at REAL,
    auto_paused_at REAL,           -- NULL if not auto-paused
    next_retry_at REAL,            -- When to retry an auto-paused strategy
    retry_count INTEGER DEFAULT 0  -- How many times we've retried after auto-pause
);

-- Checkout links: maps payment_ref back to strategy/product for revenue recording
CREATE TABLE IF NOT EXISTS checkout_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_ref TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    product TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT DEFAULT 'EUR',
    provider TEXT NOT NULL,
    checkout_url TEXT,
    status TEXT DEFAULT 'pending',  -- pending, paid, expired, refunded
    metadata TEXT,                  -- JSON: list_id, bot_id, newsletter_id, etc.
    paid_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Performance indexes
CREATE INDEX IF NOT EXISTS idx_agent_log_agent_name ON agent_log(agent_name);
CREATE INDEX IF NOT EXISTS idx_strategies_status ON strategies(status);
CREATE INDEX IF NOT EXISTS idx_contacts_platform ON contacts(platform, platform_id);
CREATE INDEX IF NOT EXISTS idx_transactions_strategy ON transactions(strategy_id, created_at);
CREATE INDEX IF NOT EXISTS idx_transactions_type ON transactions(type, category);
CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);
CREATE INDEX IF NOT EXISTS idx_messages_contact ON messages(contact_id, created_at);
CREATE INDEX IF NOT EXISTS idx_checkout_links_ref ON checkout_links(payment_ref);
CREATE INDEX IF NOT EXISTS idx_checkout_links_strategy ON checkout_links(strategy_name, status);
"""


class Database:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self):
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def execute(self, query: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.connect() as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchall()

    def execute_insert(self, query: str, params: tuple = ()) -> int:
        with self.connect() as conn:
            cursor = conn.execute(query, params)
            return cursor.lastrowid

    def execute_many(self, query: str, params_list: list[tuple]) -> None:
        with self.connect() as conn:
            conn.executemany(query, params_list)

    @contextmanager
    def transaction(self):
        """Explicit transaction context for atomic multi-statement operations.

        Usage:
            with db.transaction() as conn:
                conn.execute("INSERT INTO ...", (...))
                conn.execute("UPDATE ...", (...))
                # Both succeed or both roll back
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
