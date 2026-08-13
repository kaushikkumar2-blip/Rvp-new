"""RVP Pickup Dashboard - Streamlit app.

Three modes for sourcing data:
- Upload: drop a fresh CSV/XLSX, compute pivots, optionally save snapshot to GitHub.
- GitHub snapshot: load a previously-saved snapshot of percentage tables.
- Trends: stitch every snapshot together to see metrics over time.

Two analytical pages:
1. Pickup Performance - day/week/month + seller-type outcome breakdown + conversion %
2. Attempt Performance - day/week/month + seller-type attempt buckets

Plus a Trends page that only activates when snapshots exist on GitHub.
"""

from __future__ import annotations

import hashlib
import io
from datetime import timedelta
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd
import streamlit as st

import github_store as ghs

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DEFAULT_CSV_PATH = DATA_DIR / "default.csv"

AGG_OUTCOME_COUNT = {
    "D0": "pickup_d0",
    "D1": "pickup_d1_cum",
    "D2": "pickup_d2_cum",
    "D3": "pickup_d3_cum",
    "D4+": "pickup_d4plus_cum",
    "Pending": "pickup_pending",
    "QC failed": "pickup_qc_failed",
    "Not Attempted": "pickup_not_attempted",
}
AGG_ATTEMPT_COUNT = {
    "D0": "attempt_d0",
    "D1": "attempt_d1_cum",
    "D2": "attempt_d2_cum",
    "D3": "attempt_d3_cum",
    "D4+": "attempt_d4plus_cum",
    "Not Attempted": "attempt_not_attempted",
}
DEFAULT_SHEET = "Externalization_RVP_report_ship"
ATTEMPT_ORDER = ["D0", "D1", "D2", "D3", "D4+", "Not Attempted"]
OUTCOME_ORDER = [
    "D0", "D1", "D2", "D3", "D4+",
    "Pending", "QC failed", "Not Attempted",
]
QC_REASONS = {"PRODUCT_DAMAGED", "PRODUCT_MISMATCH", "PRODUCT_MISMATCHED"}
DATE_FMT = "%Y-%m-%d"

REQUIRED_COLS = [
    "vendor_tracking_id",
    "seller_type",
    "Request_created_date",
    "First_pickup_date",
    "rvp_pickup_completed_date",
    "shipment_status_reason",
]

# Pulled through parsing when present; absent in some legacy files so we don't
# fail the upload if a column here is missing.
OPTIONAL_COLS = [
    "src_pincode",
]

# Explicit dtypes cut memory ~50% and let pandas skip type inference.
CSV_DTYPES = {
    "vendor_tracking_id": "string",
    "seller_type": "category",
    "shipment_status_reason": "category",
    "Request_created_date": "string",
    "First_pickup_date": "string",
    "rvp_pickup_completed_date": "string",
    "src_pincode": "string",
}


# ---------------------------------------------------------------------------
# Compute (unchanged logic, slightly cleaner)
# ---------------------------------------------------------------------------

def compute_attempt(df: pd.DataFrame) -> Tuple[pd.Series, pd.Series]:
    fp = pd.to_datetime(df["First_pickup_date"], errors="coerce", dayfirst=True)
    rc = pd.to_datetime(df["Request_created_date"], errors="coerce", dayfirst=True)

    rvp_done = df["rvp_pickup_completed_date"].astype("string").str.strip()
    rvp_empty = rvp_done.isna() | (rvp_done == "") | (rvp_done.str.lower() == "nan")

    reason = df["shipment_status_reason"].astype("string").str.upper().str.strip()
    qc_reasons = reason.isin(QC_REASONS)

    day_diff = (fp - rc).dt.days
    not_attempted = fp.isna()
    qc_failed = (~not_attempted) & rvp_empty & qc_reasons

    bucket = pd.Series(index=df.index, dtype="object")
    bucket[not_attempted] = "Not Attempted"
    bucket[qc_failed] = "QC failed"

    rest_mask = bucket.isna()
    if rest_mask.any():
        n = day_diff[rest_mask].clip(lower=0).fillna(0).astype(int)
        labels = n.where(n < 4, other=-1).map(lambda v: "D4+" if v == -1 else f"D{v}")
        bucket[rest_mask] = labels

    return bucket, day_diff


def _parse_rvp_completed(series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(series, errors="coerce", format="ISO8601")
    if parsed.isna().any():
        fallback = pd.to_datetime(series, errors="coerce", dayfirst=True)
        parsed = parsed.fillna(fallback)
    if getattr(getattr(parsed, "dt", None), "tz", None) is not None:
        parsed = parsed.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    return parsed


def compute_outcome(df: pd.DataFrame) -> pd.Series:
    fp = pd.to_datetime(df["First_pickup_date"], errors="coerce", dayfirst=True)
    rc = pd.to_datetime(df["Request_created_date"], errors="coerce", dayfirst=True)

    rvp_str = df["rvp_pickup_completed_date"].astype("string").str.strip()
    converted = rvp_str.notna() & (rvp_str != "") & (rvp_str.str.lower() != "nan")
    rvp_done = _parse_rvp_completed(df["rvp_pickup_completed_date"])

    not_attempted = fp.isna() & ~converted
    reason = df["shipment_status_reason"].astype("string").str.upper().str.strip()
    qc_failed = ~converted & ~not_attempted & reason.isin(QC_REASONS)

    out = pd.Series("Pending", index=df.index, dtype="object")
    out[qc_failed] = "QC failed"
    out[not_attempted] = "Not Attempted"

    if converted.any():
        diff = (rvp_done - rc).dt.days.clip(lower=0).fillna(0).astype(int)
        lag_labels = diff.where(diff < 4, other=-1).map(
            lambda v: "D4+" if v == -1 else f"D{v}"
        )
        out.loc[converted] = lag_labels.loc[converted].values

    return out


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

def as_row_percent(table: pd.DataFrame) -> pd.DataFrame:
    if "Grand Total" not in table.columns:
        raise ValueError("Expected a 'Grand Total' column")
    gt = table["Grand Total"].replace(0, pd.NA)
    return (table.div(gt, axis=0) * 100).round(1)


def cumulate_d_buckets(table: pd.DataFrame) -> pd.DataFrame:
    out = table.copy()
    d_cols = [c for c in ["D0", "D1", "D2", "D3", "D4+"] if c in out.columns]
    if d_cols:
        out[d_cols] = out[d_cols].cumsum(axis=1).astype(int)
    return out


def display_pivot(table: pd.DataFrame, as_percent: bool) -> None:
    if as_percent:
        styled = as_row_percent(table).style.format("{:.1f}%", na_rep="-")
    else:
        styled = table.style.format("{:,}", na_rep="-")
    st.dataframe(styled, use_container_width=True)


def pivot_counts(df: pd.DataFrame, index_col: str, value_col: str,
                 order: list) -> pd.DataFrame:
    p = df.pivot_table(
        index=index_col, columns=value_col,
        values="vendor_tracking_id", aggfunc="count", fill_value=0,
    )
    for b in order:
        if b not in p.columns:
            p[b] = 0
    p = p[order]
    p["Grand Total"] = p.sum(axis=1)
    p.loc["Grand Total"] = p.sum(axis=0)
    return p.astype(int)


def conversion_table(df: pd.DataFrame, group_col: str = "seller_type") -> pd.DataFrame:
    rvp_done = df["rvp_pickup_completed_date"].astype("string").str.strip()
    picked_mask = rvp_done.notna() & (rvp_done != "") & (rvp_done.str.lower() != "nan")

    work = df.assign(_picked=picked_mask.astype(int))
    agg = work.groupby(group_col, dropna=False, observed=True).agg(
        **{
            "Total Shipments": ("vendor_tracking_id", "count"),
            "Picked Shipments": ("_picked", "sum"),
        }
    )
    agg["Conversion %"] = (agg["Picked Shipments"] / agg["Total Shipments"] * 100).round(2)
    agg.loc["Grand Total"] = [
        agg["Total Shipments"].sum(),
        agg["Picked Shipments"].sum(),
        round(agg["Picked Shipments"].sum() / max(agg["Total Shipments"].sum(), 1) * 100, 2),
    ]
    agg["Total Shipments"] = agg["Total Shipments"].astype(int)
    agg["Picked Shipments"] = agg["Picked Shipments"].astype(int)
    return agg


# ---------------------------------------------------------------------------
# IO: parse upload (cached so toggling filters is instant)
# ---------------------------------------------------------------------------

def read_csv_fast(content: bytes) -> pd.DataFrame:
    """Read required + optional columns with explicit dtypes.

    Optional columns are tolerated as missing: we use a callable for usecols so
    pandas silently skips columns that aren't in the file's header.
    """
    wanted = set(REQUIRED_COLS) | set(OPTIONAL_COLS)
    usecols = lambda c: c in wanted  # noqa: E731 - tiny predicate
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return pd.read_csv(
                io.BytesIO(content),
                encoding=encoding,
                usecols=usecols,
                dtype=CSV_DTYPES,
                engine="c",
                low_memory=False,
            )
        except UnicodeDecodeError:
            continue
        except ValueError as exc:
            raise exc
    return pd.read_csv(io.BytesIO(content), encoding="latin-1",
                       usecols=usecols, dtype=CSV_DTYPES,
                       on_bad_lines="skip")


def is_csv(filename: str) -> bool:
    return filename.lower().endswith(".csv")


def read_bytes(content: bytes, filename: str,
               sheet_name: Optional[str] = None) -> pd.DataFrame:
    if is_csv(filename):
        return read_csv_fast(content)
    xl = pd.ExcelFile(io.BytesIO(content))
    if sheet_name is None:
        sheet_name = DEFAULT_SHEET if DEFAULT_SHEET in xl.sheet_names else xl.sheet_names[0]
    # Excel: read all then prune (openpyxl doesn't have usecols-by-name reliably).
    df = pd.read_excel(io.BytesIO(content), sheet_name=sheet_name)
    wanted = REQUIRED_COLS + OPTIONAL_COLS
    keep = [c for c in wanted if c in df.columns]
    return df[keep] if keep else df


def list_sheets(content: bytes) -> list[str]:
    return pd.ExcelFile(io.BytesIO(content)).sheet_names


@st.cache_data(show_spinner="Reading BIC pincodes...", max_entries=4)
def load_bic_pincodes(content: bytes) -> frozenset[str]:
    """Parse a single-column pincode CSV into a normalized frozenset.

    Tolerates a few common header spellings (Pincode/pincode/PIN/pin_code).
    Returns stripped strings to match the normalization done in
    parse_and_enrich for `src_pincode`.
    """
    candidates = ("Pincode", "pincode", "PIN", "pin", "pin_code", "Pin")
    last_err: Optional[Exception] = None
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            df = pd.read_csv(io.BytesIO(content), encoding=encoding, dtype=str)
            break
        except UnicodeDecodeError as exc:
            last_err = exc
            continue
    else:
        raise ValueError(f"Could not decode pincode CSV: {last_err}")

    col = next((c for c in candidates if c in df.columns), None)
    if col is None:
        # Fall back to the first column if header doesn't match a known name.
        if df.shape[1] == 0:
            raise ValueError("Pincode CSV has no columns.")
        col = df.columns[0]

    series = df[col].dropna().astype(str).str.strip()
    series = series.str.replace(r"\.0$", "", regex=True)
    series = series[series != ""]
    return frozenset(series)


def missing_cols(df: pd.DataFrame) -> list[str]:
    return [c for c in REQUIRED_COLS if c not in df.columns]


def format_date(value) -> str:
    if hasattr(value, "strftime"):
        return value.strftime(DATE_FMT)
    return str(value)


@st.cache_data(show_spinner="Parsing file...", max_entries=4)
def parse_and_enrich(content: bytes, filename: str,
                     sheet_name: Optional[str]) -> pd.DataFrame:
    df = read_bytes(content, filename, sheet_name)
    missing = missing_cols(df)
    if missing:
        raise ValueError("Missing required columns: " + ", ".join(missing))
    df = df.copy()
    df["Request_created_date"] = pd.to_datetime(
        df["Request_created_date"], errors="coerce", dayfirst=True
    )
    df["First_pickup_date"] = pd.to_datetime(
        df["First_pickup_date"], errors="coerce", dayfirst=True
    )
    if "src_pincode" in df.columns:
        # Pincodes can arrive as int, float, or string with whitespace; coerce
        # to a clean string so set-membership checks against the BIC list work.
        pin = df["src_pincode"].astype("string").str.strip()
        # Drop a trailing ".0" left over when pandas reads ints via float.
        pin = pin.str.replace(r"\.0$", "", regex=True)
        df["src_pincode"] = pin
    bucket, _ = compute_attempt(df)
    df = df.assign(
        AttemptBucket=bucket,
        PickupOutcome=compute_outcome(df),
    )
    df["RequestDate"] = df["Request_created_date"].dt.date
    df["WeekStart"] = df["Request_created_date"].dt.to_period("W").dt.start_time.dt.date
    df["MonthStart"] = df["Request_created_date"].dt.to_period("M").dt.start_time.dt.date
    return df


# ---------------------------------------------------------------------------
# Filtered-frame + pivot caches (keyed on a digest of the filters)
# ---------------------------------------------------------------------------

def _df_signature(df: pd.DataFrame) -> str:
    """Stable hash of a dataframe's identity (shape + a few key column hashes).

    pd.util.hash_pandas_object is fast and column-aware; we cap the rows we
    hash for very big frames to keep this snappy.
    """
    sample = df if len(df) <= 50_000 else df.iloc[:: max(1, len(df) // 50_000)]
    h = hashlib.md5()
    h.update(str(sample.shape).encode())
    for col in ("vendor_tracking_id", "AttemptBucket", "PickupOutcome"):
        if col in sample.columns:
            h.update(pd.util.hash_pandas_object(sample[col], index=False).values.tobytes())
    return h.hexdigest()


@st.cache_data(show_spinner=False, max_entries=8)
def cached_pivot(_df: pd.DataFrame, sig: str, index_col: str,
                 value_col: str, order: tuple) -> pd.DataFrame:
    """Cached counts pivot. `sig` is the cache key; _df is the data."""
    return pivot_counts(_df, index_col, value_col, list(order))


@st.cache_data(show_spinner=False, max_entries=8)
def cached_conversion(_df: pd.DataFrame, sig: str) -> pd.DataFrame:
    return conversion_table(_df, "seller_type")


# ---------------------------------------------------------------------------
# Granularity helpers
# ---------------------------------------------------------------------------

def _week_label(value) -> str:
    if hasattr(value, "strftime"):
        end = value + timedelta(days=6)
        wk = pd.Timestamp(value).isocalendar().week
        return f"W{wk:02d} · {value.strftime(DATE_FMT)} to {end.strftime(DATE_FMT)}"
    return str(value)


def _month_label(value) -> str:
    if hasattr(value, "strftime"):
        ts = pd.Timestamp(value)
        return f"M{ts.month:02d} · {ts.strftime('%Y-%m')}"
    return str(value)


GRANULARITY_CONFIG = {
    "Day": ("RequestDate", format_date, "Request_created_date"),
    "Week": ("WeekStart", _week_label, "Week"),
    "Month": ("MonthStart", _month_label, "Month"),
}


# ---------------------------------------------------------------------------
# Snapshot bundle: compute all 7 % tables for storage
# ---------------------------------------------------------------------------

def build_snapshot_tables(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Produce the seven % tables that get committed to GitHub.

    All pivots are cumulated D-buckets first, then converted to row % so the
    stored numbers are immediately readable.
    """
    out: dict[str, pd.DataFrame] = {}

    grans = {
        "daily": ("RequestDate", format_date, "Request_created_date"),
        "weekly": ("WeekStart", _week_label, "Week"),
        "monthly": ("MonthStart", _month_label, "Month"),
    }

    for prefix, (col, labeler, idx_name) in grans.items():
        att = pivot_counts(df, col, "AttemptBucket", ATTEMPT_ORDER)
        att = cumulate_d_buckets(att)
        att.index = att.index.map(labeler)
        att.index.name = idx_name
        out[f"{prefix}_attempt_pct"] = as_row_percent(att)

        outc = pivot_counts(df, col, "PickupOutcome", OUTCOME_ORDER)
        outc = cumulate_d_buckets(outc)
        outc.index = outc.index.map(labeler)
        outc.index.name = idx_name
        out[f"{prefix}_outcome_pct"] = as_row_percent(outc)

    conv = conversion_table(df, "seller_type")
    conv.index.name = "seller_type"
    out["conversion_by_seller"] = conv

    return out


# ---------------------------------------------------------------------------
# Excel exports (kept from original)
# ---------------------------------------------------------------------------

def build_attempt_workbook(day_pivot, seller_pivot, granularity="Day") -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        day_pivot.to_excel(writer, sheet_name=f"{granularity}-wise attempt")
        seller_pivot.to_excel(writer, sheet_name="Seller-type attempt")
    return buf.getvalue()


def build_pickup_workbook(day_outcome, seller_outcome, conv_table,
                          granularity="Day") -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        day_outcome.to_excel(writer, sheet_name=f"{granularity}-wise outcome")
        seller_outcome.to_excel(writer, sheet_name="Seller-type outcome")
        conv_table.to_excel(writer, sheet_name="Conversion")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Default dataset (bundled CSV)
# ---------------------------------------------------------------------------

def _resolve_default_csv_path() -> Optional[Path]:
    """Return the default shipment CSV path, if configured and present."""
    try:
        configured = st.secrets.get("default_data", {}).get("path")
    except (KeyError, FileNotFoundError):
        configured = None
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = APP_DIR / path
        return path if path.is_file() else None
    if DEFAULT_CSV_PATH.is_file():
        return DEFAULT_CSV_PATH
    csvs = sorted(DATA_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not csvs:
        return None
    # Prefer the current export schema (has `converted`) over legacy daily files.
    new_schema = [
        p for p in csvs
        if "converted" in pd.read_csv(p, nrows=0).columns
    ]
    if new_schema:
        return max(new_schema, key=lambda p: p.stat().st_mtime)
    return csvs[0]


@st.cache_data(show_spinner=False)
def _read_default_csv(path_str: str, mtime_ns: int) -> bytes:
    del mtime_ns  # cache bust when the file changes on disk
    return Path(path_str).read_bytes()


def _default_dataset() -> Optional[Tuple[bytes, str, Path]]:
    """Load bundled default CSV bytes. Cached by path mtime."""
    path = _resolve_default_csv_path()
    if path is None:
        return None
    content = _read_default_csv(str(path), path.stat().st_mtime_ns)
    return content, path.name, path


def is_aggregated_shipment_csv(columns) -> bool:
    cols = set(columns)
    return (
        "total_shipments" in cols
        and (
            "converted" in cols
            or "pickup_d0" in cols
            or "pickup_d2_cum" in cols
        )
        and "vendor_tracking_id" not in cols
    )


def sniff_data_format(content: bytes, filename: str) -> str:
    if not is_csv(filename):
        return "raw"
    head = pd.read_csv(io.BytesIO(content), nrows=0)
    return "aggregated" if is_aggregated_shipment_csv(head.columns) else "raw"


def _parse_aggregated_day(series: pd.Series) -> pd.Series:
    """ISO (2026-07-08) or legacy DD-MM-YYYY."""
    parsed = pd.to_datetime(series, errors="coerce", format="ISO8601")
    if parsed.notna().sum() < max(1, len(series) // 2):
        parsed = parsed.fillna(pd.to_datetime(series, errors="coerce", dayfirst=True))
    return parsed


@st.cache_data(show_spinner="Reading aggregated CSV...", max_entries=4)
def parse_aggregated_csv(content: bytes) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(content))
    day = _parse_aggregated_day(df["day"])
    df = df.assign(
        day=day,
        RequestDate=day.dt.date,
        WeekStart=day.dt.to_period("W").dt.start_time.dt.date,
        MonthStart=day.dt.to_period("M").dt.start_time.dt.date,
        seller_type=df["seller_type"].astype(str),
    )
    return df


def _first_col(df: pd.DataFrame, *names: str) -> Optional[str]:
    for name in names:
        if name in df.columns:
            return name
    return None


def _is_pickup_sum_col(name: str, df: pd.DataFrame) -> bool:
    if name.endswith("_pct") or name == "converted":
        return False
    if name.startswith("pickup_") or name.startswith("cancelled"):
        return True
    return name in {"qc_failed", "not_attempted", "pending"}


def _is_attempt_sum_col(name: str, df: pd.DataFrame) -> bool:
    if name.endswith("_pct"):
        return False
    if name.startswith("attempt_"):
        return True
    if name.startswith("cancelled"):
        return "attempt_cancelled" not in df.columns or name != "cancelled"
    return name in {"qc_failed", "not_attempted", "pending"}


def pickup_sum_cols(df: pd.DataFrame) -> list[str]:
    """Count columns for pickup view — follows CSV column order."""
    cols: list[str] = []
    if "total_shipments" in df.columns:
        cols.append("total_shipments")
    conv_col = _first_col(df, "converted", "pickup_d4plus_cum")
    if conv_col and conv_col not in cols:
        cols.append(conv_col)
    for c in df.columns:
        if c in cols or not _is_pickup_sum_col(c, df):
            continue
        cols.append(c)
    return cols


def attempt_sum_cols(df: pd.DataFrame) -> list[str]:
    """Count columns for attempt view — follows CSV column order."""
    cols: list[str] = []
    if "total_shipments" in df.columns:
        cols.append("total_shipments")
    for c in df.columns:
        if c in cols or not _is_attempt_sum_col(c, df):
            continue
        cols.append(c)
    return cols


def _agg_sum_pivot(
    df: pd.DataFrame,
    index_col: str,
    col_map: dict[str, str],
    order: list[str],
) -> pd.DataFrame:
    grouped = df.groupby(index_col, dropna=False)[list(col_map.values())].sum()
    grouped = grouped.rename(columns={v: k for k, v in col_map.items()})
    for bucket in order:
        if bucket not in grouped.columns:
            grouped[bucket] = 0
    grouped = grouped[order]
    grouped["Grand Total"] = grouped.sum(axis=1)
    grouped.loc["Grand Total"] = grouped.sum(axis=0)
    return grouped.astype(int)


def _agg_conversion_table(df: pd.DataFrame) -> pd.DataFrame:
    picked_col = _first_col(df, "converted", "pickup_d4plus_cum") or "pickup_d4plus_cum"
    agg = df.groupby("seller_type", dropna=False).agg(
        **{
            "Total Shipments": ("total_shipments", "sum"),
            "Picked Shipments": (picked_col, "sum"),
        }
    )
    agg["Conversion %"] = (
        agg["Picked Shipments"] / agg["Total Shipments"] * 100
    ).round(2)
    agg.loc["Grand Total"] = [
        int(agg["Total Shipments"].sum()),
        int(agg["Picked Shipments"].sum()),
        round(
            agg["Picked Shipments"].sum()
            / max(agg["Total Shipments"].sum(), 1)
            * 100,
            2,
        ),
    ]
    agg["Total Shipments"] = agg["Total Shipments"].astype(int)
    agg["Picked Shipments"] = agg["Picked Shipments"].astype(int)
    return agg


def _apply_filters_agg(df_full: pd.DataFrame) -> Optional[pd.DataFrame]:
    valid_dates = df_full["RequestDate"].dropna()
    if valid_dates.empty:
        st.warning("No valid day values found in the aggregated CSV.")
        return None

    min_date, max_date = valid_dates.min(), valid_dates.max()
    seller_options = sorted(df_full["seller_type"].dropna().astype(str).unique().tolist())

    date_col, seller_col = st.columns([1, 1])
    with date_col:
        date_range = st.date_input(
            "day",
            value=(min_date, max_date),
            min_value=min_date,
            max_value=max_date,
            format="YYYY-MM-DD",
            key="agg_date_range",
        )
    with seller_col:
        selected_sellers = st.multiselect(
            "seller_type",
            options=seller_options,
            default=seller_options,
            placeholder="All seller types",
            key="agg_seller_filter",
        )

    df = df_full
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start, end = date_range
        df = df[(df["RequestDate"] >= start) & (df["RequestDate"] <= end)]
    if not selected_sellers:
        st.warning("Select at least one seller_type.")
        return None
    if len(selected_sellers) != len(seller_options):
        df = df[df["seller_type"].isin(selected_sellers)]
    if df.empty:
        st.warning("No rows match the selected filters.")
        return None
    return df


# ---------------------------------------------------------------------------
# Sidebar: source picker (Upload / GitHub snapshot)
# ---------------------------------------------------------------------------

def _render_upload_source() -> tuple[Optional[pd.DataFrame], Optional[pd.DataFrame]]:
    """Return (raw_df, agg_df); exactly one may be set."""
    uploaded = st.file_uploader(
        "Upload raw file (.xlsx, .xls, or .csv)",
        type=["xlsx", "xls", "csv"], key="uploader",
        help="Optional — replaces the CSV loaded from the data/ folder.",
    )
    if uploaded is not None:
        st.session_state["_file_bytes"] = uploaded.getvalue()
        st.session_state["_file_name"] = uploaded.name

    content = st.session_state.get("_file_bytes")
    filename = st.session_state.get("_file_name")
    default_path: Optional[Path] = None
    using_upload = content is not None and filename is not None

    if not using_upload:
        default = _default_dataset()
        if default is None:
            hint = "`data/` (any `.csv`; `default.csv` preferred)"
            try:
                if st.secrets.get("default_data", {}).get("path"):
                    hint = f"`{st.secrets['default_data']['path']}` (from secrets)"
            except (KeyError, FileNotFoundError):
                pass
            st.info(f"Upload a file to get started, or place a CSV at {hint}.")
            return None, None
        content, filename, default_path = default

    info_col, clear_col = st.columns([6, 1])
    if using_upload:
        info_col.caption(f"Using `{filename}` ({len(content) / 1024:.0f} KB)")
        if clear_col.button("Clear", help="Remove the uploaded file and use default CSV"):
            st.session_state.pop("_file_bytes", None)
            st.session_state.pop("_file_name", None)
            st.rerun()
    else:
        rel = default_path.relative_to(APP_DIR) if default_path else DEFAULT_CSV_PATH
        info_col.caption(
            f"Default dataset: `{rel}` ({len(content) / 1024:.0f} KB). "
            "Upload a file above to replace it."
        )

    sheet_name = None
    if not is_csv(filename):
        try:
            sheets = list_sheets(content)
        except Exception as exc:
            st.error(f"Could not read workbook: {exc}")
            return None, None
        default_idx = sheets.index(DEFAULT_SHEET) if DEFAULT_SHEET in sheets else 0
        sheet_name = st.selectbox("Sheet", sheets, index=default_idx, key="sheet")

    if sniff_data_format(content, filename) == "aggregated":
        try:
            df_full = parse_aggregated_csv(content)
        except Exception as exc:
            st.error(f"Could not parse aggregated CSV: {exc}")
            return None, None
        st.caption(
            "Pre-aggregated daily export — counts are summed across sellers "
            "for day/week/month views."
        )
        return None, _apply_filters_agg(df_full)

    try:
        df_full = parse_and_enrich(content, filename, sheet_name)
    except ValueError as exc:
        st.error(str(exc))
        return None, None
    except Exception as exc:
        st.error(f"Could not parse file: {exc}")
        return None, None

    return _apply_filters(df_full), None


def _apply_filters(df_full: pd.DataFrame) -> Optional[pd.DataFrame]:
    valid_dates = df_full["RequestDate"].dropna()
    if valid_dates.empty:
        st.warning("No valid Request_created_date values found.")
        return None

    min_date, max_date = valid_dates.min(), valid_dates.max()
    seller_options = sorted(df_full["seller_type"].dropna().astype(str).unique().tolist())

    date_col, seller_col = st.columns([1, 1])
    with date_col:
        date_range = st.date_input(
            "Request_created_date",
            value=(min_date, max_date),
            min_value=min_date, max_value=max_date,
            format="YYYY-MM-DD", key="date_range",
        )
    with seller_col:
        selected_sellers = st.multiselect(
            "seller_type", options=seller_options, default=seller_options,
            placeholder="All seller types", key="seller_filter",
        )

    df = df_full
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start, end = date_range
        df = df[(df["RequestDate"] >= start) & (df["RequestDate"] <= end)]
    if not selected_sellers:
        st.warning("Select at least one seller_type.")
        return None
    if len(selected_sellers) != len(seller_options):
        df = df[df["seller_type"].astype(str).isin(selected_sellers)]
    if df.empty:
        st.warning("No rows match the selected filters.")
        return None
    return df


def _render_github_save_section(df: pd.DataFrame) -> None:
    """Save-to-GitHub UI shown under upload mode after a successful parse."""
    if not ghs.is_configured():
        st.caption("💡 Configure `[github]` in secrets.toml to enable snapshot save.")
        return

    with st.expander("💾 Save snapshot to GitHub", expanded=False):
        d_min = str(df["RequestDate"].min())
        d_max = str(df["RequestDate"].max())
        default_label = f"{d_min} to {d_max}"
        label = st.text_input("Snapshot label", value=default_label,
                              key="snap_label",
                              help="Free-text identifier. Shown in dropdowns.")
        st.caption(f"Date range: **{d_min}** to **{d_max}** · "
                   f"Rows: **{len(df):,}** · "
                   f"Sellers: **{df['seller_type'].nunique()}**")

        if st.button("Save snapshot", type="primary", key="save_snap_btn"):
            existing = None
            try:
                existing = ghs.snapshot_exists_for_range(d_min, d_max)
            except Exception as exc:
                st.error(f"GitHub check failed: {exc}")
                return

            if existing and not st.session_state.get("_confirm_overwrite"):
                st.session_state["_confirm_overwrite"] = existing.snapshot_id
                st.warning(
                    f"A snapshot for **{d_min} to {d_max}** already exists "
                    f"(`{existing.label}`, uploaded {existing.uploaded_at}). "
                    "Click **Confirm overwrite** to replace, or **Save as new** "
                    "to keep both."
                )
                return
            else:
                _do_save(df, label, d_min, d_max, overwrite_id=None)

        if st.session_state.get("_confirm_overwrite"):
            c1, c2, c3 = st.columns(3)
            if c1.button("Confirm overwrite", type="primary",
                         key="confirm_overwrite_btn"):
                _do_save(df, label, d_min, d_max,
                         overwrite_id=st.session_state["_confirm_overwrite"])
                st.session_state.pop("_confirm_overwrite", None)
            if c2.button("Save as new", key="save_new_btn"):
                _do_save(df, label, d_min, d_max, overwrite_id=None)
                st.session_state.pop("_confirm_overwrite", None)
            if c3.button("Cancel", key="cancel_save_btn"):
                st.session_state.pop("_confirm_overwrite", None)
                st.rerun()


def _do_save(df: pd.DataFrame, label: str, d_min: str, d_max: str,
             overwrite_id: Optional[str]) -> None:
    try:
        with st.spinner("Building % tables..."):
            tables = build_snapshot_tables(df)
        with st.spinner("Uploading to GitHub..."):
            meta = ghs.save_snapshot(
                label=label,
                date_min=d_min, date_max=d_max,
                row_count=len(df),
                seller_types=sorted(df["seller_type"].dropna().astype(str).unique().tolist()),
                tables=tables,
                overwrite_id=overwrite_id,
            )
        ghs.clear_caches()
        st.success(f"Saved snapshot `{meta.snapshot_id}` "
                   f"({meta.row_count:,} rows, {len(tables)} tables).")
    except ghs.GitHubConfigError as exc:
        st.error(str(exc))
    except Exception as exc:
        st.error(f"Snapshot save failed: {exc}")


def _render_github_load_source() -> Optional[dict[str, pd.DataFrame]]:
    """Returns a dict of {table_name: % DataFrame} for the selected snapshot."""
    if not ghs.is_configured():
        st.error("GitHub is not configured. Add `[github]` to `.streamlit/secrets.toml`.")
        return None

    try:
        snaps = ghs.list_snapshots()
    except Exception as exc:
        st.error(f"Could not list snapshots: {exc}")
        return None

    if not snaps:
        st.info("No snapshots saved yet. Upload a file and save one first.")
        return None

    options = {
        f"{s.label} · {s.date_min}→{s.date_max} · {s.row_count:,} rows "
        f"({s.uploaded_at[:10]})": s
        for s in snaps
    }
    pick = st.selectbox("Snapshot", list(options.keys()), key="snap_pick")
    meta = options[pick]

    try:
        with st.spinner("Loading from GitHub..."):
            tables = ghs.load_snapshot(meta.snapshot_id)
    except Exception as exc:
        st.error(f"Load failed: {exc}")
        return None

    st.caption(
        f"Sellers in snapshot: {', '.join(meta.seller_types) or '—'}"
    )

    with st.expander("Danger zone", expanded=False):
        if st.button("Delete this snapshot", key="delete_snap"):
            try:
                ghs.delete_snapshot(meta.snapshot_id)
                ghs.clear_caches()
                st.success("Deleted. Refreshing...")
                st.rerun()
            except Exception as exc:
                st.error(f"Delete failed: {exc}")

    return tables


# ---------------------------------------------------------------------------
# Page renderers
# ---------------------------------------------------------------------------

def _source_picker() -> tuple[str, Optional[pd.DataFrame], Optional[pd.DataFrame], Optional[dict]]:
    """Top-of-page source selector. Returns (mode, raw_df, agg_df, snapshot_tables)."""
    with st.expander("Data & Filters", expanded=True):
        modes = ["Upload new file"]
        if ghs.is_configured():
            modes.append("Load from GitHub")
        mode = st.radio("Source", modes, horizontal=True, key="source_mode")

        if mode == "Upload new file":
            raw_df, agg_df = _render_upload_source()
            if raw_df is not None:
                _render_github_save_section(raw_df)
            return mode, raw_df, agg_df, None
        else:
            tables = _render_github_load_source()
            return mode, None, None, tables


def _repair_snapshot_table(t: pd.DataFrame, table_name: str) -> pd.DataFrame:
    """Repair legacy snapshots: force the right index name, coerce to numeric."""
    expected_idx = ghs._INDEX_COLS.get(table_name)
    if expected_idx and t.index.name != expected_idx:
        if expected_idx in t.columns:
            t = t.set_index(expected_idx)
        else:
            t = t.copy()
            t.index = t.index.rename(expected_idx)
    return t


def _render_loaded_snapshot(tables: dict[str, pd.DataFrame], page: str) -> None:
    """Display pre-computed % tables from a GitHub snapshot."""
    st.caption(
        "Showing pre-computed **percentage** tables from a saved snapshot. "
        "Counts and seller-type filters aren't available in snapshot mode — "
        "use Upload mode for that."
    )

    granularity = st.radio(
        "Granularity", ("Day", "Week", "Month"), horizontal=True,
        key=f"snap_gran_{page}",
    )
    g_prefix = {"Day": "daily", "Week": "weekly", "Month": "monthly"}[granularity]

    if page == "pickup":
        outc_key = f"{g_prefix}_outcome_pct"
        if outc_key in tables:
            st.subheader(f"{granularity}-wise pickup outcome (% of row)")
            t = _repair_snapshot_table(tables[outc_key], outc_key)
            t = t.apply(pd.to_numeric, errors="coerce")
            st.dataframe(t.style.format("{:.1f}%", na_rep="-"),
                         use_container_width=True)
        else:
            st.warning(f"Snapshot is missing `{outc_key}`.")

        if "conversion_by_seller" in tables:
            st.subheader("Return-pickup conversion by seller type")
            conv_t = _repair_snapshot_table(tables["conversion_by_seller"],
                                            "conversion_by_seller")
            conv_t = conv_t.apply(pd.to_numeric, errors="coerce")
            st.dataframe(conv_t, use_container_width=True)
    else:  # attempt
        att_key = f"{g_prefix}_attempt_pct"
        if att_key in tables:
            st.subheader(f"{granularity}-wise attempt (% of row)")
            t = _repair_snapshot_table(tables[att_key], att_key)
            t = t.apply(pd.to_numeric, errors="coerce")
            st.dataframe(t.style.format("{:.1f}%", na_rep="-"),
                         use_container_width=True)
        else:
            st.warning(f"Snapshot is missing `{att_key}`.")


def _render_agg_pickup_page(df: pd.DataFrame) -> None:
    ctrl_col1, ctrl_col2 = st.columns([1, 1])
    with ctrl_col1:
        granularity = st.radio(
            "Granularity", tuple(GRANULARITY_CONFIG.keys()),
            horizontal=True, key="agg_pickup_granularity",
        )
    with ctrl_col2:
        display_mode = st.radio(
            "Display values as", ("Counts", "% of row"),
            horizontal=True, key="agg_pickup_display_mode",
        )
    as_percent = display_mode == "% of row"
    period_col, label_fn, index_label = GRANULARITY_CONFIG[granularity]

    st.subheader(f"{granularity}-wise pickup outcome (cumulative D-buckets)")
    day_outcome = _agg_sum_pivot(df, period_col, AGG_OUTCOME_COUNT, OUTCOME_ORDER)
    day_outcome.index = day_outcome.index.map(label_fn)
    day_outcome.index.name = index_label
    display_pivot(day_outcome, as_percent)

    st.subheader("Seller-type-wise pickup outcome (cumulative D-buckets)")
    seller_outcome = _agg_sum_pivot(df, "seller_type", AGG_OUTCOME_COUNT, OUTCOME_ORDER)
    seller_outcome.index.name = "seller_type"
    display_pivot(seller_outcome, as_percent)

    st.subheader("Return-pickup conversion by seller type")
    conv = _agg_conversion_table(df)
    conv.index.name = "seller_type"
    st.dataframe(conv, use_container_width=True)


def _render_agg_attempt_page(df: pd.DataFrame) -> None:
    ctrl_col1, ctrl_col2 = st.columns([1, 1])
    with ctrl_col1:
        granularity = st.radio(
            "Granularity", tuple(GRANULARITY_CONFIG.keys()),
            horizontal=True, key="agg_attempt_granularity",
        )
    with ctrl_col2:
        display_mode = st.radio(
            "Display values as", ("Counts", "% of row"),
            horizontal=True, key="agg_attempt_display_mode",
        )
    as_percent = display_mode == "% of row"
    period_col, label_fn, index_label = GRANULARITY_CONFIG[granularity]

    st.subheader(f"{granularity}-wise attempt pivot (cumulative D-buckets)")
    day_pivot = _agg_sum_pivot(df, period_col, AGG_ATTEMPT_COUNT, ATTEMPT_ORDER)
    day_pivot.index = day_pivot.index.map(label_fn)
    day_pivot.index.name = index_label
    display_pivot(day_pivot, as_percent)

    st.subheader("Seller-type-wise attempt pivot (cumulative D-buckets)")
    seller_pivot = _agg_sum_pivot(df, "seller_type", AGG_ATTEMPT_COUNT, ATTEMPT_ORDER)
    seller_pivot.index.name = "seller_type"
    display_pivot(seller_pivot, as_percent)


def render_pickup_page() -> None:
    render_seller_summary_page("pickup")


def render_attempt_page() -> None:
    render_seller_summary_page("attempt")


def render_pickup_period_page() -> None:
    render_period_detail_page("pickup")


def render_attempt_period_page() -> None:
    render_period_detail_page("attempt")


# ---------------------------------------------------------------------------
# Pincode Performance page: client x BIC/Non-BIC pickup performance
# ---------------------------------------------------------------------------

def _render_bic_upload() -> Optional[Tuple[frozenset, str]]:
    """Upload-and-save flow. Returns (bic_set, source_label) or None."""
    bic_file = st.file_uploader(
        "Upload BIC pincode CSV (a single column named `Pincode`)",
        type=["csv"], key="bic_uploader",
    )
    if bic_file is not None:
        st.session_state["_bic_bytes"] = bic_file.getvalue()
        st.session_state["_bic_name"] = bic_file.name

    bic_bytes = st.session_state.get("_bic_bytes")
    bic_name = st.session_state.get("_bic_name")
    if bic_bytes is None:
        st.info("Upload a BIC pincode CSV to continue.")
        return None

    try:
        bic_set = load_bic_pincodes(bic_bytes)
    except Exception as exc:
        st.error(f"Could not read BIC pincode CSV: {exc}")
        return None

    info_col, clear_col = st.columns([6, 1])
    info_col.caption(
        f"Using `{bic_name}` — {len(bic_set):,} unique BIC pincodes loaded."
    )
    if clear_col.button("Clear", key="bic_clear",
                        help="Remove the uploaded BIC pincode file"):
        st.session_state.pop("_bic_bytes", None)
        st.session_state.pop("_bic_name", None)
        st.rerun()

    if ghs.is_configured():
        _render_bic_save_section(bic_set, bic_name or "")

    return bic_set, bic_name or "uploaded"


def _render_bic_save_section(bic_set: frozenset, source_filename: str) -> None:
    """Save-to-GitHub UI for the currently loaded BIC list."""
    with st.expander("Save BIC list to GitHub", expanded=False):
        default_label = (source_filename.rsplit(".", 1)[0]
                         if source_filename else "BIC list")
        label = st.text_input("BIC list label", value=default_label,
                              key="bic_save_label",
                              help="Free-text identifier. Shown in dropdowns.")
        st.caption(f"{len(bic_set):,} unique pincodes will be saved.")

        if st.button("Save BIC list", type="primary", key="bic_save_btn"):
            try:
                existing = ghs.bic_list_exists_for_label(label)
            except Exception as exc:
                st.error(f"GitHub check failed: {exc}")
                return

            if existing and not st.session_state.get("_confirm_bic_overwrite"):
                st.session_state["_confirm_bic_overwrite"] = existing.bic_id
                st.warning(
                    f"A BIC list labeled **{label}** already exists "
                    f"(`{existing.bic_id}`, uploaded {existing.uploaded_at}). "
                    "Click **Confirm overwrite** to replace, or **Save as new** "
                    "to keep both."
                )
                return
            _do_bic_save(bic_set, label, source_filename, overwrite_id=None)

        if st.session_state.get("_confirm_bic_overwrite"):
            c1, c2, c3 = st.columns(3)
            if c1.button("Confirm overwrite", type="primary",
                         key="confirm_bic_overwrite_btn"):
                _do_bic_save(
                    bic_set,
                    st.session_state.get("bic_save_label", default_label),
                    source_filename,
                    overwrite_id=st.session_state["_confirm_bic_overwrite"],
                )
                st.session_state.pop("_confirm_bic_overwrite", None)
            if c2.button("Save as new", key="bic_save_new_btn"):
                _do_bic_save(
                    bic_set,
                    st.session_state.get("bic_save_label", default_label),
                    source_filename, overwrite_id=None,
                )
                st.session_state.pop("_confirm_bic_overwrite", None)
            if c3.button("Cancel", key="bic_cancel_save_btn"):
                st.session_state.pop("_confirm_bic_overwrite", None)
                st.rerun()


def _do_bic_save(bic_set: frozenset, label: str, source_filename: str,
                 overwrite_id: Optional[str]) -> None:
    try:
        with st.spinner("Uploading BIC list to GitHub..."):
            meta = ghs.save_bic_list(
                label=label,
                pincodes=sorted(bic_set),
                source_filename=source_filename,
                overwrite_id=overwrite_id,
            )
        ghs.clear_bic_caches()
        st.success(
            f"Saved BIC list `{meta.bic_id}` "
            f"({meta.pincode_count:,} pincodes)."
        )
    except ghs.GitHubConfigError as exc:
        st.error(str(exc))
    except Exception as exc:
        st.error(f"BIC list save failed: {exc}")


def _render_bic_github_load() -> Optional[Tuple[frozenset, str]]:
    """Load-from-GitHub picker. Returns (bic_set, label) or None."""
    if not ghs.is_configured():
        st.error("GitHub is not configured.")
        return None

    try:
        lists = ghs.list_bic_lists()
    except Exception as exc:
        st.error(f"Could not list BIC lists: {exc}")
        return None

    if not lists:
        st.info(
            "No BIC lists saved on GitHub yet. Switch to **Upload BIC CSV** "
            "to save your first one."
        )
        return None

    options = {
        f"{m.label} · {m.pincode_count:,} pincodes ({m.uploaded_at[:10]})": m
        for m in lists
    }
    pick = st.selectbox("BIC list", list(options.keys()), key="bic_gh_pick")
    meta = options[pick]

    try:
        with st.spinner("Loading BIC list from GitHub..."):
            bic_set = ghs.load_bic_list(meta.bic_id)
    except Exception as exc:
        st.error(f"Load failed: {exc}")
        return None

    if not bic_set:
        st.warning("That BIC list is empty.")
        return None

    st.caption(
        f"Loaded **{meta.label}** — {len(bic_set):,} pincodes "
        f"(saved {meta.uploaded_at})."
    )

    with st.expander("Danger zone", expanded=False):
        if st.button("Delete this BIC list", key="delete_bic_btn"):
            try:
                ghs.delete_bic_list(meta.bic_id)
                ghs.clear_bic_caches()
                st.success("Deleted. Refreshing...")
                st.rerun()
            except Exception as exc:
                st.error(f"Delete failed: {exc}")

    return bic_set, meta.label


def _get_bic_set() -> Optional[Tuple[frozenset, str]]:
    """Resolve a BIC pincode set from either Upload or GitHub. Returns
    `(bic_set, source_label)` or `None` if not yet ready."""
    st.subheader("BIC pincode list")
    sources = ["Upload BIC CSV"]
    if ghs.is_configured():
        sources.append("Load from GitHub")
    if len(sources) == 1:
        return _render_bic_upload()

    source = st.radio(
        "BIC source", sources, horizontal=True, key="bic_source_mode",
    )
    if source == "Upload BIC CSV":
        return _render_bic_upload()
    return _render_bic_github_load()


def render_pincode_performance_page() -> None:
    st.title("Pincode Performance")
    st.caption(
        "Pickup performance for one chosen client (seller_type), sliced by "
        "whether the destination pincode is in the uploaded BIC list. "
        "D0..D4+ are cumulative — D2 includes D0-D1, etc."
    )

    mode, df, agg_df, _snap = _source_picker()
    if mode == "Load from GitHub":
        st.info(
            "Pincode Performance needs raw shipment rows. Switch the source "
            "above to **Upload new file** to use this page."
        )
        return

    if agg_df is not None:
        st.info(
            "Pincode Performance needs raw shipment rows with `src_pincode`. "
            "Upload a raw RVP export to use this page."
        )
        return

    if df is None:
        return

    if "src_pincode" not in df.columns:
        st.warning(
            "The uploaded shipment file does not contain a "
            "`src_pincode` column, so pincode performance cannot be "
            "computed. Re-upload a file that includes it."
        )
        return

    bic_result = _get_bic_set()
    if bic_result is None:
        return
    bic_set, _bic_source_label = bic_result

    df = df.assign(is_bic=df["src_pincode"].isin(bic_set))

    clients = sorted(df["seller_type"].dropna().astype(str).unique().tolist())
    if not clients:
        st.warning("No clients available in the current filter window.")
        return

    st.caption(
        "Seller filters live in the **Data & Filters** panel above. The date "
        "range and granularity below further narrow this page."
    )

    valid_dates = df["RequestDate"].dropna()
    if valid_dates.empty:
        st.warning("No valid Request_created_date values found.")
        return
    pin_min, pin_max = valid_dates.min(), valid_dates.max()

    fc1, fc2 = st.columns([2, 1])
    with fc1:
        page_date_range = st.date_input(
            "Date range",
            value=(pin_min, pin_max),
            min_value=pin_min, max_value=pin_max,
            format="YYYY-MM-DD", key="pin_date_range",
        )
    with fc2:
        granularity = st.radio(
            "Granularity", tuple(GRANULARITY_CONFIG.keys()),
            horizontal=True, key="pin_granularity",
        )
    period_col, label_fn, index_label = GRANULARITY_CONFIG[granularity]

    if isinstance(page_date_range, tuple) and len(page_date_range) == 2:
        start, end = page_date_range
        df = df[(df["RequestDate"] >= start) & (df["RequestDate"] <= end)]
        if df.empty:
            st.warning("No rows match the selected date range.")
            return

    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        client = st.selectbox("Client (seller_type)", clients, key="pin_client")
    with c2:
        segment = st.radio(
            "Pincode segment", ("All", "BIC", "Non-BIC"),
            horizontal=True, key="pin_segment",
        )
    with c3:
        display_mode = st.radio(
            "Display values as", ("Counts", "% of row"),
            horizontal=True, key="pin_display_mode",
        )
    as_percent = display_mode == "% of row"

    client_df = df[df["seller_type"].astype(str) == client]
    if client_df.empty:
        st.warning(f"No shipments for client `{client}` in the current window.")
        return

    if segment == "BIC":
        view_df = client_df[client_df["is_bic"]]
    elif segment == "Non-BIC":
        view_df = client_df[~client_df["is_bic"]]
    else:
        view_df = client_df

    if view_df.empty:
        st.warning(
            f"No shipments for client `{client}` in segment `{segment}` "
            "match the current filters."
        )
        return

    rvp_done = view_df["rvp_pickup_completed_date"].astype("string").str.strip()
    picked_mask = rvp_done.notna() & (rvp_done != "") & (rvp_done.str.lower() != "nan")
    picked = int(picked_mask.sum())
    total = len(view_df)
    conv_pct = (picked / total * 100) if total else 0.0
    unique_pins = view_df["src_pincode"].dropna().nunique()

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total shipments", f"{total:,}")
    m2.metric("Picked shipments", f"{picked:,}")
    m3.metric("Conversion %", f"{conv_pct:.2f}%")
    m4.metric("Unique pincodes", f"{unique_pins:,}")

    view_sig = _df_signature(view_df)

    st.subheader(
        f"{granularity}-wise pickup outcome — {client} ({segment}) "
        "(cumulative D-buckets)"
    )
    time_outcome = cached_pivot(view_df, view_sig, period_col,
                                "PickupOutcome", tuple(OUTCOME_ORDER))
    time_outcome = cumulate_d_buckets(time_outcome)
    time_outcome.index = time_outcome.index.map(label_fn)
    time_outcome.index.name = index_label
    display_pivot(time_outcome, as_percent)

    # Drop rows where src_pincode is missing so the index is clean.
    pin_view = view_df.dropna(subset=["src_pincode"])
    pin_view = pin_view[pin_view["src_pincode"].astype(str) != ""]
    if pin_view.empty:
        st.warning("No shipments with a valid src_pincode in this view.")
        return

    sig = _df_signature(pin_view)

    st.subheader(
        f"Pincode-wise pickup outcome — {client} ({segment}) "
        "(cumulative D-buckets)"
    )

    top_n = st.number_input(
        "Show top N pincodes (by volume)", min_value=10, max_value=5000,
        value=min(100, max(10, unique_pins)), step=10,
        key="pin_top_n",
        help="Pincodes are sorted by total shipments. Increase to see more.",
    )

    pincode_outcome = cached_pivot(
        pin_view, sig, "src_pincode", "PickupOutcome",
        tuple(OUTCOME_ORDER),
    )
    pincode_outcome = cumulate_d_buckets(pincode_outcome)
    # Keep Grand Total at the bottom; sort the body by total volume desc.
    if "Grand Total" in pincode_outcome.index:
        gt = pincode_outcome.loc[["Grand Total"]]
        body = pincode_outcome.drop(index="Grand Total")
        body = body.sort_values("Grand Total", ascending=False).head(int(top_n))
        pincode_outcome = pd.concat([body, gt])
    pincode_outcome.index = pincode_outcome.index.astype(str)
    pincode_outcome.index.name = "src_pincode"
    display_pivot(pincode_outcome, as_percent)

    bic_view = client_df.assign(
        bic_segment=client_df["is_bic"].map({True: "BIC", False: "Non-BIC"})
    )
    bic_sig = _df_signature(bic_view)

    st.subheader(
        f"BIC vs Non-BIC pickup outcome — {client} (cumulative D-buckets)"
    )
    bic_outcome = cached_pivot(bic_view, bic_sig, "bic_segment",
                               "PickupOutcome", tuple(OUTCOME_ORDER))
    bic_outcome = cumulate_d_buckets(bic_outcome)
    bic_outcome.index.name = "BIC segment"
    display_pivot(bic_outcome, as_percent)

    st.subheader(f"Return-pickup conversion — {client} by BIC segment")
    conv = conversion_table(bic_view, group_col="bic_segment")
    conv.index.name = "BIC segment"
    st.dataframe(conv, use_container_width=True)

    def _build_pincode_workbook() -> bytes:
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            time_outcome.to_excel(writer, sheet_name=f"{granularity}-wise outcome")
            pincode_outcome.to_excel(writer, sheet_name="Pincode-wise outcome")
            bic_outcome.to_excel(writer, sheet_name="BIC vs Non-BIC outcome")
            conv.to_excel(writer, sheet_name="Conversion by BIC")
        return buf.getvalue()

    st.download_button(
        "Download pincode tables as Excel (counts)",
        data=_build_pincode_workbook(),
        file_name=(
            f"rvp_pincode_performance_{client}_{segment}_"
            f"{granularity.lower()}.xlsx"
        ),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---------------------------------------------------------------------------
# Trends page: stitch every snapshot together
# ---------------------------------------------------------------------------

def _concat_trend_table(table_name: str) -> Optional[pd.DataFrame]:
    """Pull `table_name` from every snapshot, concatenate, dedupe on the index.

    Drops the per-row 'Grand Total' (always 100, noisy) and the 'Grand Total'
    column. Returns a numeric-only frame sorted chronologically.

    Resilient to legacy snapshots saved before the index-name fix: forces the
    expected index name in those cases so dedupe still works.
    """
    snaps = ghs.list_snapshots()
    if not snaps:
        return None

    # The expected index column for each snapshot table.
    expected_idx = ghs._INDEX_COLS.get(table_name, "period")

    frames = []
    for s in snaps:
        try:
            tables = ghs.load_snapshot(s.snapshot_id)
        except Exception:
            continue
        if table_name not in tables:
            continue
        t = tables[table_name].copy()
        # Drop the always-100 totals row and column.
        t = t.drop(index=[i for i in t.index if str(i) == "Grand Total"], errors="ignore")
        t = t.drop(columns=[c for c in ("Grand Total",) if c in t.columns], errors="ignore")
        # Legacy snapshots may not have a named index — force it so concat+dedupe works.
        if t.index.name != expected_idx:
            t.index = t.index.rename(expected_idx)
        # Also handle the case where the index column is sitting as a regular column.
        if expected_idx in t.columns:
            t = t.set_index(expected_idx)
        t["__uploaded_at__"] = s.uploaded_at
        frames.append(t)
    if not frames:
        return None
    out = pd.concat(frames)
    out = out.reset_index().sort_values("__uploaded_at__")
    # After reset_index the index column should be named expected_idx, but if
    # the source frame had a default RangeIndex we may end up with a column
    # called 'index' instead. Handle either.
    if expected_idx not in out.columns:
        if "index" in out.columns:
            out = out.rename(columns={"index": expected_idx})
        else:
            # Last resort: first column is the old index.
            out = out.rename(columns={out.columns[0]: expected_idx})
    out = out.drop_duplicates(subset=[expected_idx], keep="last")
    out = out.drop(columns=["__uploaded_at__"]).set_index(expected_idx)
    # Coerce every value column to numeric (Parquet round-trip can produce strings).
    out = out.apply(pd.to_numeric, errors="coerce")
    return out


def _sort_index_chronologically(df: pd.DataFrame, granularity: str) -> pd.DataFrame:
    """Sort the index by the date it represents. Returns df unchanged on failure."""
    try:
        if granularity == "Day":
            order = pd.to_datetime(df.index, format=DATE_FMT, errors="coerce")
        elif granularity == "Week":
            firsts = df.index.to_series().str.split(" to ").str[0]
            order = pd.to_datetime(firsts, format=DATE_FMT, errors="coerce")
        else:  # Month
            order = pd.to_datetime(df.index, format="%b %Y", errors="coerce")
        return df.assign(_o=order).sort_values("_o").drop(columns=["_o"])
    except Exception:
        return df


def render_trends_page() -> None:
    st.title("Trends across snapshots")

    if not ghs.is_configured():
        st.error(
            "Trends requires GitHub to be configured. Add `[github]` to "
            "`.streamlit/secrets.toml`."
        )
        return

    try:
        snaps = ghs.list_snapshots()
    except Exception as exc:
        st.error(f"Could not list snapshots: {exc}")
        return

    if not snaps:
        st.info("No snapshots yet. Upload data and save a snapshot first.")
        return

    st.caption(
        f"Stitched from **{len(snaps)}** snapshot(s). "
        f"When two snapshots cover the same period, the most recently uploaded wins."
    )

    # --- Controls ---
    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        granularity = st.radio("Granularity", ("Day", "Week", "Month"),
                               horizontal=True, key="trend_gran", index=1)
    with c2:
        metric = st.radio("Metric", ("Pickup outcome", "Attempts"),
                          horizontal=True, key="trend_metric")

    g_prefix = {"Day": "daily", "Week": "weekly", "Month": "monthly"}[granularity]
    table_name = (f"{g_prefix}_outcome_pct"
                  if metric == "Pickup outcome" else f"{g_prefix}_attempt_pct")

    with st.spinner("Loading all snapshots..."):
        merged = _concat_trend_table(table_name)
    if merged is None or merged.empty:
        st.warning("No snapshots contain this table.")
        return
    merged = _sort_index_chronologically(merged, granularity)

    # The column picker — show what's actually in the data.
    available_cols = [c for c in merged.columns]
    default_cols = (
        [c for c in ("D0", "D1", "D2", "D3", "D4+") if c in available_cols]
        if metric == "Pickup outcome"
        else [c for c in ("D0", "D1", "D2") if c in available_cols]
    )
    with c3:
        picked = st.multiselect(
            "Show these series",
            options=available_cols,
            default=default_cols or available_cols[:3],
            key="trend_cols",
            help="Cumulative D-buckets: D2 = converted within 2 days, etc.",
        )

    if not picked:
        st.info("Pick at least one series to chart.")
        return

    chart_df = merged[picked]
    chart_df.index.name = {"Day": "Date", "Week": "Week", "Month": "Month"}[granularity]

    st.subheader(f"{metric} % over time ({granularity.lower()}) — selected series")
    st.dataframe(
        chart_df.style.format("{:.1f}%", na_rep="-"),
        use_container_width=True,
    )

    st.subheader("Full table (all columns)")
    st.dataframe(
        merged.style.format("{:.1f}%", na_rep="-"),
        use_container_width=True,
    )

    # --- Conversion by seller across snapshots ---
    st.divider()
    st.subheader("Conversion % by seller (across snapshots)")
    conv_long = []
    for s in snaps:
        try:
            tables = ghs.load_snapshot(s.snapshot_id)
        except Exception:
            continue
        if "conversion_by_seller" not in tables:
            continue
        c = tables["conversion_by_seller"].copy()
        if "Conversion %" not in c.columns:
            continue
        # The seller_type names live in the *index*, not a column.
        for seller in c.index:
            if str(seller) == "Grand Total":
                continue
            try:
                pct = float(c.loc[seller, "Conversion %"])
            except (TypeError, ValueError):
                continue
            conv_long.append({
                "snapshot": f"{s.date_min} → {s.date_max}",
                "uploaded_at": s.uploaded_at,
                "seller_type": str(seller),
                "Conversion %": pct,
            })

    if not conv_long:
        st.caption("No conversion data found across snapshots.")
        return

    conv_df = pd.DataFrame(conv_long).sort_values("uploaded_at")
    pivoted = conv_df.pivot_table(
        index="snapshot", columns="seller_type",
        values="Conversion %", aggfunc="last",
    )
    # Preserve upload order for the x-axis.
    snap_order = conv_df.drop_duplicates("snapshot")["snapshot"].tolist()
    pivoted = pivoted.reindex(snap_order)
    pivoted.index.name = "Snapshot (date range)"

    seller_options = list(pivoted.columns)
    picked_sellers = st.multiselect(
        "Seller types to show",
        options=seller_options,
        default=seller_options,
        key="trend_sellers",
    )
    if not picked_sellers:
        st.info("Pick at least one seller_type.")
        return

    sub = pivoted[picked_sellers]
    st.dataframe(
        sub.style.format("{:.2f}%", na_rep="-"),
        use_container_width=True,
    )


def _load_simple_table_data() -> tuple[Optional[pd.DataFrame], str]:
    """Load aggregated CSV from session upload (if any) or data/ folder."""
    content = st.session_state.get("_file_bytes")
    filename = st.session_state.get("_file_name")
    if content is not None and filename is not None:
        source = filename
    else:
        default = _default_dataset()
        if default is None:
            return None, ""
        content, filename, path = default
        source = str(path.relative_to(APP_DIR))

    if sniff_data_format(content, filename) != "aggregated":
        st.error(
            "This view needs the aggregated CSV format "
            "(seller_type, day, total_shipments, pickup_*, attempt_*)."
        )
        return None, source

    try:
        return parse_aggregated_csv(content), source
    except Exception as exc:
        st.error(f"Could not read CSV: {exc}")
        return None, source


def summarize_metrics(
    df: pd.DataFrame,
    start,
    end,
    sum_cols: list[str],
    include_grand_total: bool,
    as_percent: bool = False,
    conversion_col: Optional[str] = None,
) -> pd.DataFrame:
    """One row per seller_type with counts summed over the selected period."""
    sum_cols = [c for c in sum_cols if c in df.columns]
    if not sum_cols:
        raise ValueError("No metric columns found in the data.")

    grouped = df.groupby("seller_type", as_index=False)[sum_cols].sum()
    range_label = f"{format_date(start)} to {format_date(end)}"
    grouped.insert(0, "date_range", range_label)

    if include_grand_total and len(grouped) > 1:
        totals = grouped[sum_cols].sum()
        gt_row: dict = {
            "date_range": range_label,
            "seller_type": "Grand Total",
            **totals.to_dict(),
        }
        grouped = pd.concat([grouped, pd.DataFrame([gt_row])], ignore_index=True)

    if as_percent:
        out = grouped[["date_range", "seller_type", "total_shipments"]].copy()
        ts = grouped["total_shipments"].replace(0, pd.NA)
        for col in sum_cols:
            if col == "total_shipments":
                continue
            out[col] = (grouped[col] / ts * 100).round(1)
        if conversion_col:
            out["Conversion %"] = (grouped[conversion_col] / ts * 100).round(2)
        return out

    return grouped[["date_range", "seller_type"] + sum_cols]


# ---------------------------------------------------------------------------
# Report UI (seller summary + period drill-down)
# ---------------------------------------------------------------------------

_DASHBOARD_CSS = """
<style>
.kpi-row { display:flex; gap:12px; margin:0 0 1rem; flex-wrap:wrap; }
.kpi-card {
  flex:1; min-width:150px; background:#fff; border-radius:8px;
  padding:14px 16px; border-top:4px solid #ccc;
  box-shadow:0 1px 3px rgba(0,0,0,.08);
}
.kpi-card h4 { margin:0; font-size:.72rem; color:#666; text-transform:uppercase; letter-spacing:.03em; }
.kpi-card .val { font-size:1.55rem; font-weight:700; margin:4px 0 2px; line-height:1.2; }
.kpi-card .sub { font-size:.68rem; color:#888; margin:0; }
.legend-box { font-size:.78rem; color:#555; margin:.25rem 0 .75rem; }
</style>
"""

_PICKUP_PCT_METRIC_CANDIDATES = [
    ("D0 %", ("pickup_d0_pct", "pickup_d0")),
    ("D1 cum %", ("pickup_d1_cum_pct", "pickup_d1_cum")),
    ("D2 cum %", ("pickup_d2_cum_pct", "pickup_d2_cum")),
    ("QC Failed %", ("qc_failed_pct", "pickup_qc_failed_pct", "qc_failed", "pickup_qc_failed")),
    ("Not Attempted %", ("not_attempted_pct", "pickup_not_attempted_pct", "not_attempted", "pickup_not_attempted")),
    ("Pending %", ("pending_pct", "pickup_pending_pct", "pending", "pickup_pending")),
    ("Conversion %", ("converted_pct", "converted", "pickup_d4plus_cum_pct", "pickup_d4plus_cum")),
    ("Cancelled After D2 %", ("cancelled_pct", "cancelled_after_d2")),
    ("Cancelled D2 cum %", ("cancelled_d2_cum_pct", "cancelled_d2_cum")),
    ("Cancelled D2 %", ("cancelled_d2_pct", "cancelled_d2")),  # legacy column name
]
_ATTEMPT_PCT_METRIC_CANDIDATES = [
    ("Attempt D0 %", ("attempt_d0_pct", "attempt_d0")),
    ("Attempt D1 cum %", ("attempt_d1_cum_pct", "attempt_d1_cum")),
    ("Attempt D2 cum %", ("attempt_d2_cum_pct", "attempt_d2_cum")),
    ("Attempt QC Failed %", ("attempt_qc_failed_pct", "attempt_qc_failed")),
    ("Attempt Not Attempted %", ("attempt_not_attempted_pct", "attempt_not_attempted")),
    ("Cancelled After D2 %", ("attempt_cancelled_pct", "attempt_cancelled", "cancelled_pct", "cancelled_after_d2")),
    ("Cancelled D2 cum %", ("cancelled_d2_cum_pct", "cancelled_d2_cum")),
    ("Cancelled D2 %", ("cancelled_d2_pct", "cancelled_d2")),  # legacy column name
]


def _pct_metrics_for_df(
    df: pd.DataFrame,
    candidates: list[tuple[str, tuple[str, ...]]],
) -> list[tuple[str, str]]:
    specs: list[tuple[str, str]] = []
    for label, names in candidates:
        col = _first_col(df, *names)
        if col:
            specs.append((label, col))
    return specs


def _pickup_pct_metrics(df: pd.DataFrame) -> list[tuple[str, str]]:
    return _pct_metrics_for_df(df, _PICKUP_PCT_METRIC_CANDIDATES)


def _attempt_pct_metrics(df: pd.DataFrame) -> list[tuple[str, str]]:
    return _pct_metrics_for_df(df, _ATTEMPT_PCT_METRIC_CANDIDATES)


def _inject_dashboard_css() -> None:
    st.markdown(_DASHBOARD_CSS, unsafe_allow_html=True)


def _kpi_metric_labels(pct_specs: list[tuple[str, str]]) -> list[str]:
    """Keep Conversion + Cancelled visible even when other outcome buckets exist."""
    labels = [label for label, _ in pct_specs]
    ordered: list[str] = []
    for preferred in ("Conversion %", "Cancelled After D2 %", "D2 cum %", "Attempt D2 cum %"):
        if preferred in labels and preferred not in ordered:
            ordered.append(preferred)
    for label in labels:
        if label not in ordered:
            ordered.append(label)
    return ordered[:5]


def _week_label_bounded(week_start, range_start, range_end) -> str:
    week_end = week_start + timedelta(days=6)
    label = _week_label(week_start)
    if range_start > week_start or range_end < week_end:
        actual_s = max(week_start, range_start)
        actual_e = min(week_end, range_end)
        label += f" ({format_date(actual_s)} to {format_date(actual_e)})"
    return label


def _month_label_bounded(month_start, range_start, range_end) -> str:
    month_end = (pd.Timestamp(month_start) + pd.offsets.MonthEnd(0)).date()
    label = _month_label(month_start)
    if range_start > month_start or range_end < month_end:
        actual_s = max(month_start, range_start)
        actual_e = min(month_end, range_end)
        label += f" ({format_date(actual_s)} to {format_date(actual_e)})"
    return label


def _metric_numerator(sums: dict[str, float], label: str, col: str) -> float:
    """Count used for a % metric. Legacy exports under-count `cancelled`."""
    if label != "Cancelled After D2 %":
        return sums.get(col, 0) or 0
    if "converted" in sums:
        return sums.get(col, 0) or 0
    vol = sums.get("total_shipments", 0) or 0
    if "attempt_d4plus_cum" in sums:
        return max(vol - (sums.get("attempt_d4plus_cum", 0) or 0), 0)
    if "pickup_d4plus_cum" in sums:
        return max(vol - (sums.get("pickup_d4plus_cum", 0) or 0), 0)
    return sums.get(col, 0) or 0


def _view_metric_numerator(view: pd.DataFrame, label: str, col: str) -> float:
    if col.endswith("_pct"):
        return (view[col] * view["total_shipments"]).sum() / 100
    if label == "Cancelled After D2 %" and "converted" not in view.columns:
        vol = view["total_shipments"].sum()
        if "attempt_d4plus_cum" in view.columns:
            return max(vol - view["attempt_d4plus_cum"].sum(), 0)
        picked_col = _first_col(view, "converted", "pickup_d4plus_cum")
        if picked_col:
            return max(vol - view[picked_col].sum(), 0)
    return view[col].sum() if col in view.columns else 0


def _metrics_from_group(
    group: pd.DataFrame, pct_specs: list[tuple[str, str]],
) -> dict:
    vol = group["total_shipments"].sum()
    row: dict = {"Volume": int(vol)}
    for label, col in pct_specs:
        num = _view_metric_numerator(group, label, col)
        row[label] = round(num / vol * 100, 2) if vol else 0.0
    return row


def _metrics_from_sums(sums: dict[str, float], pct_specs: list[tuple[str, str]]) -> dict:
    vol = sums.get("total_shipments", 0) or 0
    row: dict = {"Volume": int(vol)}
    for label, col in pct_specs:
        num = _metric_numerator(sums, label, col)
        row[label] = round(num / vol * 100, 2) if vol else 0.0
    return row


def _order_metric_labels(labels: list[str]) -> list[str]:
    """Place cancellation metrics immediately after the D2 bucket."""
    cancelled = [l for l in labels if "Cancelled" in l]
    remaining = [l for l in labels if l not in cancelled]
    d2_index = next(
        (i for i, label in enumerate(remaining) if label in {"D2 cum %", "Attempt D2 cum %"}),
        len(remaining) - 1,
    )
    return remaining[: d2_index + 1] + cancelled + remaining[d2_index + 1 :]


def _build_seller_summary(
    view: pd.DataFrame, sum_cols: list[str], pct_specs: list[tuple[str, str]],
) -> pd.DataFrame:
    rows = []
    for seller, grp in view.groupby("seller_type", sort=True):
        sums = {c: grp[c].sum() for c in sum_cols if c in grp.columns}
        row = _metrics_from_group(grp, pct_specs)
        row["Seller"] = str(seller)
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    metric_labels = _order_metric_labels([label for label, _ in pct_specs])
    cols = ["Seller", "Volume"] + metric_labels
    return out[cols].sort_values("Volume", ascending=False)


def _build_period_detail(
    view: pd.DataFrame,
    range_start,
    range_end,
    sum_cols: list[str],
    pct_specs: list[tuple[str, str]],
    granularity: str,
) -> pd.DataFrame:
    if granularity == "Daily":
        period_col = "RequestDate"
        label_fn = lambda k, _: format_date(k)  # noqa: E731
    elif granularity == "Weekly":
        period_col = "WeekStart"
        label_fn = lambda k, _: _week_label_bounded(k, range_start, range_end)  # noqa: E731
    else:
        period_col = "MonthStart"
        label_fn = lambda k, _: _month_label_bounded(k, range_start, range_end)  # noqa: E731

    rows = []
    for (period_key, seller), grp in view.groupby([period_col, "seller_type"], sort=True):
        sums = {c: grp[c].sum() for c in sum_cols if c in grp.columns}
        row = _metrics_from_group(grp, pct_specs)
        row["Period"] = label_fn(period_key, grp)
        row["Seller"] = str(seller)
        row["_period_sort"] = pd.Timestamp(period_key)
        rows.append(row)

    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    metric_labels = _order_metric_labels([label for label, _ in pct_specs])
    cols = ["Period", "Seller", "Volume"] + metric_labels
    return out.sort_values(["_period_sort", "Seller"]).drop(columns="_period_sort")[cols]


def _pct_cell_style(val, col: str) -> str:
    if pd.isna(val):
        return ""
    higher_better = col in (
        "Conversion %", "D0 %", "D1 cum %", "D2 cum %",
        "Attempt D0 %", "Attempt D1 cum %", "Attempt D2 cum %",
    )
    if higher_better:
        if val >= 70:
            return "background-color:#d4edda;color:#155724"
        if val >= 50:
            return "background-color:#fff3cd;color:#856404"
        return "background-color:#f8d7da;color:#721c24"
    if val <= 5:
        return "background-color:#d4edda;color:#155724"
    if val <= 10:
        return "background-color:#fff3cd;color:#856404"
    return "background-color:#f8d7da;color:#721c24"


def _style_report_table(df: pd.DataFrame, pct_cols: list[str]):
    fmt = {"Volume": "{:,}"}
    fmt.update({c: "{:.2f}%" for c in pct_cols if c in df.columns})

    def _row_style(row):
        return [_pct_cell_style(row[col], col) if col in pct_cols else "" for col in row.index]

    return df.style.format(fmt, na_rep="-").apply(_row_style, axis=1)


def _render_kpi_cards(totals: dict, pct_specs: list[tuple[str, str]]) -> None:
    cards = [("Total Volume", f"{totals.get('Volume', 0):,}", "Shipments in range", "#4a90d9")]
    for label in _kpi_metric_labels(pct_specs):
        val = totals.get(label, 0)
        if label == "Conversion %":
            border = "#28a745"
        elif label == "Cancelled After D2 %":
            border = "#dc3545"
        elif "Failed" in label or "Not Attempted" in label or label == "Pending %":
            border = "#dc3545"
        else:
            border = "#f0ad4e"
        cards.append((label, f"{val:.2f}%", "Across selected range", border))

    html = '<div class="kpi-row">'
    for title, val, sub, color in cards:
        html += (
            f'<div class="kpi-card" style="border-top-color:{color}">'
            f'<h4>{title}</h4><div class="val">{val}</div><p class="sub">{sub}</p></div>'
        )
    html += "</div>"
    st.markdown(html, unsafe_allow_html=True)


def _date_range_inputs(df: pd.DataFrame, key_prefix: str) -> Optional[tuple[object, object]]:
    valid = df["RequestDate"].dropna()
    if valid.empty:
        st.warning("No valid dates in the CSV.")
        return None
    min_d, max_d = valid.min(), valid.max()
    c1, c2 = st.columns(2)
    with c1:
        start = st.date_input(
            "From", value=min_d, min_value=min_d, max_value=max_d,
            format="YYYY-MM-DD", key=f"{key_prefix}_from",
        )
    with c2:
        end = st.date_input(
            "To", value=max_d, min_value=min_d, max_value=max_d,
            format="YYYY-MM-DD", key=f"{key_prefix}_to",
        )
    if start > end:
        st.warning("From date must be on or before To date.")
        return None
    return start, end


def _metric_config(metric_family: str, df: pd.DataFrame) -> tuple[list[str], list[tuple[str, str]]]:
    if metric_family == "pickup":
        sum_cols = pickup_sum_cols(df)
        pct_specs = _pickup_pct_metrics(df)
    else:
        sum_cols = attempt_sum_cols(df)
        pct_specs = _attempt_pct_metrics(df)
    return sum_cols, pct_specs


def render_seller_summary_page(metric_family: str) -> None:
    _inject_dashboard_css()
    is_pickup = metric_family == "pickup"
    title = "Seller-wise Pickup Performance" if is_pickup else "Seller-wise Attempt Performance"
    st.title(title)
    st.caption("Summarised seller view for the selected date range.")

    df, source = _load_simple_table_data()
    if df is None:
        st.info("Place an aggregated CSV in the `data/` folder to get started.")
        return

    key = metric_family
    bounds = _date_range_inputs(df, f"summary_{key}")
    if bounds is None:
        return
    start, end = bounds
    view = df[(df["RequestDate"] >= start) & (df["RequestDate"] <= end)]
    if view.empty:
        st.warning("No data in the selected date range.")
        return

    sum_cols, pct_specs = _metric_config(metric_family, df)
    summary = _build_seller_summary(view, sum_cols, pct_specs)
    if summary.empty:
        st.warning("No rows to display.")
        return

    st.markdown(
        f"Showing **{len(summary)}** sellers · Date range: "
        f"**{format_date(start)}** to **{format_date(end)}** · `{source}`"
    )

    vol = int(view["total_shipments"].sum())
    totals: dict = {"Volume": vol}
    for label, col in pct_specs:
        num = _view_metric_numerator(view, label, col)
        totals[label] = round(num / vol * 100, 2) if vol else 0.0
    _render_kpi_cards(totals, pct_specs)

    st.markdown(
        '<div class="legend-box">'
        '<span style="background:#d4edda;padding:2px 8px;border-radius:3px">Green = good</span> '
        '<span style="background:#fff3cd;padding:2px 8px;border-radius:3px">Yellow = caution</span> '
        '<span style="background:#f8d7da;padding:2px 8px;border-radius:3px">Red = alert</span>'
        "</div>",
        unsafe_allow_html=True,
    )

    search = st.text_input("Search seller type...", key=f"summary_search_{key}")
    display = summary
    if search.strip():
        display = summary[summary["Seller"].str.contains(search.strip(), case=False, na=False)]

    pct_cols = [label for label, _ in pct_specs]
    st.dataframe(_style_report_table(display, pct_cols), use_container_width=True, hide_index=True)


def render_period_detail_page(metric_family: str) -> None:
    _inject_dashboard_css()
    is_pickup = metric_family == "pickup"
    title = (
        "Day-wise Pickup Performance"
        if is_pickup
        else "Day-wise Attempt Performance"
    )
    st.title(title)
    st.caption(
        "Select a date range and one or more sellers. "
        "Weekly and monthly labels include the actual date span when the period is partial."
    )

    df, source = _load_simple_table_data()
    if df is None:
        st.info("Place an aggregated CSV in the `data/` folder to get started.")
        return

    key = metric_family
    valid = df["RequestDate"].dropna()
    min_d, max_d = valid.min(), valid.max()
    saved_range = st.session_state.get(f"detail_range_{key}", (min_d, max_d))

    c1, c2, c3 = st.columns([1, 1, 1.5])
    with c1:
        start = st.date_input(
            "From", value=saved_range[0], min_value=min_d, max_value=max_d,
            format="YYYY-MM-DD", key=f"detail_from_{key}",
        )
    with c2:
        end = st.date_input(
            "To", value=saved_range[1], min_value=min_d, max_value=max_d,
            format="YYYY-MM-DD", key=f"detail_to_{key}",
        )
    all_sellers = sorted(df["seller_type"].dropna().astype(str).unique().tolist())
    with c3:
        sellers = st.multiselect(
            "Select sellers",
            options=all_sellers,
            default=st.session_state.get(f"detail_sellers_{key}", []),
            placeholder="Choose one or more sellers...",
            key=f"detail_sellers_pick_{key}",
        )

    granularity = st.radio(
        "View by", ("Daily", "Weekly", "Monthly"),
        horizontal=True, key=f"detail_granularity_{key}",
    )

    if start > end:
        st.warning("From date must be on or before To date.")
        return
    if not sellers:
        st.info("Select at least one seller above to load the report.")
        return

    view = df[
        (df["RequestDate"] >= start)
        & (df["RequestDate"] <= end)
        & (df["seller_type"].isin(sellers))
    ]
    if view.empty:
        st.warning("No rows match the selected filters.")
        return

    sum_cols, pct_specs = _metric_config(metric_family, df)
    detail = _build_period_detail(view, start, end, sum_cols, pct_specs, granularity)
    pct_cols = [label for label, _ in pct_specs]
    st.caption(
        f"{len(detail)} rows · {granularity.lower()} · "
        f"{format_date(start)} to {format_date(end)} · `{source}`"
    )
    st.dataframe(_style_report_table(detail, pct_cols), use_container_width=True, hide_index=True)


def _available_weeks(df: pd.DataFrame) -> list[tuple[object, str]]:
    starts = (
        df["WeekStart"].dropna().drop_duplicates().sort_values().tolist()
    )
    return [(ws, _week_label(ws)) for ws in starts]


def _available_months(df: pd.DataFrame) -> list[tuple[object, str]]:
    starts = (
        df["MonthStart"].dropna().drop_duplicates().sort_values().tolist()
    )
    return [(ms, _month_label(ms)) for ms in starts]


def _render_simple_filters(
    df: pd.DataFrame, key_prefix: str,
) -> Optional[tuple[pd.DataFrame, object, object, str]]:
    """Period + seller_type filters. Returns (view, start, end, seller) or None."""
    valid_dates = df["RequestDate"].dropna()
    if valid_dates.empty:
        st.warning("No valid dates in the CSV.")
        return None

    min_date, max_date = valid_dates.min(), valid_dates.max()
    sellers = sorted(df["seller_type"].dropna().astype(str).unique().tolist())

    period = st.radio(
        "Period",
        ("Day", "Week", "Month"),
        horizontal=True,
        key=f"{key_prefix}_period",
    )

    filter_col, seller_col = st.columns(2)
    start, end = None, None

    with filter_col:
        if period == "Day":
            date_range = st.date_input(
                "Date range",
                value=(min_date, max_date),
                min_value=min_date,
                max_value=max_date,
                format="YYYY-MM-DD",
                key=f"{key_prefix}_date_range",
            )
            if not (isinstance(date_range, tuple) and len(date_range) == 2):
                st.warning("Select a start and end date.")
                return None
            start, end = date_range
            view = df[(df["RequestDate"] >= start) & (df["RequestDate"] <= end)]
        elif period == "Week":
            weeks = _available_weeks(df)
            if not weeks:
                st.warning("No weekly periods in the data.")
                return None
            labels = [label for _, label in weeks]
            pick = st.selectbox("Week", labels, key=f"{key_prefix}_week")
            week_start = next(ws for ws, lb in weeks if lb == pick)
            start = week_start
            end = week_start + timedelta(days=6)
            view = df[df["WeekStart"] == week_start]
        else:  # Month
            months = _available_months(df)
            if not months:
                st.warning("No monthly periods in the data.")
                return None
            labels = [label for _, label in months]
            pick = st.selectbox("Month", labels, key=f"{key_prefix}_month")
            month_start = next(ms for ms, lb in months if lb == pick)
            view = df[df["MonthStart"] == month_start]
            start = month_start
            end = view["RequestDate"].max() if not view.empty else month_start

    with seller_col:
        seller = st.selectbox(
            "seller_type",
            ["All"] + sellers,
            key=f"{key_prefix}_seller",
        )

    if seller != "All":
        view = view[view["seller_type"] == seller]
    if view.empty:
        st.warning("No rows match the selected filters.")
        return None
    return view, start, end, seller


def _render_summary_table(
    view: pd.DataFrame,
    start,
    end,
    seller: str,
    source: str,
    sum_cols: list[str],
    key_prefix: str,
    conversion_col: Optional[str] = None,
) -> None:
    display_mode = st.radio(
        "Display values as",
        ("Counts", "% of row"),
        horizontal=True,
        key=f"{key_prefix}_display_mode",
    )
    as_percent = display_mode == "% of row"
    display = summarize_metrics(
        view, start, end, sum_cols,
        include_grand_total=(seller == "All"),
        as_percent=as_percent,
        conversion_col=conversion_col if as_percent else None,
    )
    st.caption(
        f"Summarised over {format_date(start)}–{format_date(end)} · "
        f"{len(view):,} daily rows rolled up · source: `{source}`"
    )
    if as_percent:
        fmt = {
            c: "{:.2f}%" if c == "Conversion %" else "{:.1f}%"
            for c in display.columns
            if c not in ("date_range", "seller_type", "total_shipments")
        }
        fmt["total_shipments"] = "{:,}"
        styled = display.style.format(fmt, na_rep="-")
    else:
        styled = display.style.format(
            {c: "{:,}" for c in display.columns if c not in ("date_range", "seller_type")},
            na_rep="-",
        )
    st.dataframe(styled, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="RVP Pickup Dashboard", layout="wide")
    pages = [
        st.Page(render_pickup_page, title="Pickup Summary", default=True),
        st.Page(render_pickup_period_page, title="Pickup Period Detail"),
        st.Page(render_attempt_page, title="Attempt Summary"),
        st.Page(render_attempt_period_page, title="Attempt Period Detail"),
    ]
    try:
        nav = st.navigation(pages, position="top")
    except TypeError:
        nav = st.navigation(pages)
    nav.run()


if __name__ == "__main__":
    main()
