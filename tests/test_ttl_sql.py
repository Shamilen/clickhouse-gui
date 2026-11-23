import unittest

from clickhouse_ttl import expiration, expiration_from_base, parse_ttl_policy, tenant_rules


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


class ReadTTLTests(unittest.TestCase):
    def test_reads_multi_if_retention_and_preserves_original_cast(self):
        ttl = """toDateTime(start_time) + toIntervalDay(multiIf(
            ifNull(toString(tenant) = 'fo_mir', 0), 3,
            ifNull(toString(tenant) = 'jenkins', 0), 3,
            ifNull(toString(tenant) = 'sage', 0), 2, 7))"""

        policy = parse_ttl_policy(ttl)

        self.assertEqual(policy.column, "start_time")
        self.assertEqual(policy.base, "toDateTime(`start_time`)")
        self.assertEqual(policy.default_days, 7)
        self.assertEqual(policy.rules_text, "fo_mir=3\njenkins=3\nsage=2")
        rebuilt = expiration_from_base(policy.base, 8, tenant_rules(policy.rules_text))
        self.assertEqual(parse_ttl_policy(rebuilt).rules, policy.rules)

    def test_reads_formatted_legacy_delete_rules(self):
        ttl = """max_start_time + toIntervalDay(3) WHERE tenant = 'sage',
                 max_start_time + INTERVAL 7 DAY DELETE"""

        policy = parse_ttl_policy(ttl)

        self.assertEqual(policy.column, "max_start_time")
        self.assertEqual(policy.default_days, 7)
        self.assertEqual(policy.rules, {"sage": 3})

    def test_reads_quoted_names_and_single_retention(self):
        rules = {r"a\b(,)=o'hare": 14}
        ttl = expiration("event date", "Date", 7, rules, server_version="25.8.33.6")

        policy = parse_ttl_policy(ttl)

        self.assertEqual(policy.column, "event date")
        self.assertEqual(tenant_rules(policy.rules_text), rules)
        self.assertEqual(parse_ttl_policy("event_date + INTERVAL 3 DAY").default_days, 3)

    def test_rejects_policies_the_form_cannot_reproduce(self):
        for ttl in (
            "event_date + toIntervalDay(3) TO VOLUME 'cold', event_date + toIntervalDay(7)",
            "event_date + toIntervalDay(9) WHERE tenant = 'sage', event_date + toIntervalDay(7)",
            "event_date + toIntervalDay(3) WHERE tenant = 'sage', other_date + toIntervalDay(7)",
            "event_date + toIntervalDay(3) WHERE tenant = 'sage'",
            "event_date + toIntervalDay(3) WHERE tenant = ' ', event_date + toIntervalDay(7)",
            "event_date + toIntervalDay(multiIf(ifNull(toString(tenant) = 'sage', 0), 3, tenant = 'jenkins', 1, 7))",
        ):
            with self.subTest(ttl=ttl):
                self.assertIsNone(parse_ttl_policy(ttl))
