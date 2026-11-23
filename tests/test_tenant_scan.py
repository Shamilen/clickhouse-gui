import unittest
from unittest.mock import patch

from clickhouse_ttl import GIB, ClickHouse, ClickHouseError, Table, tenant_scan_limits


class TenantScanTests(unittest.TestCase):
    def test_accepts_fractional_size_and_zero_for_unlimited_reads(self):
        self.assertEqual(tenant_scan_limits("1,5", "120"),
                         {"max_bytes_to_read": 3 * GIB // 2, "max_execution_time": 120})
        self.assertEqual(tenant_scan_limits("0", "25")["max_bytes_to_read"], 0)

    def test_rejects_invalid_limits(self):
        for size, seconds in (("-1", "25"), ("NaN", "25"), ("0.00000000001", "25"), ("2", "0")):
            with self.subTest(size=size, seconds=seconds), self.assertRaises(ValueError):
                tenant_scan_limits(size, seconds)

    def test_custom_budget_reaches_clickhouse_and_extends_http_timeout(self):
        client = ClickHouse("http://localhost:8123", "default", "")
        table = Table("dtracing", "spans_v5", "MergeTree", "", "", "", None, None, None)
        with patch.object(client, "select", return_value=[]) as select:
            client.tenant_sizes(table, **tenant_scan_limits("0", "120"))

        self.assertEqual(select.call_args.args[2]["max_bytes_to_read"], 0)
        self.assertEqual(select.call_args.args[2]["max_execution_time"], 120)
        self.assertGreater(select.call_args.kwargs["timeout"], 120)

    def test_read_limit_error_preserves_code_and_query_id(self):
        client = ClickHouse("http://localhost:8123", "default", "")
        table = Table("dtracing", "spans_v5", "MergeTree", "", "", "", None, None, None)
        with patch.object(client, "select", side_effect=ClickHouseError("TOO_MANY_BYTES", code=307, query_id="scan")):
            with self.assertRaisesRegex(ClickHouseError, "Лимит чтения, ГиБ") as error:
                client.tenant_sizes(table)
        self.assertEqual(error.exception.code, 307)
        self.assertEqual(error.exception.query_id, "scan")
