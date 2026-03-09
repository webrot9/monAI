"""Shared test DB schema matching the actual monAI database tables."""

TEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT, category TEXT, description TEXT,
    status TEXT DEFAULT 'active', allocated_budget REAL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER, project_id INTEGER,
    type TEXT, category TEXT, amount REAL,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT, action TEXT, details TEXT, result TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL, topic TEXT NOT NULL, content TEXT NOT NULL,
    source_agent TEXT NOT NULL, confidence REAL DEFAULT 1.0,
    tags TEXT, referenced_by INTEGER DEFAULT 0,
    expires_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_agent TEXT NOT NULL, to_agent TEXT NOT NULL,
    msg_type TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
    priority INTEGER DEFAULT 5, status TEXT DEFAULT 'unread',
    parent_id INTEGER, metadata TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL, category TEXT NOT NULL,
    situation TEXT NOT NULL, lesson TEXT NOT NULL,
    rule TEXT, severity TEXT DEFAULT 'medium',
    times_applied INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL, cycle INTEGER,
    action_type TEXT NOT NULL, summary TEXT NOT NULL,
    details TEXT, outcome TEXT, duration_ms INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS budget (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    initial_amount REAL NOT NULL, current_balance REAL NOT NULL,
    currency TEXT DEFAULT 'EUR',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cost_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT, cost_type TEXT, model TEXT,
    input_tokens INTEGER, output_tokens INTEGER,
    cost_eur REAL, description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS marketing_campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, campaign_type TEXT NOT NULL,
    strategy_name TEXT, status TEXT DEFAULT 'planned',
    channel TEXT, target_audience TEXT,
    budget_eur REAL DEFAULT 0, spent_eur REAL DEFAULT 0,
    leads_generated INTEGER DEFAULT 0, conversions INTEGER DEFAULT 0,
    revenue_attributed REAL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS marketing_content (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER, content_type TEXT NOT NULL,
    title TEXT, body TEXT, platform TEXT,
    status TEXT DEFAULT 'draft', engagement_score REAL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS marketing_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER, metric_date TEXT NOT NULL,
    impressions INTEGER DEFAULT 0, clicks INTEGER DEFAULT 0,
    leads INTEGER DEFAULT 0, conversions INTEGER DEFAULT 0,
    cost_eur REAL DEFAULT 0, revenue_eur REAL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""
