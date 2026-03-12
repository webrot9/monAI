"""Tests for the Alerting Rules Engine.

Covers:
- Default rule installation
- Rule CRUD (add, update, delete)
- Metric collection from dashboard data
- Rule evaluation with operator checks
- Cooldown deduplication
- Alert history tracking
"""

import pytest

from monai.business.alerting import AlertingEngine


@pytest.fixture
def alerting(db):
    return AlertingEngine(db)


class TestDefaultRules:
    def test_default_rules_installed(self, alerting):
        """Default rules should be installed on first init."""
        rules = alerting.get_rules()
        assert len(rules) >= 3
        names = {r["name"] for r in rules}
        assert "budget_critical" in names
        assert "budget_low" in names
        assert "daily_revenue" in names

    def test_defaults_not_duplicated(self, db):
        """Re-initializing should not duplicate default rules."""
        engine1 = AlertingEngine(db)
        count1 = len(engine1.get_rules())
        engine2 = AlertingEngine(db)
        count2 = len(engine2.get_rules())
        assert count1 == count2


class TestRuleCRUD:
    def test_add_rule(self, alerting):
        rule_id = alerting.add_rule(
            name="test_rule",
            metric="budget.balance",
            operator="lt",
            threshold=100.0,
            severity="warning",
        )
        assert rule_id > 0
        rules = alerting.get_rules()
        test_rule = [r for r in rules if r["name"] == "test_rule"]
        assert len(test_rule) == 1
        assert test_rule[0]["threshold"] == 100.0

    def test_update_rule(self, alerting):
        rule_id = alerting.add_rule(
            name="update_test",
            metric="budget.balance",
            operator="lt",
            threshold=50.0,
        )
        alerting.update_rule(rule_id, threshold=200.0, severity="critical")
        rules = alerting.get_rules()
        updated = [r for r in rules if r["id"] == rule_id][0]
        assert updated["threshold"] == 200.0
        assert updated["severity"] == "critical"

    def test_delete_rule(self, alerting):
        rule_id = alerting.add_rule(
            name="delete_test",
            metric="x.y",
            operator="gt",
            threshold=0,
        )
        alerting.delete_rule(rule_id)
        rules = alerting.get_rules()
        assert not any(r["id"] == rule_id for r in rules)

    def test_invalid_operator_rejected(self, alerting):
        with pytest.raises(ValueError, match="Unknown operator"):
            alerting.add_rule(
                name="bad_op",
                metric="x",
                operator="like",
                threshold=0,
            )


class TestMetricCollection:
    def test_extracts_nested_metrics(self, alerting):
        data = {
            "budget": {"balance": 42.5, "days_until_broke": 7},
            "today": {"revenue": 10.0, "net": 5.0},
        }
        metrics = alerting.collect_metrics(data)
        assert metrics["budget.balance"] == 42.5
        assert metrics["budget.days_until_broke"] == 7.0
        assert metrics["today.revenue"] == 10.0

    def test_skips_non_numeric(self, alerting):
        data = {"budget": {"status": "ok", "balance": 100}}
        metrics = alerting.collect_metrics(data)
        assert "budget.status" not in metrics
        assert "budget.balance" in metrics


class TestRuleEvaluation:
    def test_fires_on_threshold_breach(self, alerting):
        # Disable defaults to isolate test
        for r in alerting.get_rules():
            alerting.update_rule(r["id"], enabled=0)

        alerting.add_rule(
            name="low_balance_test",
            metric="budget.balance",
            operator="lt",
            threshold=100.0,
            severity="critical",
            cooldown_minutes=0,
            message_template="Balance is €{value:.2f}, below €{threshold:.2f}",
        )

        data = {"budget": {"balance": 50.0}}
        fired = alerting.evaluate(data)
        assert len(fired) == 1
        assert fired[0]["rule_name"] == "low_balance_test"
        assert fired[0]["severity"] == "critical"
        assert "50.00" in fired[0]["message"]

    def test_does_not_fire_when_within_threshold(self, alerting):
        for r in alerting.get_rules():
            alerting.update_rule(r["id"], enabled=0)

        alerting.add_rule(
            name="safe_balance",
            metric="budget.balance",
            operator="lt",
            threshold=100.0,
            cooldown_minutes=0,
        )

        data = {"budget": {"balance": 200.0}}
        fired = alerting.evaluate(data)
        assert len(fired) == 0

    def test_gt_operator(self, alerting):
        for r in alerting.get_rules():
            alerting.update_rule(r["id"], enabled=0)

        alerting.add_rule(
            name="high_revenue",
            metric="today.revenue",
            operator="gt",
            threshold=0,
            cooldown_minutes=0,
        )

        data = {"today": {"revenue": 50.0}}
        fired = alerting.evaluate(data)
        assert len(fired) == 1

    def test_cooldown_prevents_spam(self, alerting):
        for r in alerting.get_rules():
            alerting.update_rule(r["id"], enabled=0)

        alerting.add_rule(
            name="cooldown_test",
            metric="budget.balance",
            operator="lt",
            threshold=100.0,
            cooldown_minutes=60,
        )

        data = {"budget": {"balance": 50.0}}

        # First evaluation should fire
        fired1 = alerting.evaluate(data)
        assert len(fired1) == 1

        # Second evaluation within cooldown should NOT fire
        fired2 = alerting.evaluate(data)
        assert len(fired2) == 0

    def test_disabled_rules_skipped(self, alerting):
        for r in alerting.get_rules():
            alerting.update_rule(r["id"], enabled=0)

        rule_id = alerting.add_rule(
            name="disabled_test",
            metric="budget.balance",
            operator="lt",
            threshold=999.0,
            cooldown_minutes=0,
        )
        alerting.update_rule(rule_id, enabled=0)

        data = {"budget": {"balance": 1.0}}
        fired = alerting.evaluate(data)
        assert len(fired) == 0

    def test_missing_metric_skipped(self, alerting):
        for r in alerting.get_rules():
            alerting.update_rule(r["id"], enabled=0)

        alerting.add_rule(
            name="missing_metric",
            metric="nonexistent.metric",
            operator="gt",
            threshold=0,
            cooldown_minutes=0,
        )

        data = {"budget": {"balance": 100.0}}
        fired = alerting.evaluate(data)
        assert len(fired) == 0


class TestAlertHistory:
    def test_history_recorded(self, alerting):
        for r in alerting.get_rules():
            alerting.update_rule(r["id"], enabled=0)

        alerting.add_rule(
            name="history_test",
            metric="budget.balance",
            operator="lt",
            threshold=100.0,
            cooldown_minutes=0,
        )

        alerting.evaluate({"budget": {"balance": 10.0}})
        history = alerting.get_recent_alerts(10)
        assert len(history) >= 1
        assert history[0]["rule_name"] == "history_test"
        assert history[0]["metric_value"] == 10.0

    def test_mark_delivered(self, alerting):
        for r in alerting.get_rules():
            alerting.update_rule(r["id"], enabled=0)

        alerting.add_rule(
            name="deliver_test",
            metric="budget.balance",
            operator="lt",
            threshold=100.0,
            cooldown_minutes=0,
        )
        alerting.evaluate({"budget": {"balance": 5.0}})

        history = alerting.get_recent_alerts(1)
        assert history[0]["delivered"] == 0

        alerting.mark_delivered(history[0]["id"])
        history = alerting.get_recent_alerts(1)
        assert history[0]["delivered"] == 1

    def test_summary(self, alerting):
        for r in alerting.get_rules():
            alerting.update_rule(r["id"], enabled=0)

        alerting.add_rule(
            name="sum_test",
            metric="budget.balance",
            operator="lt",
            threshold=100.0,
            severity="critical",
            cooldown_minutes=0,
        )
        alerting.evaluate({"budget": {"balance": 5.0}})

        summary = alerting.get_summary(days=1)
        assert summary["total"] >= 1
        assert "critical" in summary["by_severity"]
