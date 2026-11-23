"""Desktop GUI for viewing and managing ClickHouse retention."""

import os
from queue import Empty, Queue
from threading import Thread
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from clickhouse_ttl import (
    ClickHouse, ClickHouseError, expiration, materialize_sql,
    modify_sql, retention_days, supported_tenant, tenant_rules, time_columns,
)


BG = "#eef2f7"
PANEL = "#ffffff"
INK = "#172b42"
MUTED = "#4d6278"
BLUE = "#245ac5"
EDGE = "#d4deeb"
NAVY = "#13273f"


def size_label(value):
    if value is None:
        return "—"
    size = int(value)
    for unit in ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ", "ПиБ"):
        if size < 1024 or unit == "ПиБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024


def count_label(value):
    return "—" if value is None else f"{int(value):,}".replace(",", " ")


def set_text(widget, value):
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", value)
    widget.configure(state="disabled")


def text_panel(parent, height, *, editable=False):
    widget = ScrolledText(
        parent, height=height, wrap="word", font="TkFixedFont", relief="flat",
        padx=10, pady=8, background="#f4f7fb", foreground=INK,
        highlightthickness=1, highlightbackground=EDGE,
    )
    if not editable:
        widget.configure(state="disabled")
    return widget


def tree_panel(parent, specs, height=8):
    wrap = ttk.Frame(parent, style="Panel.TFrame")
    wrap.rowconfigure(0, weight=1)
    wrap.columnconfigure(0, weight=1)
    tree = ttk.Treeview(wrap, columns=[spec[0] for spec in specs], show="headings", height=height)
    ybar = ttk.Scrollbar(wrap, command=tree.yview)
    xbar = ttk.Scrollbar(wrap, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set, selectmode="browse")
    tree.grid(row=0, column=0, sticky="nsew")
    ybar.grid(row=0, column=1, sticky="ns")
    xbar.grid(row=1, column=0, sticky="ew")
    for key, label, width in specs:
        tree.heading(key, text=label)
        tree.column(key, width=width, minwidth=45, stretch=key == specs[-1][0])
    tree.tag_configure("alternate", background="#f5f8fc")
    return wrap, tree


def fill_tree(tree, rows):
    tree.delete(*tree.get_children())
    for index, values in enumerate(rows):
        tree.insert("", "end", iid=str(index), values=values, tags=("alternate",) if index % 2 else ())


def selected_index(tree):
    selection = tree.selection()
    return int(selection[0]) if selection else None


def configure_styles(root):
    root.configure(bg=BG)
    style = ttk.Style()
    style.theme_use("clam")
    style.configure(".", font="TkDefaultFont", background=BG, foreground=INK)
    for theme in ("TFrame", "TLabel", "TNotebook", "TCheckbutton"):
        style.configure("Panel." + theme, background=PANEL)
    style.configure("Title.TLabel", background=PANEL, font=("TkDefaultFont", 15, "bold"))
    style.configure("Hint.TLabel", background=PANEL, foreground=MUTED)
    style.configure("TButton", padding=(10, 6))
    style.configure("Accent.TButton", background=BLUE, foreground="#ffffff", bordercolor=BLUE)
    style.map("Accent.TButton", background=[("disabled", "#afbed6"), ("active", "#1b479d")],
              foreground=[("disabled", "#ffffff")])
    style.configure("Treeview", rowheight=27, background=PANEL, fieldbackground=PANEL, bordercolor=EDGE)
    style.configure("Treeview.Heading", background="#e8eff8", foreground=INK, padding=(5, 6))
    style.map("Treeview", background=[("selected", "#dceaff")], foreground=[("selected", "#122f5d")])
    style.configure("TNotebook", borderwidth=0)
    style.configure("TNotebook.Tab", padding=(12, 6))
    style.map("TNotebook.Tab", background=[("selected", PANEL), ("!selected", "#e2e9f3")])
    style.configure("TEntry", padding=5)
    style.configure("TCombobox", padding=4)
    style.configure("Status.TLabel", foreground=MUTED)


class App:
    def __init__(self, root):
        self.root = root
        self.client = None
        self.database = ""
        self.server = None
        self.tables = []
        self.columns = {}
        self.partitions = []
        self.mutations = []
        self.processes = []
        self.table = None
        self.editor = None
        self.busy = False
        self.results = Queue()

        root.title("ClickHouse TTL")
        root.geometry("1230x850")
        root.minsize(960, 680)
        configure_styles(root)
        self.build()
        self.update_actions()
        root.after(60, self.check_results)

    def build(self):
        header = tk.Frame(self.root, bg=NAVY, padx=20, pady=12)
        header.pack(fill="x")
        tk.Label(header, text="ClickHouse TTL", bg=NAVY, fg="#ffffff",
                 font=("TkDefaultFont", 20, "bold")).pack(side="left")
        self.connection_status = tk.Label(header, text="Нет подключения", bg=NAVY, fg="#d5e3f7", padx=6)
        self.connection_status.pack(side="right")

        connection = ttk.Frame(self.root, padding=(14, 10), style="Panel.TFrame")
        connection.pack(fill="x", padx=14, pady=(12, 8))
        self.url = tk.StringVar(value=os.environ.get("CLICKHOUSE_URL", "http://127.0.0.1:8123"))
        self.user = tk.StringVar(value="default")
        self.password = tk.StringVar()
        self.db = tk.StringVar(value="dtracing")
        for index, (label, variable, width, mask) in enumerate((
            ("HTTP(S)-адрес", self.url, 30, ""),
            ("Пользователь", self.user, 13, ""),
            ("Пароль", self.password, 13, "•"),
            ("База данных", self.db, 14, ""),
        )):
            ttk.Label(connection, text=label, style="Hint.TLabel").grid(row=0, column=index, sticky="w")
            field = ttk.Entry(connection, textvariable=variable, width=width, show=mask)
            field.grid(row=1, column=index, padx=(0, 10), sticky="ew")
            field.bind("<Return>", lambda _: self.connect())
        connection.columnconfigure(0, weight=1)
        self.connect_button = ttk.Button(connection, text="Подключиться", command=self.connect, style="Accent.TButton")
        self.connect_button.grid(row=1, column=4, sticky="e")

        pages = ttk.Notebook(self.root)
        pages.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        manage = ttk.Frame(pages, padding=9, style="Panel.TFrame")
        activity = ttk.Frame(pages, padding=12, style="Panel.TFrame")
        pages.add(manage, text="Таблицы и TTL")
        pages.add(activity, text="Активные запросы")
        self.build_manage(manage)
        self.build_activity(activity)

        self.status = tk.StringVar(value="Подключитесь к ClickHouse, чтобы загрузить таблицы.")
        ttk.Label(self.root, textvariable=self.status, style="Status.TLabel", anchor="w").pack(
            fill="x", padx=16, pady=(0, 10),
        )

    def build_manage(self, parent):
        pane = ttk.Panedwindow(parent, orient="horizontal")
        pane.pack(fill="both", expand=True)
        inventory = ttk.Frame(pane, padding=(2, 2, 8, 2), style="Panel.TFrame")
        detail = ttk.Frame(pane, padding=(8, 2, 2, 2), style="Panel.TFrame")
        pane.add(inventory, weight=3)
        pane.add(detail, weight=4)

        bar = ttk.Frame(inventory, style="Panel.TFrame")
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="Таблицы", style="Title.TLabel").pack(side="left")
        self.refresh_button = ttk.Button(bar, text="Обновить", command=self.refresh_tables)
        self.refresh_button.pack(side="right")
        self.filter = tk.StringVar()
        search = ttk.Frame(inventory, style="Panel.TFrame")
        search.pack(fill="x", pady=(0, 9))
        ttk.Label(search, text="Фильтр", style="Hint.TLabel").pack(side="left", padx=(0, 8))
        ttk.Entry(search, textvariable=self.filter).pack(side="left", fill="x", expand=True)
        self.filter.trace_add("write", lambda *_: self.show_tables())

        grid, self.table_tree = tree_panel(inventory, (
            ("table", "Таблица", 204), ("size", "Размер", 82), ("rows", "Строки", 80), ("ttl", "TTL", 58),
        ))
        grid.pack(fill="both", expand=True)
        self.table_tree.bind("<<TreeviewSelect>>", self.select_table)

        self.table_title = ttk.Label(detail, text="Выберите таблицу", style="Title.TLabel")
        self.table_title.pack(anchor="w")
        self.table_summary = ttk.Label(detail, style="Hint.TLabel")
        self.table_summary.pack(anchor="w", pady=(1, 7))
        tabs = ttk.Notebook(detail, style="Panel.TNotebook")
        self.detail_tabs = tabs
        tabs.pack(fill="both", expand=True)
        policy = ttk.Frame(tabs, padding=10, style="Panel.TFrame")
        parts = ttk.Frame(tabs, padding=10, style="Panel.TFrame")
        mutations = ttk.Frame(tabs, padding=10, style="Panel.TFrame")
        ddl = ttk.Frame(tabs, padding=10, style="Panel.TFrame")
        self.tenant_tab = ttk.Frame(tabs, padding=10, style="Panel.TFrame")
        tabs.add(policy, text="TTL")
        tabs.add(parts, text="Партиции")
        tabs.add(self.tenant_tab, text="Тенанты", state="hidden")
        tabs.add(mutations, text="Мутации")
        tabs.add(ddl, text="DDL")

        ttk.Label(policy, text="Действующий TTL таблицы", style="Hint.TLabel").pack(anchor="w", pady=(0, 5))
        self.current_ttl = text_panel(policy, 6)
        self.current_ttl.pack(fill="both", expand=True)
        self.system_note = ttk.Label(policy, style="Hint.TLabel", wraplength=470, justify="left")
        self.system_note.pack(anchor="w", pady=8)
        self.edit_button = ttk.Button(policy, text="Настроить TTL", command=self.open_editor, style="Accent.TButton")
        self.edit_button.pack(anchor="w")

        actions = ttk.Frame(parts, style="Panel.TFrame")
        actions.pack(fill="x", pady=(0, 8))
        self.parts_button = ttk.Button(actions, text="Загрузить партиции", command=self.load_partitions)
        self.parts_button.pack(side="left")
        self.clean_part_button = ttk.Button(actions, text="Очистить выбранную", command=lambda: self.clean(False))
        self.clean_part_button.pack(side="left", padx=5)
        self.clean_table_button = ttk.Button(actions, text="Очистить всю таблицу по TTL", command=lambda: self.clean(True))
        self.clean_table_button.pack(side="left")
        grid, self.part_tree = tree_panel(parts, (
            ("id", "ID партиции", 105), ("partition", "Партиция", 90), ("size", "Размер", 79),
            ("rows", "Строки", 78), ("min", "Мин. TTL", 134), ("max", "Макс. TTL", 134),
        ))
        grid.pack(fill="both", expand=True)
        self.part_tree.bind("<<TreeviewSelect>>", lambda _: self.update_actions())

        self.mutation_button = ttk.Button(mutations, text="Обновить мутации", command=self.load_mutations)
        self.mutation_button.pack(anchor="w", pady=(0, 8))
        grid, self.mutation_tree = tree_panel(mutations, (
            ("id", "ID мутации", 116), ("time", "Создана", 136), ("pending", "Части", 64), ("status", "Состояние", 125),
        ))
        grid.pack(fill="both", expand=True)
        self.mutation_detail = text_panel(mutations, 5)
        self.mutation_detail.pack(fill="x", pady=(8, 0))
        self.mutation_tree.bind("<<TreeviewSelect>>", self.show_mutation)
        self.ddl = text_panel(ddl, 16)
        self.ddl.pack(fill="both", expand=True)
        self.build_tenants(self.tenant_tab)

    def build_tenants(self, parent):
        controls = ttk.Frame(parent, style="Panel.TFrame")
        controls.pack(fill="x", pady=(0, 8))
        ttk.Label(controls, text="Область", style="Hint.TLabel").pack(side="left", padx=(0, 8))
        self.tenant_scope = ttk.Combobox(controls, values=("Вся таблица",), state="readonly", width=15)
        self.tenant_scope.current(0)
        self.tenant_scope.pack(side="left")
        self.tenant_scope.bind("<<ComboboxSelected>>", lambda _: self.reset_tenant_sizes())
        self.tenant_parts_button = ttk.Button(controls, text="Загрузить партиции", command=self.load_partitions)
        self.tenant_parts_button.pack(side="left", padx=6)
        self.tenant_size_button = ttk.Button(
            controls, text="Посчитать", command=self.load_tenant_sizes, style="Accent.TButton",
        )
        self.tenant_size_button.pack(side="left")

        self.measure_tenant_bytes = tk.BooleanVar()
        self.tenant_measure_button = ttk.Checkbutton(
            parent, text="Считать объём без сжатия", variable=self.measure_tenant_bytes,
            command=self.reset_tenant_sizes, style="Panel.TCheckbutton",
        )
        self.tenant_measure_button.pack(anchor="w")
        self.tenant_method = ttk.Label(parent, style="Hint.TLabel", wraplength=430, justify="left")
        self.tenant_method.pack(anchor="w", pady=(2, 8))
        grid, self.tenant_tree = tree_panel(parent, (
            ("tenant", "Tenant", 130), ("rows", "Строки", 82),
            ("data", "Без сжатия", 110), ("disk", "На диске, оценка", 132),
        ))
        grid.pack(fill="both", expand=True)
        self.tenant_summary = ttk.Label(parent, style="Hint.TLabel", wraplength=430, justify="left")
        self.tenant_summary.pack(anchor="w", pady=(6, 0))
        self.reset_tenant_sizes()

    def build_activity(self, parent):
        bar = ttk.Frame(parent, style="Panel.TFrame")
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="Запрос содержит", style="Hint.TLabel").pack(side="left", padx=(0, 8))
        self.query_filter = tk.StringVar(value="v5")
        ttk.Entry(bar, textvariable=self.query_filter, width=27).pack(side="left", padx=(0, 8))
        self.process_button = ttk.Button(bar, text="Обновить запросы", command=self.load_processes)
        self.process_button.pack(side="left")
        grid, self.process_tree = tree_panel(parent, (
            ("id", "Query ID", 247), ("user", "Пользователь", 112), ("seconds", "Секунды", 78),
            ("cancelled", "Отмена", 70), ("query", "SQL", 300),
        ))
        grid.pack(fill="both", expand=True)
        self.query_detail = text_panel(parent, 7)
        self.query_detail.pack(fill="x", pady=(8, 0))
        self.process_tree.bind("<<TreeviewSelect>>", self.show_process)

    def run(self, description, work, apply, error_parent=None):
        if self.busy:
            return
        self.busy = True
        self.status.set(description)
        self.update_actions()

        def worker():
            try:
                self.results.put((apply, work(), None, error_parent))
            except Exception as error:
                self.results.put((apply, None, str(error), error_parent))

        Thread(target=worker, daemon=True).start()

    def check_results(self):
        try:
            apply, value, error, parent = self.results.get_nowait()
        except Empty:
            pass
        else:
            try:
                self.busy = False
                self.update_actions()
                if error is not None:
                    self.status.set("Операция завершилась с ошибкой.")
                    messagebox.showerror("ClickHouse TTL", error, parent=parent or self.root)
                else:
                    apply(value)
            finally:
                self.root.after(60, self.check_results)
            return
        self.root.after(60, self.check_results)

    def update_actions(self):
        idle = not self.busy
        table = self.table
        merge_tree = bool(table and table.merge_tree)
        ttl_known = bool(merge_tree and table.formatted_ddl is not None)
        has_ttl = bool(ttl_known and table.ttl)
        has_tenants = bool(table and table.supports_tenant_sizes and "tenant" in self.columns)
        for button, allowed in (
            (self.connect_button, True), (self.refresh_button, self.client),
            (self.edit_button, ttl_known and time_columns(table, self.columns)),
            (self.parts_button, merge_tree),
            (self.clean_part_button, has_ttl and self.part_tree.selection()),
            (self.clean_table_button, has_ttl), (self.mutation_button, merge_tree),
            (self.tenant_parts_button, has_tenants), (self.tenant_size_button, has_tenants),
            (self.tenant_measure_button, has_tenants),
            (self.process_button, self.client),
        ):
            button.configure(state="normal" if idle and allowed else "disabled")
        self.tenant_scope.configure(state="readonly" if idle and has_tenants else "disabled")
        if self.editor:
            self.editor.update_actions()

    def connect(self):
        if self.busy:
            return
        database = self.db.get().strip()
        try:
            if not database:
                raise ValueError("Укажите базу данных.")
            client = ClickHouse(self.url.get(), self.user.get(), self.password.get())
        except ValueError as error:
            messagebox.showerror("Подключение", str(error), parent=self.root)
            return

        def work():
            server = client.server()
            return server, client.tables(database)

        def apply(result):
            self.server, self.tables = result
            self.client, self.database = client, database
            self.password.set("")
            self.connection_status.configure(
                text=f"{self.server['host']}   ·   ClickHouse {self.server['version']}   ·   {database}",
            )
            self.processes = []
            fill_tree(self.process_tree, ())
            set_text(self.query_detail, "")
            self.populate_tables(keep=False)

        self.run("Подключение и загрузка метаданных…", work, apply)

    def refresh_tables(self):
        if not self.client:
            return

        def apply(tables):
            self.tables = tables
            self.populate_tables()

        self.run("Обновление списка таблиц…", lambda: self.client.tables(self.database), apply)

    def populate_tables(self, keep=True):
        saved = self.table.title if keep and self.table else None
        self.set_table(None)
        self.show_tables(preferred=saved)
        self.status.set(f"Загружено таблиц: {len(self.tables)}")
        self.update_actions()

    def show_tables(self, preferred=None):
        preferred = preferred or (self.table.title if self.table else None)
        term = self.filter.get().strip().casefold()
        self.filtered_tables = [table for table in self.tables if term in table.title.casefold()]
        fill_tree(self.table_tree, (
            (table.title, size_label(table.size), count_label(table.rows),
             "Есть" if table.ttl else "—" if table.ttl == "" else "?")
            for table in self.filtered_tables
        ))
        for index, table in enumerate(self.filtered_tables):
            if table.title == preferred:
                self.table_tree.selection_set(str(index))
                self.table_tree.see(str(index))
                return
        if self.table:
            self.set_table(None)

    def select_table(self, _event):
        index = selected_index(self.table_tree)
        if index is None or index >= len(self.filtered_tables):
            return
        table = self.filtered_tables[index]
        if self.busy:
            if self.table in self.filtered_tables:
                current = self.filtered_tables.index(self.table)
                if index != current:
                    self.table_tree.selection_set(str(current))
            else:
                self.table_tree.selection_remove(self.table_tree.selection())
            return
        if self.table == table:
            return
        self.set_table(table)
        if table.merge_tree:
            def apply(columns):
                if self.table == table:
                    self.columns = columns
                    if table.supports_tenant_sizes and "tenant" not in columns:
                        self.tenant_summary.configure(text="В таблице нет столбца tenant.")
                    self.status.set(f"Выбрана {table.title}")
                    self.update_actions()

            self.run("Загрузка столбцов…", lambda: self.client.columns(table), apply)

    def set_table(self, table):
        self.table = table
        self.columns = {}
        self.partitions = []
        self.mutations = []
        fill_tree(self.part_tree, ())
        self.tenant_scope.configure(values=("Вся таблица",))
        self.tenant_scope.current(0)
        self.reset_tenant_sizes()
        if table and table.supports_tenant_sizes:
            self.detail_tabs.add(self.tenant_tab)
        else:
            self.detail_tabs.hide(self.tenant_tab)
        fill_tree(self.mutation_tree, ())
        set_text(self.mutation_detail, "")
        self.table_title.configure(text=table.title if table else "Выберите таблицу")
        self.table_summary.configure(text=(
            f"{table.engine}   ·   {size_label(table.size)}   ·   {count_label(table.rows)} строк"
            if table else ""
        ))
        self.system_note.configure(text=(
            "Постоянный TTL активного системного лога задаётся в конфигурации ClickHouse."
            if table and table.database == "system" else ""
        ))
        if table:
            ttl = table.ttl
            set_text(self.current_ttl, "TTL не задан." if ttl == "" else ttl or "Не удалось разобрать DDL.")
            set_text(self.ddl, table.formatted_ddl or table.ddl)
        else:
            set_text(self.current_ttl, "")
            set_text(self.ddl, "")
        self.update_actions()

    def open_editor(self):
        if not self.table or self.busy:
            return
        self.editor = Editor(self, self.table)
        self.editor.window.wait_window()
        self.editor = None

    def load_partitions(self):
        table = self.table

        def apply(partitions):
            if self.table != table:
                return
            self.partitions = partitions
            fill_tree(self.part_tree, (
                (part["partition_id"], part["label"], size_label(part["size"]), count_label(part["rows"]),
                 part["ttl_min"] if not part["ttl_min"].startswith("1970-01-01") else "—",
                 part["ttl_max"] if not part["ttl_max"].startswith("1970-01-01") else "—")
                for part in partitions
            ))
            scope = self.tenant_scope.get()
            scopes = ("Вся таблица", *(part["partition_id"] for part in partitions))
            self.tenant_scope.configure(values=scopes)
            self.tenant_scope.current(scopes.index(scope) if scope in scopes else 0)
            if self.tenant_scope.get() != scope:
                self.reset_tenant_sizes()
            self.status.set(f"Активных партиций: {len(partitions)}")
            self.update_actions()

        self.run("Загрузка активных партиций…", lambda: self.client.partitions(table), apply)

    def reset_tenant_sizes(self):
        measure_data = self.measure_tenant_bytes.get()
        method = (
            "Размер каждой части распределяется по объёму данных тенантов. Расчёт читает столбцы таблицы."
            if measure_data else
            "Размер каждой части распределяется по числу строк тенантов. Расчёт читает столбец tenant."
        )
        self.tenant_method.configure(text=method)
        self.tenant_tree.configure(displaycolumns=("tenant", "rows", "data", "disk") if measure_data
                                   else ("tenant", "rows", "disk"))
        fill_tree(self.tenant_tree, ())
        self.tenant_summary.configure(text="")

    def load_tenant_sizes(self):
        table = self.table
        measure_data = self.measure_tenant_bytes.get()
        partition = self.tenant_scope.get() if self.tenant_scope.current() > 0 else None
        scope = f"Партиция {partition}" if partition is not None else "Вся таблица"

        def work():
            self.client.assert_current(table)
            return self.client.tenant_sizes(table, partition, measure_data=measure_data)

        def apply(rows):
            if self.table != table:
                return
            fill_tree(self.tenant_tree, (
                ("⟨NULL⟩" if row["tenant"] is None else str(row["tenant"]) or "⟨пусто⟩",
                 count_label(row["rows"]), size_label(row["data_bytes"]), size_label(row["disk_bytes"]))
                for row in rows
            ))
            total_rows = count_label(sum(int(row["rows"]) for row in rows))
            summary = f"{scope}   ·   Тенантов: {len(rows)}   ·   Строк: {total_rows}"
            if any(row["disk_bytes"] is None for row in rows):
                summary += "\n«—» означает, что для части строк не удалось сопоставить размер части."
            self.tenant_summary.configure(text=summary)
            self.status.set("Расчёт по тенантам завершён.")

        self.run("Расчёт размера по тенантам…", work, apply)

    def clean(self, whole_table):
        if self.busy or not self.table or not self.table.ttl:
            return
        index = selected_index(self.part_tree)
        if whole_table:
            partition = None
            scope = "все партиции"
        elif index is not None:
            partition = self.partitions[index]["partition_id"]
            scope = f"партиция {partition}"
        else:
            return
        table = self.table
        sql = materialize_sql(table, partition)
        confirmed = messagebox.askokcancel(
            "Материализация TTL",
            f"{table.host}  ·  {table.title}  ·  {scope}\n\n{sql}\n\n"
            "ClickHouse поставит мутацию в очередь. Действующий TTL может удалить истёкшие строки или применить другие действия из DDL. Продолжить?",
            icon="warning", default="cancel", parent=self.root,
        )
        if not confirmed:
            return

        def work():
            self.client.assert_current(table)
            if partition is not None and not any(
                row["partition_id"] == partition for row in self.client.partitions(table)
            ):
                raise ClickHouseError("Партиция больше не активна. Обновите список партиций.")
            return self.client.change(sql, {"mutations_sync": 0})

        def apply(query_id):
            self.status.set(f"Команда принята ClickHouse. Query ID {query_id}")
            messagebox.showinfo("Материализация TTL",
                                f"Команда принята. Проверьте состояние на вкладке «Мутации».\n\nQuery ID {query_id}",
                                parent=self.root)

        self.run("Отправка MATERIALIZE TTL…", work, apply)

    def load_mutations(self):
        table = self.table

        def apply(mutations):
            self.mutations = mutations
            fill_tree(self.mutation_tree, (
                (row["mutation_id"], row["create_time"], row["parts_to_do"],
                 "Завершена" if row["is_done"] else "Есть ошибка" if row["latest_fail_reason"] else "В обработке")
                for row in mutations
            ))
            set_text(self.mutation_detail, "")
            self.status.set(f"Мутаций в выборке: {len(mutations)}")

        self.run("Загрузка мутаций…", lambda: self.client.mutations(table), apply)

    def show_mutation(self, _event):
        index = selected_index(self.mutation_tree)
        if index is not None and index < len(self.mutations):
            row = self.mutations[index]
            set_text(self.mutation_detail, row["command"] + "\n\n" + row["latest_fail_reason"])

    def load_processes(self):
        term = self.query_filter.get().strip()

        def apply(processes):
            self.processes = processes
            fill_tree(self.process_tree, (
                (row["query_id"], row["user"], row["elapsed"], "Да" if row["is_cancelled"] else "Нет",
                 " ".join(row["query"].split()))
                for row in processes
            ))
            set_text(self.query_detail, "")
            self.status.set(f"Запросов в выборке: {len(processes)}")

        self.run("Загрузка активных запросов…", lambda: self.client.processes(term), apply)

    def show_process(self, _event):
        index = selected_index(self.process_tree)
        if index is not None and index < len(self.processes):
            set_text(self.query_detail, self.processes[index]["query"])


class Editor:
    def __init__(self, app, table):
        self.app, self.table = app, table
        self.window = tk.Toplevel(app.root)
        self.window.title(f"Настроить TTL — {table.title}")
        self.window.geometry("720x725")
        self.window.minsize(600, 650)
        self.window.transient(app.root)
        self.window.grab_set()
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        self.date_columns = time_columns(table, app.columns)
        self.can_use_tenants = table.database != "system" and supported_tenant(app.columns.get("tenant", ""))
        form = ttk.Frame(self.window, padding=14, style="Panel.TFrame")
        form.pack(fill="both", expand=True)
        ttk.Label(form, text=table.title, style="Title.TLabel").pack(anchor="w")
        ttk.Label(form, text=f"{table.host}  ·  Часовой пояс сервера {app.server['timezone']}",
                  style="Hint.TLabel").pack(anchor="w", pady=(1, 9))

        current = text_panel(form, 3)
        current.pack(fill="x", pady=(0, 10))
        set_text(current, "Текущий TTL\n" + (table.ttl or "Не задан."))

        fields = ttk.Frame(form, style="Panel.TFrame")
        fields.pack(fill="x", pady=(0, 8))
        ttk.Label(fields, text="Столбец даты", style="Hint.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(fields, text="Хранить дней", style="Hint.TLabel").grid(row=0, column=1, sticky="w")
        preferred = {"traces_meta_v5": "max_start_time", "spans_v5": "start_time"}.get(table.name, "event_date")
        self.column = tk.StringVar(value=preferred if preferred in self.date_columns else "")
        self.days = tk.StringVar()
        choice = ttk.Combobox(fields, textvariable=self.column, values=tuple(self.date_columns), state="readonly", width=26)
        choice.grid(row=1, column=0, sticky="w", padx=(0, 10))
        ttk.Entry(fields, textvariable=self.days, width=12).grid(row=1, column=1, sticky="w")

        self.tenant_text = None
        if self.can_use_tenants:
            ttk.Label(form, text="Исключения по tenant, по одному на строку в формате имя=дни",
                      style="Hint.TLabel").pack(anchor="w", pady=(0, 4))
            self.tenant_text = text_panel(form, 3, editable=True)
            self.tenant_text.pack(fill="x")
            presets = ttk.Frame(form, style="Panel.TFrame")
            presets.pack(fill="x", pady=(4, 8))
            ttk.Button(presets, text="Шаблон sage", command=lambda: self.preset("sage=3")).pack(side="left")
            ttk.Button(presets, text="Шаблон fo_mir и jenkins", command=lambda: self.preset("fo_mir=3\njenkins=3")).pack(side="left", padx=6)
            self.tenant_text.bind("<<Modified>>", self.tenant_changed)

        ttk.Label(form, text="SQL для предпросмотра", style="Hint.TLabel").pack(anchor="w")
        self.preview = text_panel(form, 6)
        self.preview.pack(fill="both", expand=True, pady=(4, 7))
        self.notice = tk.StringVar()
        ttk.Label(form, textvariable=self.notice, style="Hint.TLabel", wraplength=610).pack(anchor="w", pady=(0, 8))

        actions = ttk.Frame(form, style="Panel.TFrame")
        actions.pack(fill="x")
        partition = selected_index(app.part_tree)
        self.partition_id = app.partitions[partition]["partition_id"] if partition is not None else None
        estimate_label = "Посчитать в партиции" if self.partition_id is not None else "Посчитать истёкшие строки"
        self.estimate_button = ttk.Button(actions, text=estimate_label, command=self.estimate)
        self.estimate_button.pack(side="left")
        self.apply_button = ttk.Button(actions, text="Заменить TTL", command=self.apply, style="Accent.TButton")
        self.apply_button.pack(side="right")
        self.close_button = ttk.Button(actions, text="Закрыть", command=self.close)
        self.close_button.pack(side="right", padx=6)
        self.column.trace_add("write", self.render)
        self.days.trace_add("write", self.render)
        self.render()

    def expression(self):
        column = self.column.get()
        if column not in self.date_columns:
            raise ValueError("Выберите поддерживаемый столбец даты или времени.")
        rules = tenant_rules(self.tenant_text.get("1.0", "end")) if self.tenant_text else {}
        name, data_type = self.date_columns[column]
        return expiration(
            name, data_type, retention_days(self.days.get()), rules,
            server_version=self.app.server["version"],
        )

    def tenant_changed(self, _event):
        if self.tenant_text.edit_modified():
            self.tenant_text.edit_modified(False)
            self.render()

    def preset(self, text):
        self.days.set("7")
        self.tenant_text.delete("1.0", "end")
        self.tenant_text.insert("1.0", text)
        self.render()

    def render(self, *_):
        try:
            sql = modify_sql(self.table, self.expression())
        except ValueError as error:
            self.valid = False
            self.notice.set(str(error))
            set_text(self.preview, "")
        else:
            self.valid = True
            self.notice.set("Команда заменит все правила TTL таблицы. Автоматическая материализация при ALTER отключена.")
            set_text(self.preview, "SET materialize_ttl_after_modify = 0;\n\n" + sql + ";")
        self.update_actions()

    def update_actions(self):
        enabled = self.valid and not self.app.busy
        for button in (self.estimate_button, self.apply_button):
            button.configure(state="normal" if enabled else "disabled")
        self.close_button.configure(state="disabled" if self.app.busy else "normal")

    def estimate(self):
        expression = self.expression()
        partition_id = self.partition_id
        suffix = f"партиция {partition_id}" if partition_id is not None else "вся таблица"

        def work():
            self.app.client.assert_current(self.table)
            return self.app.client.estimate(self.table, expression, partition_id)

        def apply(rows):
            if self.valid and expression == self.expression():
                self.notice.set(f"По новому условию истекло строк: {count_label(rows)} ({suffix}).")
            self.app.status.set("Подсчёт по условию TTL завершён.")

        self.app.run("Подсчёт строк по новому условию TTL…", work, apply, self.window)

    def apply(self):
        sql = modify_sql(self.table, self.expression())
        confirmed = messagebox.askokcancel(
            "Заменить TTL", f"{self.table.host}  ·  {self.table.title}\n\n{sql}\n\n"
            "Все действующие правила TTL таблицы будут заменены. Продолжить?",
            icon="warning", default="cancel", parent=self.window,
        )
        if not confirmed:
            return

        def work():
            self.app.client.assert_current(self.table)
            return self.app.client.change(sql, {"materialize_ttl_after_modify": 0, "mutations_sync": 0})

        def done(query_id):
            self.close()
            messagebox.showinfo("TTL изменён", f"ClickHouse принял ALTER.\n\nQuery ID {query_id}", parent=self.app.root)
            self.app.refresh_tables()

        self.app.run("Отправка MODIFY TTL…", work, done, self.window)

    def close(self):
        if not self.app.busy:
            self.app.editor = None
            self.window.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
