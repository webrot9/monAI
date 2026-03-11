"""Finance module — tracks all money in and out.

Includes both the legacy single-entry Finance class (for backward compatibility)
and a full double-entry GeneralLedger for proper accounting compliance.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

from monai.db.database import Database


# ── Legacy Single-Entry (kept for backward compat) ──────────────────


class Finance:
    def __init__(self, db: Database):
        self.db = db

    def get_total_revenue(self, strategy_id: int | None = None,
                          days: int | None = None) -> float:
        query = "SELECT COALESCE(SUM(amount), 0) as total FROM transactions WHERE type = 'revenue'"
        params: list = []
        if strategy_id:
            query += " AND strategy_id = ?"
            params.append(strategy_id)
        if days:
            since = (datetime.now() - timedelta(days=days)).isoformat()
            query += " AND created_at >= ?"
            params.append(since)
        rows = self.db.execute(query, tuple(params))
        return rows[0]["total"]

    def get_total_expenses(self, strategy_id: int | None = None,
                           days: int | None = None) -> float:
        query = "SELECT COALESCE(SUM(amount), 0) as total FROM transactions WHERE type = 'expense'"
        params: list = []
        if strategy_id:
            query += " AND strategy_id = ?"
            params.append(strategy_id)
        if days:
            since = (datetime.now() - timedelta(days=days)).isoformat()
            query += " AND created_at >= ?"
            params.append(since)
        rows = self.db.execute(query, tuple(params))
        return rows[0]["total"]

    def get_net_profit(self, strategy_id: int | None = None,
                       days: int | None = None) -> float:
        return self.get_total_revenue(strategy_id, days) - self.get_total_expenses(strategy_id, days)

    def get_strategy_pnl(self) -> list[dict[str, Any]]:
        rows = self.db.execute("""
            SELECT
                s.id, s.name, s.category,
                COALESCE(SUM(CASE WHEN t.type = 'revenue' THEN t.amount ELSE 0 END), 0) as revenue,
                COALESCE(SUM(CASE WHEN t.type = 'expense' THEN t.amount ELSE 0 END), 0) as expenses,
                COALESCE(SUM(CASE WHEN t.type = 'revenue' THEN t.amount ELSE -t.amount END), 0) as net
            FROM strategies s
            LEFT JOIN transactions t ON t.strategy_id = s.id
            GROUP BY s.id
            ORDER BY net DESC
        """)
        return [dict(r) for r in rows]

    def get_daily_summary(self, date: str | None = None) -> dict[str, float]:
        date = date or datetime.now().strftime("%Y-%m-%d")
        rows = self.db.execute(
            "SELECT type, COALESCE(SUM(amount), 0) as total "
            "FROM transactions WHERE DATE(created_at) = ? GROUP BY type",
            (date,),
        )
        result = {"revenue": 0.0, "expense": 0.0, "net": 0.0}
        for row in rows:
            result[row["type"]] = row["total"]
        result["net"] = result["revenue"] - result["expense"]
        return result

    def get_roi(self, strategy_id: int | None = None, days: int | None = None) -> float:
        expenses = self.get_total_expenses(strategy_id, days)
        if expenses == 0:
            return 0.0
        revenue = self.get_total_revenue(strategy_id, days)
        return revenue / expenses


# ── Double-Entry Bookkeeping ─────────────────────────────────────────


class AccountType(Enum):
    """Standard accounting account types."""
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


# Standard chart of accounts for monAI
DEFAULT_CHART_OF_ACCOUNTS = [
    # Assets
    ("1000", "Cash - Operating", AccountType.ASSET),
    ("1010", "Cash - Stripe", AccountType.ASSET),
    ("1020", "Cash - Gumroad", AccountType.ASSET),
    ("1030", "Cash - LemonSqueezy", AccountType.ASSET),
    ("1040", "Cash - BTCPay", AccountType.ASSET),
    ("1050", "Cash - Monero", AccountType.ASSET),
    ("1060", "Cash - Ko-fi", AccountType.ASSET),
    ("1100", "Accounts Receivable", AccountType.ASSET),
    ("1200", "Prepaid Expenses", AccountType.ASSET),
    # Liabilities
    ("2000", "Accounts Payable", AccountType.LIABILITY),
    ("2100", "Platform Fees Payable", AccountType.LIABILITY),
    ("2200", "Tax Payable - US", AccountType.LIABILITY),
    ("2210", "Tax Payable - IT (IVA)", AccountType.LIABILITY),
    ("2300", "Creator Payable", AccountType.LIABILITY),
    # Equity
    ("3000", "Owner's Equity - Seed Capital", AccountType.EQUITY),
    ("3100", "Retained Earnings", AccountType.EQUITY),
    # Revenue
    ("4000", "Revenue - Services", AccountType.REVENUE),
    ("4100", "Revenue - Digital Products", AccountType.REVENUE),
    ("4200", "Revenue - Subscriptions", AccountType.REVENUE),
    ("4300", "Revenue - Affiliate", AccountType.REVENUE),
    ("4400", "Revenue - Crowdfunding", AccountType.REVENUE),
    ("4900", "Revenue - Other", AccountType.REVENUE),
    # Expenses
    ("5000", "Expense - API Costs (OpenAI)", AccountType.EXPENSE),
    ("5100", "Expense - Hosting & Infrastructure", AccountType.EXPENSE),
    ("5200", "Expense - Platform Fees", AccountType.EXPENSE),
    ("5300", "Expense - Domain Registration", AccountType.EXPENSE),
    ("5400", "Expense - LLC Maintenance", AccountType.EXPENSE),
    ("5500", "Expense - Registered Agent", AccountType.EXPENSE),
    ("5600", "Expense - Marketing", AccountType.EXPENSE),
    ("5700", "Expense - Contractor Payments", AccountType.EXPENSE),
    ("5800", "Expense - Tax Filing", AccountType.EXPENSE),
    ("5900", "Expense - Other", AccountType.EXPENSE),
]


GENERAL_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS gl_accounts (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    account_type TEXT NOT NULL,  -- asset, liability, equity, revenue, expense
    parent_code TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS gl_journal_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_date TEXT NOT NULL,           -- YYYY-MM-DD
    description TEXT NOT NULL,
    reference TEXT,                     -- external ref (payment_ref, invoice_number, etc.)
    source TEXT,                        -- which module created this (sweep_engine, webhook, etc.)
    brand TEXT,
    strategy_id INTEGER,
    is_reconciled INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS gl_journal_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL REFERENCES gl_journal_entries(id),
    account_code TEXT NOT NULL REFERENCES gl_accounts(code),
    debit REAL NOT NULL DEFAULT 0,
    credit REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'EUR',
    memo TEXT,
    CONSTRAINT positive_amounts CHECK (debit >= 0 AND credit >= 0),
    CONSTRAINT one_side CHECK (debit = 0 OR credit = 0)
);

CREATE INDEX IF NOT EXISTS idx_gl_lines_entry ON gl_journal_lines(entry_id);
CREATE INDEX IF NOT EXISTS idx_gl_lines_account ON gl_journal_lines(account_code);
CREATE INDEX IF NOT EXISTS idx_gl_entries_date ON gl_journal_entries(entry_date);
CREATE INDEX IF NOT EXISTS idx_gl_entries_brand ON gl_journal_entries(brand);
CREATE INDEX IF NOT EXISTS idx_gl_entries_ref ON gl_journal_entries(reference);
"""


class GeneralLedger:
    """Double-entry bookkeeping general ledger.

    Every financial event creates a journal entry with balanced debit/credit lines.
    Debits always equal credits — enforced at the database level.

    Account types and their normal balances:
    - Assets: debit increases, credit decreases (normal debit balance)
    - Liabilities: credit increases, debit decreases (normal credit balance)
    - Equity: credit increases, debit decreases (normal credit balance)
    - Revenue: credit increases, debit decreases (normal credit balance)
    - Expenses: debit increases, credit decreases (normal debit balance)
    """

    def __init__(self, db: Database):
        self.db = db
        self._init_schema()
        self._ensure_chart_of_accounts()

    def _init_schema(self) -> None:
        with self.db.connect() as conn:
            conn.executescript(GENERAL_LEDGER_SCHEMA)

    def _ensure_chart_of_accounts(self) -> None:
        """Seed default accounts if the chart is empty."""
        rows = self.db.execute("SELECT COUNT(*) as cnt FROM gl_accounts")
        if rows[0]["cnt"] > 0:
            return
        with self.db.connect() as conn:
            for code, name, acct_type in DEFAULT_CHART_OF_ACCOUNTS:
                conn.execute(
                    "INSERT OR IGNORE INTO gl_accounts (code, name, account_type) "
                    "VALUES (?, ?, ?)",
                    (code, name, acct_type.value),
                )

    # ── Account Management ───────────────────────────────────────

    def add_account(self, code: str, name: str, account_type: AccountType,
                    parent_code: str | None = None) -> None:
        """Add a new account to the chart of accounts."""
        self.db.execute_insert(
            "INSERT INTO gl_accounts (code, name, account_type, parent_code) "
            "VALUES (?, ?, ?, ?)",
            (code, name, account_type.value, parent_code),
        )

    def get_chart_of_accounts(self) -> list[dict[str, Any]]:
        """Return the full chart of accounts."""
        rows = self.db.execute(
            "SELECT * FROM gl_accounts WHERE is_active = 1 ORDER BY code"
        )
        return [dict(r) for r in rows]

    # ── Journal Entry Creation ───────────────────────────────────

    def record_entry(
        self,
        date: str,
        description: str,
        lines: list[dict[str, Any]],
        reference: str = "",
        source: str = "",
        brand: str = "",
        strategy_id: int | None = None,
    ) -> int:
        """Record a balanced journal entry atomically.

        Args:
            date: Entry date (YYYY-MM-DD).
            description: Human-readable description.
            lines: List of dicts with keys: account_code, debit, credit, currency, memo.
                   Each line must have either debit > 0 or credit > 0, not both.
            reference: External reference (payment_ref, invoice number, etc.).
            source: Module that created this entry.
            brand: Brand this entry relates to.
            strategy_id: Strategy ID if applicable.

        Returns:
            The journal entry ID.

        Raises:
            ValueError: If debits don't equal credits or lines are invalid.
        """
        if not lines:
            raise ValueError("Journal entry must have at least one line")

        # Validate balance: total debits must equal total credits
        total_debit = Decimal("0")
        total_credit = Decimal("0")
        for line in lines:
            d = Decimal(str(line.get("debit", 0)))
            c = Decimal(str(line.get("credit", 0)))
            if d < 0 or c < 0:
                raise ValueError(f"Negative amount in journal line: debit={d}, credit={c}")
            if d > 0 and c > 0:
                raise ValueError(f"Line cannot have both debit and credit: {line}")
            total_debit += d
            total_credit += c

        if total_debit != total_credit:
            raise ValueError(
                f"Journal entry not balanced: debits={total_debit}, credits={total_credit}"
            )

        # Record atomically
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO gl_journal_entries "
                "(entry_date, description, reference, source, brand, strategy_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (date, description, reference, source, brand, strategy_id),
            )
            entry_id = cursor.lastrowid

            for line in lines:
                conn.execute(
                    "INSERT INTO gl_journal_lines "
                    "(entry_id, account_code, debit, credit, currency, memo) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        entry_id,
                        line["account_code"],
                        float(line.get("debit", 0)),
                        float(line.get("credit", 0)),
                        line.get("currency", "EUR"),
                        line.get("memo", ""),
                    ),
                )

        return entry_id

    # ── Convenience Methods ──────────────────────────────────────

    def record_revenue(
        self,
        amount: float,
        revenue_account: str,
        cash_account: str,
        description: str,
        currency: str = "EUR",
        **kwargs: Any,
    ) -> int:
        """Record a revenue event (debit cash, credit revenue)."""
        return self.record_entry(
            date=kwargs.get("date", datetime.now().strftime("%Y-%m-%d")),
            description=description,
            lines=[
                {"account_code": cash_account, "debit": amount, "currency": currency},
                {"account_code": revenue_account, "credit": amount, "currency": currency},
            ],
            **{k: v for k, v in kwargs.items() if k != "date"},
        )

    def record_expense(
        self,
        amount: float,
        expense_account: str,
        cash_account: str,
        description: str,
        currency: str = "EUR",
        **kwargs: Any,
    ) -> int:
        """Record an expense (debit expense, credit cash)."""
        return self.record_entry(
            date=kwargs.get("date", datetime.now().strftime("%Y-%m-%d")),
            description=description,
            lines=[
                {"account_code": expense_account, "debit": amount, "currency": currency},
                {"account_code": cash_account, "credit": amount, "currency": currency},
            ],
            **{k: v for k, v in kwargs.items() if k != "date"},
        )

    def record_platform_fee(
        self,
        gross: float,
        fee: float,
        revenue_account: str,
        cash_account: str,
        description: str,
        currency: str = "EUR",
        **kwargs: Any,
    ) -> int:
        """Record revenue with platform fee deduction.

        Creates a 3-line entry:
        - Debit cash (net amount received)
        - Debit platform fee expense
        - Credit revenue (gross amount)
        """
        net = gross - fee
        return self.record_entry(
            date=kwargs.get("date", datetime.now().strftime("%Y-%m-%d")),
            description=description,
            lines=[
                {"account_code": cash_account, "debit": net, "currency": currency},
                {"account_code": "5200", "debit": fee, "currency": currency,
                 "memo": "Platform fee"},
                {"account_code": revenue_account, "credit": gross, "currency": currency},
            ],
            **{k: v for k, v in kwargs.items() if k != "date"},
        )

    def record_sweep(
        self,
        amount: float,
        from_account: str,
        description: str,
        currency: str = "EUR",
        **kwargs: Any,
    ) -> int:
        """Record a profit sweep to creator (debit creator payable, credit cash)."""
        return self.record_entry(
            date=kwargs.get("date", datetime.now().strftime("%Y-%m-%d")),
            description=description,
            lines=[
                {"account_code": "2300", "debit": amount, "currency": currency,
                 "memo": "Creator payout"},
                {"account_code": from_account, "credit": amount, "currency": currency},
            ],
            **{k: v for k, v in kwargs.items() if k != "date"},
        )

    # ── Balance Queries ──────────────────────────────────────────

    def get_account_balance(self, account_code: str) -> float:
        """Get the current balance for an account.

        For asset/expense accounts: balance = total_debits - total_credits
        For liability/equity/revenue accounts: balance = total_credits - total_debits
        """
        rows = self.db.execute(
            "SELECT a.account_type, "
            "COALESCE(SUM(l.debit), 0) as total_debit, "
            "COALESCE(SUM(l.credit), 0) as total_credit "
            "FROM gl_accounts a "
            "LEFT JOIN gl_journal_lines l ON l.account_code = a.code "
            "WHERE a.code = ? "
            "GROUP BY a.code",
            (account_code,),
        )
        if not rows:
            return 0.0

        row = dict(rows[0])
        total_debit = row["total_debit"]
        total_credit = row["total_credit"]
        acct_type = row["account_type"]

        # Normal debit balance accounts
        if acct_type in ("asset", "expense"):
            return total_debit - total_credit
        # Normal credit balance accounts
        return total_credit - total_debit

    def get_trial_balance(self) -> list[dict[str, Any]]:
        """Generate a trial balance — all accounts with their balances.

        If the books are correct, total debits == total credits.
        """
        rows = self.db.execute("""
            SELECT
                a.code,
                a.name,
                a.account_type,
                COALESCE(SUM(l.debit), 0) as total_debit,
                COALESCE(SUM(l.credit), 0) as total_credit
            FROM gl_accounts a
            LEFT JOIN gl_journal_lines l ON l.account_code = a.code
            WHERE a.is_active = 1
            GROUP BY a.code
            HAVING total_debit > 0 OR total_credit > 0
            ORDER BY a.code
        """)
        result = []
        for row in rows:
            r = dict(row)
            if r["account_type"] in ("asset", "expense"):
                r["balance"] = r["total_debit"] - r["total_credit"]
            else:
                r["balance"] = r["total_credit"] - r["total_debit"]
            result.append(r)
        return result

    def get_balance_sheet(self) -> dict[str, Any]:
        """Generate a balance sheet: Assets = Liabilities + Equity."""
        tb = self.get_trial_balance()

        assets = sum(r["balance"] for r in tb if r["account_type"] == "asset")
        liabilities = sum(r["balance"] for r in tb if r["account_type"] == "liability")
        equity = sum(r["balance"] for r in tb if r["account_type"] == "equity")

        # Net income (revenue - expenses) flows into equity
        revenue = sum(r["balance"] for r in tb if r["account_type"] == "revenue")
        expenses = sum(r["balance"] for r in tb if r["account_type"] == "expense")
        net_income = revenue - expenses

        return {
            "assets": round(assets, 2),
            "liabilities": round(liabilities, 2),
            "equity": round(equity, 2),
            "net_income": round(net_income, 2),
            "total_equity": round(equity + net_income, 2),
            "balanced": abs(assets - (liabilities + equity + net_income)) < 0.01,
            "detail": {
                "asset_accounts": [r for r in tb if r["account_type"] == "asset"],
                "liability_accounts": [r for r in tb if r["account_type"] == "liability"],
                "equity_accounts": [r for r in tb if r["account_type"] == "equity"],
            },
        }

    def get_income_statement(self, start_date: str | None = None,
                             end_date: str | None = None) -> dict[str, Any]:
        """Generate an income statement for a period.

        Args:
            start_date: Period start (YYYY-MM-DD). Defaults to beginning of current month.
            end_date: Period end (YYYY-MM-DD). Defaults to today.
        """
        if not start_date:
            start_date = datetime.now().strftime("%Y-%m-01")
        if not end_date:
            end_date = datetime.now().strftime("%Y-%m-%d")

        rows = self.db.execute("""
            SELECT
                a.code,
                a.name,
                a.account_type,
                COALESCE(SUM(l.debit), 0) as total_debit,
                COALESCE(SUM(l.credit), 0) as total_credit
            FROM gl_accounts a
            JOIN gl_journal_lines l ON l.account_code = a.code
            JOIN gl_journal_entries e ON e.id = l.entry_id
            WHERE a.account_type IN ('revenue', 'expense')
              AND e.entry_date >= ? AND e.entry_date <= ?
              AND a.is_active = 1
            GROUP BY a.code
            ORDER BY a.code
        """, (start_date, end_date))

        revenue_lines = []
        expense_lines = []
        total_revenue = 0.0
        total_expenses = 0.0

        for row in rows:
            r = dict(row)
            if r["account_type"] == "revenue":
                balance = r["total_credit"] - r["total_debit"]
                r["balance"] = balance
                revenue_lines.append(r)
                total_revenue += balance
            else:
                balance = r["total_debit"] - r["total_credit"]
                r["balance"] = balance
                expense_lines.append(r)
                total_expenses += balance

        return {
            "period_start": start_date,
            "period_end": end_date,
            "revenue": revenue_lines,
            "expenses": expense_lines,
            "total_revenue": round(total_revenue, 2),
            "total_expenses": round(total_expenses, 2),
            "net_income": round(total_revenue - total_expenses, 2),
        }

    def get_income_statement_normalized(
        self,
        rates: Any,
        target_currency: str = "EUR",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Income statement with all amounts normalized to target currency.

        Args:
            rates: ExchangeRateService instance.
            target_currency: Currency to normalize to (default EUR).
            start_date: Period start (YYYY-MM-DD).
            end_date: Period end (YYYY-MM-DD).
        """
        if not start_date:
            start_date = datetime.now().strftime("%Y-%m-01")
        if not end_date:
            end_date = datetime.now().strftime("%Y-%m-%d")

        rows = self.db.execute("""
            SELECT
                a.code,
                a.name,
                a.account_type,
                l.currency,
                COALESCE(SUM(l.debit), 0) as total_debit,
                COALESCE(SUM(l.credit), 0) as total_credit
            FROM gl_accounts a
            JOIN gl_journal_lines l ON l.account_code = a.code
            JOIN gl_journal_entries e ON e.id = l.entry_id
            WHERE a.account_type IN ('revenue', 'expense')
              AND e.entry_date >= ? AND e.entry_date <= ?
              AND a.is_active = 1
            GROUP BY a.code, l.currency
            ORDER BY a.code
        """, (start_date, end_date))

        revenue_lines: list[dict[str, Any]] = []
        expense_lines: list[dict[str, Any]] = []
        total_revenue = 0.0
        total_expenses = 0.0

        for row in rows:
            r = dict(row)
            currency = r.get("currency", "EUR")
            fx = rates.get_rate(currency, target_currency) if currency != target_currency else 1.0

            if r["account_type"] == "revenue":
                balance = (r["total_credit"] - r["total_debit"]) * fx
                r["balance"] = round(balance, 2)
                r["original_currency"] = currency
                r["fx_rate"] = fx
                revenue_lines.append(r)
                total_revenue += balance
            else:
                balance = (r["total_debit"] - r["total_credit"]) * fx
                r["balance"] = round(balance, 2)
                r["original_currency"] = currency
                r["fx_rate"] = fx
                expense_lines.append(r)
                total_expenses += balance

        return {
            "period_start": start_date,
            "period_end": end_date,
            "target_currency": target_currency,
            "revenue": revenue_lines,
            "expenses": expense_lines,
            "total_revenue": round(total_revenue, 2),
            "total_expenses": round(total_expenses, 2),
            "net_income": round(total_revenue - total_expenses, 2),
        }

    # ── Journal Queries ──────────────────────────────────────────

    def get_journal_entries(self, limit: int = 50,
                           brand: str | None = None) -> list[dict[str, Any]]:
        """Get recent journal entries with their lines."""
        query = "SELECT * FROM gl_journal_entries"
        params: list[Any] = []
        if brand:
            query += " WHERE brand = ?"
            params.append(brand)
        query += " ORDER BY entry_date DESC, id DESC LIMIT ?"
        params.append(limit)

        entries = self.db.execute(query, tuple(params))
        result = []
        for entry in entries:
            e = dict(entry)
            lines = self.db.execute(
                "SELECT l.*, a.name as account_name, a.account_type "
                "FROM gl_journal_lines l "
                "JOIN gl_accounts a ON a.code = l.account_code "
                "WHERE l.entry_id = ? ORDER BY l.id",
                (e["id"],),
            )
            e["lines"] = [dict(l) for l in lines]
            result.append(e)
        return result

    # ── Reconciliation ───────────────────────────────────────────

    def reconcile_entry(self, entry_id: int) -> None:
        """Mark a journal entry as reconciled (verified against external source)."""
        self.db.execute(
            "UPDATE gl_journal_entries SET is_reconciled = 1 WHERE id = ?",
            (entry_id,),
        )

    def get_unreconciled(self, source: str | None = None) -> list[dict[str, Any]]:
        """Get unreconciled journal entries, optionally filtered by source."""
        query = "SELECT * FROM gl_journal_entries WHERE is_reconciled = 0"
        params: list[Any] = []
        if source:
            query += " AND source = ?"
            params.append(source)
        query += " ORDER BY entry_date DESC"
        rows = self.db.execute(query, tuple(params))
        return [dict(r) for r in rows]

    def verify_integrity(self) -> dict[str, Any]:
        """Verify the ledger's integrity — all entries must be balanced."""
        unbalanced = self.db.execute("""
            SELECT
                e.id,
                e.description,
                e.entry_date,
                SUM(l.debit) as total_debit,
                SUM(l.credit) as total_credit
            FROM gl_journal_entries e
            JOIN gl_journal_lines l ON l.entry_id = e.id
            GROUP BY e.id
            HAVING ABS(SUM(l.debit) - SUM(l.credit)) > 0.001
        """)
        unbalanced_list = [dict(r) for r in unbalanced]

        # Check trial balance
        tb = self.get_trial_balance()
        total_debit = sum(r["total_debit"] for r in tb)
        total_credit = sum(r["total_credit"] for r in tb)

        return {
            "balanced": len(unbalanced_list) == 0,
            "unbalanced_entries": unbalanced_list,
            "trial_balance_debit": round(total_debit, 2),
            "trial_balance_credit": round(total_credit, 2),
            "trial_balance_ok": abs(total_debit - total_credit) < 0.01,
        }
