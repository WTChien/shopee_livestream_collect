import tempfile
import unittest
from pathlib import Path

from claim_store import (
    MAX_DAILY_CLAIMS, cancel_claim, confirm_all_pending_estimates, confirm_claim, daily_count,
    record_claim, reserve_claim, summary, unverified_today,
)


class ClaimStoreTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parent / ".tmp"
        temp_root.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=temp_root)
        self.path = Path(self.directory.name) / "claims.sqlite3"

    def tearDown(self):
        self.directory.cleanup()

    def test_pending_only_counts_after_confirmation(self):
        claim_id = reserve_claim("phone-a", "0.42", path=self.path)
        self.assertEqual(daily_count("phone-a", path=self.path), 1)
        self.assertTrue(unverified_today("phone-a", path=self.path))
        self.assertEqual(summary(path=self.path)["today_amount"], 0)
        self.assertTrue(confirm_claim(claim_id, path=self.path))
        report = summary(path=self.path)
        self.assertEqual(report["today_count"], 1)
        self.assertEqual(report["today_amount"], 0.42)
        self.assertEqual(report["history"][0]["amount"], 0.42)
        self.assertFalse(unverified_today("phone-a", path=self.path))

    def test_pending_can_be_cancelled(self):
        claim_id = reserve_claim("phone-a", "0.01", path=self.path)
        self.assertTrue(cancel_claim(claim_id, path=self.path))
        self.assertEqual(daily_count("phone-a", path=self.path), 0)

    def test_success_ocr_amount_replaces_card_estimate(self):
        claim_id = reserve_claim("phone-a", "0.25", path=self.path)
        self.assertTrue(confirm_claim(claim_id, "0.30", path=self.path))
        self.assertEqual(summary(path=self.path)["today_amount"], 0.30)
        self.assertEqual(summary(path=self.path)["recent"][0]["confirmation_method"], "ocr")

    def test_old_pending_clicks_are_counted_at_card_amount(self):
        reserve_claim("phone-a", "0.45", path=self.path)
        reserve_claim("phone-b", "0.30", path=self.path)
        self.assertEqual(confirm_all_pending_estimates(serial="phone-a", path=self.path), 1)
        self.assertEqual(summary(path=self.path)["today_amount"], 0.45)
        self.assertEqual(confirm_all_pending_estimates(path=self.path), 1)
        report = summary(path=self.path)
        self.assertEqual(report["today_amount"], 0.75)
        self.assertEqual(report["pending_count"], 0)
        self.assertEqual({row["confirmation_method"] for row in report["recent"]}, {"estimated"})

    def test_daily_limit_is_ninety_attempts_per_phone(self):
        self.assertEqual(MAX_DAILY_CLAIMS, 90)
        for _ in range(MAX_DAILY_CLAIMS):
            record_claim("phone-a", "0.01", source="manual", path=self.path)
        self.assertEqual(daily_count("phone-a", path=self.path), 90)
        with self.assertRaises(ValueError):
            reserve_claim("phone-a", "0.01", path=self.path)
        record_claim("phone-b", "0.01", source="manual", path=self.path)
        self.assertEqual(daily_count("phone-b", path=self.path), 1)

    def test_summary_can_show_one_device_without_losing_device_totals(self):
        record_claim("phone-a", "0.42", source="manual", path=self.path)
        record_claim("phone-b", "0.30", source="manual", path=self.path)
        reserve_claim("phone-a", "0.25", path=self.path)
        report = summary(serial="phone-a", path=self.path)
        self.assertEqual(report["today_amount"], 0.42)
        self.assertEqual(report["today_count"], 1)
        self.assertEqual(report["pending_count"], 1)
        self.assertEqual(report["pending_by_device"], [{"serial": "phone-a", "count": 1}])
        self.assertEqual(report["history"][0]["amount"], 0.42)
        self.assertTrue(all(item["serial"] == "phone-a" for item in report["recent"]))
        self.assertEqual({item["serial"] for item in report["device_totals"]}, {"phone-a", "phone-b"})


if __name__ == "__main__":
    unittest.main()
