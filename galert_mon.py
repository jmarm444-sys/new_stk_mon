import concurrent.futures
from datetime import datetime
import os
import sys
import threading

# py2exe does not pack pytz/tzdata zone files into library.zip. Point the
# frozen exe at the copies placed next to it by freeze_gui_alert_mon1.py.
# Tcl/Tk must also be located before importing tkinter.
if getattr(sys, "frozen", False):
    _exe_dir = os.path.dirname(sys.executable)
    _pytz_zones = os.path.join(_exe_dir, "pytz", "zoneinfo")
    _tzdata_zones = os.path.join(_exe_dir, "tzdata", "zoneinfo")
    _tcl_dir = os.path.join(_exe_dir, "lib", "tcl")
    _tk_dir = os.path.join(_exe_dir, "lib", "tk")
    if os.path.isdir(_pytz_zones):
        os.environ.setdefault("PYTZ_TZDATADIR", _pytz_zones)
    if os.path.isdir(_tzdata_zones):
        os.environ.setdefault("PYTHONTZPATH", _tzdata_zones)
    if os.path.isdir(_tcl_dir):
        os.environ.setdefault("TCL_LIBRARY", _tcl_dir)
    if os.path.isdir(_tk_dir):
        os.environ.setdefault("TK_LIBRARY", _tk_dir)

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import pandas as pd
import yfinance as yf


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _default_holdings_path() -> str:
    """Prefer the classic name, then any nearby *holdings*.csv."""
    search_dirs = []
    for folder in (os.getcwd(), _app_dir()):
        if folder and folder not in search_dirs:
            search_dirs.append(folder)

    named = ("mult_holdings.csv", "9-4-eg_holdings.csv")
    for folder in search_dirs:
        for name in named:
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                return path

    for folder in search_dirs:
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            if name.lower().endswith(".csv") and "holdings" in name.lower():
                return os.path.join(folder, name)
    return "mult_holdings.csv"


def get_short_interest(symbol: str) -> str:
    """Fetch short interest percentage or shares short for a given symbol."""
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.get_info() if hasattr(ticker, "get_info") else (ticker.info or {})
        info = info or {}
        short_pct = info.get("shortPercentOfFloat")
        if short_pct is not None:
            return f"{short_pct * 100:.2f}%"
        shares_short = info.get("sharesShort")
        if shares_short is not None:
            return f"{shares_short:,} shares"
        return "N/A"
    except Exception:
        return "N/A"


def _close_frame_from_download(market_data: pd.DataFrame, symbols: list) -> pd.DataFrame:
    """Normalize yfinance download output to a Close-price DataFrame (one column per ticker)."""
    if market_data is None or getattr(market_data, "empty", True):
        raise ValueError("Yahoo Finance returned no price history.")

    close = None
    if isinstance(market_data.columns, pd.MultiIndex):
        levels = [list(market_data.columns.get_level_values(i)) for i in range(market_data.columns.nlevels)]
        if any(name == "Close" for name in levels[0]):
            close = market_data["Close"]
        elif market_data.columns.nlevels > 1 and any(name == "Close" for name in levels[1]):
            close = market_data.xs("Close", axis=1, level=1)
    elif "Close" in market_data.columns:
        close = market_data["Close"]

    if close is None:
        raise ValueError("Download is missing Close prices.")

    if isinstance(close, pd.Series):
        col_name = symbols[0] if symbols else (close.name or "Close")
        close = close.to_frame(name=col_name)

    close = close.dropna(how="all")
    if close.empty:
        raise ValueError("Yahoo Finance returned no usable daily closes.")

    close.columns = [str(c).strip() for c in close.columns]
    return close


def _nth_valid(series: pd.Series, position: int):
    clean = series.dropna()
    if clean.empty or abs(position) > len(clean) or (position >= 0 and position >= len(clean)):
        return float("nan")
    try:
        return float(clean.iloc[position])
    except (TypeError, ValueError):
        return float("nan")


def _price_moves(close: pd.DataFrame) -> pd.DataFrame:
    """Current / month-ago prices and % changes using each symbol's last valid bars.

    Yahoo often appends a stub last session with only a handful of tickers filled
    in. Using iloc[-1] then turns almost every Daily/Monthly % into NaN and the
    alert filter drops the whole book.
    """
    current = close.apply(lambda s: _nth_valid(s, -1))
    previous = close.apply(lambda s: _nth_valid(s, -2))
    month_ago = close.apply(lambda s: _nth_valid(s, 0))

    daily = (current - previous) / previous * 100
    monthly = (current - month_ago) / month_ago * 100

    moves = pd.DataFrame({
        "Current Price": current,
        "Month Ago Price": month_ago,
        "Daily % Change": daily,
        "Monthly % Change": monthly,
    })
    moves.index = moves.index.astype(str).str.strip()
    return moves


def _as_of_label(close: pd.DataFrame) -> str:
    last_dates = close.apply(lambda s: s.dropna().index[-1] if s.notna().any() else pd.NaT)
    valid = last_dates.dropna()
    if valid.empty:
        return "n/a"
    counts = valid.value_counts()
    common = counts.index[0]
    label = common.strftime("%Y-%m-%d") if hasattr(common, "strftime") else str(common)[:10]
    newer = int((valid > common).sum())
    if newer:
        return f"{label} ({newer} tickers newer)"
    return label


def _parse_number(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("$", "").replace(",", "").strip()
    if not text or text.lower() in ("nan", "none", "n/a"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


class StockAlertMonitorGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Stock Volatility & Short Interest Alert Monitor")
        self.geometry("1100;720".replace(";", "x"))
        self.minsize(900, 600)

        self.last_output_file = None
        self.is_running = False

        self._setup_styles()
        self._build_ui()

    def _setup_styles(self):
        self.style = ttk.Style(self)
        # Try to use 'clam' or native default for clean modern look
        available_themes = self.style.theme_names()
        if "vista" in available_themes:
            self.style.theme_use("vista")
        elif "clam" in available_themes:
            self.style.theme_use("clam")

        self.style.configure("Header.TLabel", font=("Segoe UI", 12, "bold"))
        self.style.configure("Bold.TLabel", font=("Segoe UI", 9, "bold"))
        self.style.configure("Status.TLabel", font=("Segoe UI", 9, "italic"))
        self.style.configure("Accent.TButton", font=("Segoe UI", 9, "bold"))
        self.style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        self.style.configure("Treeview", font=("Segoe UI", 9), rowheight=24)

    def _build_ui(self):
        # 1. Top Control Panel Frame
        control_frame = ttk.LabelFrame(self, text=" Monitor Settings & Filters ", padding=(12, 10))
        control_frame.pack(fill="x", padx=12, pady=(10, 6))

        # Row 1: Holdings CSV file selection
        ttk.Label(control_frame, text="Holdings File:").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
        self.file_entry = ttk.Entry(control_frame, width=45)
        default_file = _default_holdings_path()
        self.file_entry.insert(0, default_file)
        self.file_entry.grid(row=0, column=1, sticky="ew", padx=(0, 6), pady=4)

        browse_btn = ttk.Button(control_frame, text="Browse...", command=self._browse_file)
        browse_btn.grid(row=0, column=2, padx=(0, 16), pady=4)

        # Row 1 (cont): Threshold Filter Edit Boxes
        ttk.Label(control_frame, text="Daily Move Limit (%):", style="Bold.TLabel").grid(
            row=0, column=3, sticky="w", padx=(0, 6), pady=4
        )
        self.daily_entry = ttk.Entry(control_frame, width=8)
        self.daily_entry.insert(0, "7")
        self.daily_entry.grid(row=0, column=4, padx=(0, 16), pady=4)

        ttk.Label(control_frame, text="Monthly Move Limit (%):", style="Bold.TLabel").grid(
            row=0, column=5, sticky="w", padx=(0, 6), pady=4
        )
        self.monthly_entry = ttk.Entry(control_frame, width=8)
        self.monthly_entry.insert(0, "20")
        self.monthly_entry.grid(row=0, column=6, padx=(0, 16), pady=4)

        # Action Buttons
        self.run_btn = ttk.Button(
            control_frame,
            text="▶ Run Alert Scan",
            style="Accent.TButton",
            command=self._start_scan
        )
        self.run_btn.grid(row=0, column=7, padx=(0, 6), pady=4)

        self.open_file_btn = ttk.Button(
            control_frame,
            text="Open CSV",
            state="disabled",
            command=self._open_output_csv
        )
        self.open_file_btn.grid(row=0, column=8, padx=(0, 0), pady=4)

        control_frame.columnconfigure(1, weight=1)

        # 2. Progress and Status Frame
        status_frame = ttk.Frame(self, padding=(12, 2))
        status_frame.pack(fill="x", padx=12, pady=(0, 6))

        self.progress_bar = ttk.Progressbar(status_frame, mode="indeterminate", length=220)
        self.progress_bar.pack(side="right", padx=(10, 0))

        self.status_label = ttk.Label(
            status_frame,
            text="Ready. Set your thresholds and click 'Run Alert Scan'.",
            style="Status.TLabel"
        )
        self.status_label.pack(side="left", fill="x", expand=True)

        # 3. Results Table (Treeview)
        table_frame = ttk.LabelFrame(self, text=" Volatility & Short Interest Alerts ", padding=(8, 8))
        table_frame.pack(fill="both", expand=True, padx=12, pady=(0, 10))

        self.columns = [
            ("symbol", "Symbol", 80, "center"),
            ("acct", "Account Name", 110, "center"),
            ("qty", "Shares", 90, "e"),
            ("per_share", "Original Cost", 100, "e"),
            ("short_interest", "Short Interest", 110, "center"),
            ("current_price", "Current Price", 105, "e"),
            ("month_ago_price", "Month Ago Price", 115, "e"),
            ("daily_change", "Daily % Change", 115, "e"),
            ("monthly_change", "Monthly % Change", 125, "e"),
        ]

        self.tree = ttk.Treeview(
            table_frame,
            columns=[col[0] for col in self.columns],
            show="headings",
            selectmode="browse"
        )

        for col_id, heading, width, align in self.columns:
            self.tree.heading(col_id, text=heading, command=lambda c=col_id: self._sort_column(c, False))
            self.tree.column(col_id, width=width, anchor=align)

        # Tag configuration for coloring
        self.tree.tag_configure("positive_daily", foreground="#007700")
        self.tree.tag_configure("negative_daily", foreground="#cc0000")
        self.tree.tag_configure("even_row", background="#f9f9f9")
        self.tree.tag_configure("odd_row", background="#ffffff")

        # Scrollbars
        v_scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        h_scrollbar = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=v_scrollbar.set, xscrollcommand=h_scrollbar.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        v_scrollbar.grid(row=0, column=1, sticky="ns")
        h_scrollbar.grid(row=1, column=0, sticky="ew")

        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        # 4. Summary Bar
        self.summary_var = tk.StringVar(value="Total alerts: 0")
        summary_label = ttk.Label(self, textvariable=self.summary_var, padding=(14, 4))
        summary_label.pack(side="bottom", fill="x")

    def _browse_file(self):
        filename = filedialog.askopenfilename(
            title="Select Holdings CSV File",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
        )
        if filename:
            self.file_entry.delete(0, tk.END)
            self.file_entry.insert(0, filename)

    def _open_output_csv(self):
        if self.last_output_file and os.path.exists(self.last_output_file):
            try:
                os.startfile(os.path.abspath(self.last_output_file))
            except Exception as e:
                messagebox.showerror("Error Opening File", f"Could not open file: {e}")

    def _start_scan(self):
        if self.is_running:
            return

        input_path = self.file_entry.get().strip()
        if not input_path or not os.path.exists(input_path):
            messagebox.showerror("File Not Found", f"Holdings CSV file not found:\n'{input_path}'")
            return

        try:
            daily_limit = float(self.daily_entry.get().strip())
            monthly_limit = float(self.monthly_entry.get().strip())
        except ValueError:
            messagebox.showerror(
                "Invalid Filter Value",
                "Please enter valid numeric values for Daily and Monthly limits (e.g. 7 and 20)."
            )
            return

        # Disable scan button and show progress
        self.is_running = True
        self.run_btn.config(state="disabled")
        self.open_file_btn.config(state="disabled")
        self.progress_bar.start(10)
        self.status_label.config(text="Starting scan and reading holdings...")
        self.summary_var.set("Scanning market data...")

        # Clear existing table rows
        for item in self.tree.get_children():
            self.tree.delete(item)

        # Run backend work in separate background thread
        worker = threading.Thread(
            target=self._run_scan_thread,
            args=(input_path, daily_limit, monthly_limit),
            daemon=True
        )
        worker.start()

    def _run_scan_thread(self, input_path: str, daily_limit: float, monthly_limit: float):
        try:
            holdings = pd.read_csv(input_path)
            if holdings.empty:
                raise ValueError("Holdings CSV has no rows.")

            sym_col = next((c for c in ["symbol", "Symbol", "SYMBOL"] if c in holdings.columns), holdings.columns[0])
            holdings[sym_col] = holdings[sym_col].astype(str).str.strip()
            holdings = holdings[holdings[sym_col].ne("") & holdings[sym_col].str.lower().ne("nan")]
            symbols = holdings[sym_col].dropna().unique().tolist()
            if not symbols:
                raise ValueError("Holdings CSV has no symbols.")

            self._update_status(f"Downloading 1-month price history for {len(symbols)} symbols...")
            market_data = yf.download(
                symbols,
                period="1mo",
                interval="1d",
                group_by="column",
                auto_adjust=True,
                threads=True,
                progress=False,
                timeout=60,
            )
            close = _close_frame_from_download(market_data, symbols)
            moves = _price_moves(close)
            as_of = _as_of_label(close)

            merged_data = pd.merge(holdings, moves, left_on=sym_col, right_index=True)

            alerts = merged_data[
                (merged_data["Daily % Change"].abs() > daily_limit) |
                (merged_data["Monthly % Change"].abs() > monthly_limit)
            ].copy()

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_name = os.path.join(_app_dir(), f"{timestamp}_alerts.csv")

            if alerts.empty:
                alerts["short interest"] = pd.Series(dtype=str)
                alerts.to_csv(out_name, index=False)
                self.last_output_file = out_name
                self.after(0, self._on_scan_complete, alerts, out_name, len(symbols), 0, as_of)
                return

            alert_symbols = alerts[sym_col].unique().tolist()
            self._update_status(f"Fetching short interest data for {len(alert_symbols)} triggered symbols...")

            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                short_interest_map = dict(
                    zip(alert_symbols, executor.map(get_short_interest, alert_symbols))
                )

            alerts["short interest"] = alerts[sym_col].map(short_interest_map)

            holdings_cols = [c for c in holdings.columns if c in alerts.columns]
            price_cols = [c for c in alerts.columns if c not in holdings_cols and c != "short interest"]
            ordered_cols = holdings_cols + ["short interest"] + price_cols
            alerts = alerts[ordered_cols]

            alerts.to_csv(out_name, index=False)
            self.last_output_file = out_name

            self.after(0, self._on_scan_complete, alerts, out_name, len(symbols), len(alert_symbols), as_of)

        except Exception as err:
            self.after(0, self._on_scan_error, str(err))

    def _update_status(self, msg: str):
        self.after(0, lambda: self.status_label.config(text=msg))

    def _on_scan_complete(self, alerts_df: pd.DataFrame, out_file: str, total_syms: int, triggered_syms: int, as_of: str = ""):
        self.is_running = False
        self.progress_bar.stop()
        self.run_btn.config(state="normal")
        self.open_file_btn.config(state="normal")

        # Map column names flexibly
        sym_col = next((c for c in ["symbol", "Symbol", "SYMBOL"] if c in alerts_df.columns), "symbol")
        acct_col = next((c for c in ["acct", "Account", "Account Name", "ACCT"] if c in alerts_df.columns), "acct")
        qty_col = next((c for c in ["qty", "Shares", "QTY", "Qty"] if c in alerts_df.columns), "qty")
        cost_col = next((c for c in ["per_share", "Original Cost", "Cost", "per share"] if c in alerts_df.columns), "per_share")

        for display_idx, (_, row) in enumerate(alerts_df.iterrows()):
            sym_val = str(row.get(sym_col, ""))
            acct_val = str(row.get(acct_col, ""))

            qty_num = _parse_number(row.get(qty_col, ""))
            qty_val = f"{qty_num:,.2f}" if qty_num is not None else str(row.get(qty_col, ""))

            cost_num = _parse_number(row.get(cost_col, ""))
            cost_val = f"${cost_num:,.2f}" if cost_num is not None else str(row.get(cost_col, ""))

            si_val = str(row.get("short interest", "N/A"))

            cp_num = _parse_number(row.get("Current Price", ""))
            cp_val = f"${cp_num:,.2f}" if cp_num is not None else "N/A"

            mp_num = _parse_number(row.get("Month Ago Price", ""))
            mp_val = f"${mp_num:,.2f}" if mp_num is not None else "N/A"

            d_chg = row.get("Daily % Change", 0.0)
            d_val = f"{d_chg:+.2f}%" if pd.notnull(d_chg) else "N/A"

            m_chg = row.get("Monthly % Change", 0.0)
            m_val = f"{m_chg:+.2f}%" if pd.notnull(m_chg) else "N/A"

            values = (sym_val, acct_val, qty_val, cost_val, si_val, cp_val, mp_val, d_val, m_val)
            tags = ["even_row" if display_idx % 2 == 0 else "odd_row"]
            if pd.notnull(d_chg):
                if d_chg > 0:
                    tags.append("positive_daily")
                elif d_chg < 0:
                    tags.append("negative_daily")
            self.tree.insert("", "end", values=values, tags=tags)

        as_of_bit = f" Prices as of {as_of}." if as_of else ""
        self.status_label.config(text=f"Scan complete! Saved to: {out_file}{as_of_bit}")
        self.summary_var.set(
            f"Alerts: {len(alerts_df)} instances across {triggered_syms} symbols "
            f"(scanned {total_syms} total symbols). Output: {out_file}"
        )

    def _on_scan_error(self, err_msg: str):
        self.is_running = False
        self.progress_bar.stop()
        self.run_btn.config(state="normal")
        self.status_label.config(text=f"Scan error: {err_msg}")
        self.summary_var.set("Scan failed.")
        messagebox.showerror("Scan Error", f"An error occurred during the scan:\n\n{err_msg}")

    def _sort_column(self, col: str, reverse: bool):
        items = [(self.tree.set(k, col), k) for k in self.tree.get_children("")]

        def sort_key(element):
            val = element[0].replace("$", "").replace("%", "").replace(",", "").replace("+", "").strip()
            try:
                return (0, float(val))
            except ValueError:
                return (1, element[0].lower())

        items.sort(key=sort_key, reverse=reverse)

        for index, (_, k) in enumerate(items):
            self.tree.move(k, "", index)

        self.tree.heading(col, command=lambda: self._sort_column(col, not reverse))


def main():
    app = StockAlertMonitorGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
