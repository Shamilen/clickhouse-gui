import unittest

from clickhouse_ttl import expiration


class ExpirationTests(unittest.TestCase):
    def test_system_log_date_uses_date_on_clickhouse_25_3(self):
        expression = expiration("event_date", "Date", 2, {}, server_version="25.3.6.1")

        self.assertEqual(expression, "`event_date` + toIntervalDay(2)")

    def test_legacy_server_uses_datetime_for_wider_types(self):
        for data_type in ("Date32", "DateTime64(3, 'UTC')", "UInt32"):
            with self.subTest(data_type=data_type):
                expression = expiration("start_time", data_type, 7, {"sage": 3}, server_version="25.3.6.1")

                self.assertTrue(expression.startswith("toDateTime(`start_time`) + toIntervalDay(multiIf("))
                self.assertIn("ifNull(toString(tenant) = 'sage', 0)", expression)

    def test_extended_results_are_preserved_from_clickhouse_25_6(self):
        date = expiration("event_date", "Date", 2, {}, server_version="25.6.1.3206")
        datetime64 = expiration("start_time", "DateTime64(3, 'UTC')", 7, {}, server_version="25.8.33.6")

        self.assertEqual(date, "toDateTime64(`event_date`, 0) + toIntervalDay(2)")
        self.assertEqual(datetime64, "`start_time` + toIntervalDay(7)")
