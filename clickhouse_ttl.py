"""ClickHouse HTTP requests and SQL used by the desktop application."""

from base64 import b64encode
from dataclasses import dataclass
from http.client import HTTPException
from ipaddress import ip_address
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4


SYSTEM_LOGS = (
    "part_log", "processors_profile_log", "trace_log", "metric_log",
    "query_views_log", "query_log", "asynchronous_metric_log", "text_log",
    "query_metric_log", "error_log",
)
QUERY_PREFIX = "ttl-gui-"
TTL_BLOCK = re.compile(r"^TTL\s+(.+?)(?=\n(?:SETTINGS|COMMENT|AS)\b|\Z)", re.M | re.S)
DATE_TYPE = re.compile(r"(?:Date|Date32|DateTime(?:64)?(?:\([^\n]+\))?)")
TENANT_TYPES = re.compile(r"(?:String|FixedString\(\d+\)|Enum(?:8|16)\(.+\))")
SIMPLE_AGGREGATE_TYPE = re.compile(r"SimpleAggregateFunction\([^,]+,\s*(.+)\)")
UNIX_SECOND_TYPES = {"Int32", "UInt32", "Int64", "UInt64"}


class ClickHouseError(RuntimeError):
    """ClickHouse rejected the request or the response could not be verified."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def identifier(name):
    return "`" + name.replace("\\", "\\\\").replace("`", "\\`") + "`"


def literal(value):
    escapes = {"\\": "\\\\", "'": "\\'", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\0": "\\0"}
    return "'" + "".join(escapes.get(char, char) for char in value) + "'"


def ttl_from_ddl(ddl):
    if ddl is None:
        return None
    match = TTL_BLOCK.search(ddl)
    return match.group(1).strip() if match else ""


def retention_days(value):
    if not re.fullmatch(r"[0-9]{1,4}", value.strip()) or not 1 <= int(value) <= 3650:
        raise ValueError("Срок хранения должен быть целым числом от 1 до 3650 дней.")
    return int(value)


def tenant_rules(text):
    rules = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        name, equal, days = line.rpartition("=")
        name = name.strip()
        if not equal or not name or name in rules:
            raise ValueError(f"Строка {number}. Укажите уникальный tenant в формате имя=дни.")
        rules[name] = retention_days(days)
    return rules


def supported_tenant(data_type):
    for wrapper in ("LowCardinality", "Nullable"):
        if data_type.startswith(wrapper + "("):
            data_type = data_type[len(wrapper) + 1:-1]
    return bool(TENANT_TYPES.fullmatch(data_type))


def time_columns(table, columns):
    choices = {}
    for name, kind in columns.items():
        base_type = SIMPLE_AGGREGATE_TYPE.fullmatch(kind)
        data_type = base_type.group(1) if base_type else kind
        if DATE_TYPE.fullmatch(data_type):
            choices[name] = (name, data_type)
        elif table.name == "spans_v5" and name == "start_time" and kind in UNIX_SECOND_TYPES:
            choices[f"{name} (Unix-секунды)"] = (name, kind)
    return choices


def expiration(column, data_type, default_days, rules, *, server_version):
    base = identifier(column)
    version_match = re.match(r"^([0-9]+)\.([0-9]+)(?:\.|$)", server_version)
    version = tuple(map(int, version_match.groups())) if version_match else (0, 0)
    # ClickHouse 25.6 added Date32 and DateTime64 as valid TTL result types.
    if version >= (25, 6):
        if data_type in ("Date", "Date32") or data_type in UNIX_SECOND_TYPES:
            base = f"toDateTime64({base}, 0)"
    elif data_type == "Date32" or data_type.startswith("DateTime64") or data_type in UNIX_SECOND_TYPES:
        base = f"toDateTime({base})"
    if not rules:
        return f"{base} + toIntervalDay({default_days})"
    # A single DELETE deadline lets an exception also extend retention.
    arguments = []
    for tenant, days in rules.items():
        arguments.extend((f"ifNull(toString(tenant) = {literal(tenant)}, 0)", str(days)))
    arguments.append(str(default_days))
    return f"{base} + toIntervalDay(multiIf(\n    " + ",\n    ".join(arguments) + "\n))"


@dataclass(frozen=True)
class Table:
    database: str
    name: str
    engine: str
    uuid: str
    host: str
    ddl: str
    formatted_ddl: str | None
    size: int | None
    rows: int | None

    @classmethod
    def from_row(cls, row):
        return cls(
            row["database"], row["name"], row["engine"], row["uuid"], row["host"],
            row["ddl"], row["formatted_ddl"],
            int(row["size"]) if row["size"] is not None else None,
            int(row["rows"]) if row["rows"] is not None else None,
        )

    @property
    def sql_name(self):
        return f"{identifier(self.database)}.{identifier(self.name)}"

    @property
    def title(self):
        return f"{self.database}.{self.name}"

    @property
    def merge_tree(self):
        return self.engine.endswith("MergeTree")

    @property
    def ttl(self):
        return ttl_from_ddl(self.formatted_ddl)

    def same_definition(self, other):
        return (self.database, self.name, self.host, self.uuid, self.ddl) == (
            other.database, other.name, other.host, other.uuid, other.ddl
        )


class ClickHouse:
    def __init__(self, url, username, password):
        self.url = url.strip()
        parsed = urlsplit(self.url)
        if (
            parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.query or parsed.fragment
        ):
            raise ValueError("Введите HTTP(S)-адрес ClickHouse без логина, пароля и параметров URL.")
        if ":" in username or not username.strip():
            raise ValueError("Введите имя пользователя ClickHouse без двоеточия.")
        self.authorization = "Basic " + b64encode(f"{username}:{password}".encode()).decode()
        handlers = [NoRedirect()]
        if is_loopback(parsed.hostname):
            handlers.append(ProxyHandler({}))
        self.opener = build_opener(*handlers)

    def _request(self, sql, params=None, settings=None, write=False):
        query_id = QUERY_PREFIX + uuid4().hex
        fields = {"query_id": query_id, "wait_end_of_query": 1}
        fields.update(settings or {})
        fields.update({f"param_{key}": value for key, value in (params or {}).items()})
        request = Request(
            f"{self.url}?{urlencode(fields)}", data=sql.encode(),
            headers={"Authorization": self.authorization, "Content-Type": "text/plain; charset=utf-8"},
        )
        try:
            with self.opener.open(request, timeout=60 if write else 35) as response:
                body = response.read().decode("utf-8", errors="replace")
                if response.headers.get("X-ClickHouse-Exception-Code", "0") != "0":
                    raise ClickHouseError(f"{body.strip()}\nquery_id: {query_id}")
        except HTTPError as error:
            with error:
                details = error.read(16_384).decode("utf-8", errors="replace").strip()
                rejected = error.headers.get("X-ClickHouse-Exception-Code") or re.match(r"Code: \d+\.", details)
            result = " Исход команды проверьте на сервере." if write and not rejected else ""
            raise ClickHouseError(f"HTTP {error.code}.{result}\n{details}\nquery_id: {query_id}") from error
        except (URLError, OSError, HTTPException) as error:
            detail = " Исход команды неизвестен, проверьте её на сервере перед повтором." if write else ""
            raise ClickHouseError(f"Ошибка соединения.{detail}\n{error}\nquery_id: {query_id}") from error
        return body, query_id

    def select(self, sql, params=None, settings=None):
        raw, query_id = self._request(sql + "\nFORMAT JSONEachRow", params, settings)
        try:
            return [json.loads(line) for line in raw.split("\n") if line.strip()]
        except json.JSONDecodeError as error:
            raise ClickHouseError(f"ClickHouse вернул неожиданный ответ.\n{raw[:1500]}\nquery_id: {query_id}") from error

    def change(self, sql, settings):
        body, query_id = self._request(sql, settings=settings, write=True)
        if body.strip():
            raise ClickHouseError(f"Неожиданный ответ на ALTER. Проверьте результат на сервере.\n{body[:1500]}\nquery_id: {query_id}")
        return query_id

    def server(self):
        server, = self.select("SELECT version() AS version, hostName() AS host, timezone() AS timezone")
        return server

    def tables(self, database, name=None):
        if name is not None:
            predicate = "database = {db:String} AND name = {name:String}"
        else:
            logs = literal("^(" + "|".join(SYSTEM_LOGS) + ")(_[0-9]+)?$")
            predicate = f"database = {{db:String}} OR (database = 'system' AND match(name, {logs}))"
        rows = self.select(
            "SELECT database, name, engine, toString(uuid) AS uuid, hostName() AS host,\n"
            "       create_table_query AS ddl, formatQueryOrNull(create_table_query) AS formatted_ddl,\n"
            "       total_bytes AS size, total_rows AS rows\n"
            f"FROM system.tables WHERE ({predicate})\n"
            "ORDER BY size DESC NULLS LAST, database, name",
            {"db": database, "name": name or ""},
        )
        return [Table.from_row(row) for row in rows]

    def columns(self, table):
        rows = self.select(
            "SELECT name, type FROM system.columns\n"
            "WHERE database = {db:String} AND table = {table:String} ORDER BY position",
            {"db": table.database, "table": table.name},
        )
        return {row["name"]: row["type"] for row in rows}

    def partitions(self, table):
        return self.select(
            "SELECT partition_id, any(partition) AS label, sum(bytes_on_disk) AS size, sum(rows) AS rows,\n"
            "       toString(min(delete_ttl_info_min)) AS ttl_min, toString(max(delete_ttl_info_max)) AS ttl_max\n"
            "FROM system.parts WHERE active AND database = {db:String} AND table = {table:String}\n"
            "GROUP BY partition_id ORDER BY partition_id DESC",
            {"db": table.database, "table": table.name},
        )

    def estimate(self, table, expression, partition):
        part_filter = "AND _partition_id = {partition:String}" if partition is not None else ""
        rows = self.select(
            f"SELECT count() AS rows FROM {table.sql_name}\nWHERE ({expression}) <= now() {part_filter}",
            {"partition": partition or ""},
            {"max_execution_time": 25, "max_bytes_to_read": 2 * 1024 ** 3, "read_overflow_mode": "throw"},
        )
        return int(rows[0]["rows"])

    def mutations(self, table):
        return self.select(
            "SELECT mutation_id, create_time, command, parts_to_do, is_done, latest_fail_reason\n"
            "FROM system.mutations WHERE database = {db:String} AND table = {table:String}\n"
            "ORDER BY is_done ASC, create_time DESC, mutation_id DESC LIMIT 100",
            {"db": table.database, "table": table.name},
        )

    def processes(self, pattern):
        return self.select(
            "SELECT query_id, user, round(elapsed, 2) AS elapsed, is_cancelled, leftUTF8(query, 2000) AS query\n"
            f"FROM system.processes WHERE NOT startsWith(query_id, {literal(QUERY_PREFIX)})\n"
            "AND positionCaseInsensitive(query, {filter:String}) > 0\n"
            "ORDER BY elapsed DESC LIMIT 100",
            {"filter": pattern},
        )

    def assert_current(self, snapshot):
        tables = self.tables(snapshot.database, snapshot.name)
        if not tables or not snapshot.same_definition(tables[0]):
            raise ClickHouseError("Узел или определение таблицы изменились. Обновите список и проверьте SQL заново.")


def modify_sql(table, expression):
    return f"ALTER TABLE {table.sql_name}\nMODIFY TTL {expression}"


def materialize_sql(table, partition):
    scope = f" IN PARTITION ID {literal(partition)}" if partition is not None else ""
    return f"ALTER TABLE {table.sql_name}\nMATERIALIZE TTL{scope}"
