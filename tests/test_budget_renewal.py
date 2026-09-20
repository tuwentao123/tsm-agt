from __future__ import annotations

import unittest

from tsm_agt.core.budget_renewal import DiminishingModelCallBudgetPolicy


class DiminishingModelCallBudgetPolicyTest(unittest.TestCase):
    def test_grants_configured_diminishing_sequence(self) -> None:
        policy = DiminishingModelCallBudgetPolicy(
            increments=(10, 5, 3), max_renewals=3,
            absolute_limit=28, threshold=2,
        )
        limit = 10
        grants = []
        for count in range(3):
            decision = policy.evaluate(
                current_limit=limit, consumed=limit - 1,
                renewal_count=count, required_recoverable_work=True,
            )
            assert decision is not None
            grants.append(decision.increment)
            limit = decision.new_limit

        self.assertEqual(grants, [10, 5, 3])
        self.assertEqual(limit, 28)

    def test_does_not_renew_without_required_recoverable_work(self) -> None:
        decision = DiminishingModelCallBudgetPolicy().evaluate(
            current_limit=40, consumed=40, renewal_count=0,
            required_recoverable_work=False,
        )
        self.assertIsNone(decision)

    def test_does_not_renew_before_threshold(self) -> None:
        decision = DiminishingModelCallBudgetPolicy(threshold=4).evaluate(
            current_limit=40, consumed=35, renewal_count=0,
            required_recoverable_work=True,
        )
        self.assertIsNone(decision)

    def test_caps_the_last_grant_at_the_absolute_limit(self) -> None:
        decision = DiminishingModelCallBudgetPolicy(
            increments=(10,), max_renewals=1,
            absolute_limit=45, threshold=4,
        ).evaluate(
            current_limit=40, consumed=39, renewal_count=0,
            required_recoverable_work=True,
        )
        assert decision is not None
        self.assertEqual(decision.increment, 5)
        self.assertEqual(decision.new_limit, 45)

    def test_stops_after_maximum_renewal_count(self) -> None:
        policy = DiminishingModelCallBudgetPolicy(
            increments=(10, 5, 3), max_renewals=2,
            absolute_limit=60, threshold=4,
        )
        self.assertIsNone(policy.evaluate(
            current_limit=55, consumed=55, renewal_count=2,
            required_recoverable_work=True,
        ))

    def test_rejects_increasing_or_invalid_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not increase"):
            DiminishingModelCallBudgetPolicy(increments=(5, 10))
        with self.assertRaisesRegex(ValueError, "exceeds"):
            DiminishingModelCallBudgetPolicy(
                increments=(10,), max_renewals=2
            )


if __name__ == "__main__":
    unittest.main()
