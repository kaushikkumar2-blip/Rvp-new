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
from typing import Optional, Tuple

import pandas as pd
import streamlit as st

import github_store as ghs

DEFAULT_SHEET = "Externalization_RVP_report_ship"
ATTEMPT_ORDER = ["D0", "D1", "D2", "D3", "D4+", "Not Attempted"]
OUTCOME_ORDER = [
    "D0", "D1", "D2", "D3", "D4+",
    "Pending", "QC failed", "Not Attempted",
]
QC_REASONS = {"PRODUCT_DAMAGED", "PRODUCT_MISMATCH", "PRODUCT_MISMATCHED"}
DATE_FMT = "%d-%m-%Y"

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
        return f"{value.strftime(DATE_FMT)} to {end.strftime(DATE_FMT)}"
    return str(value)


def _month_label(value) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%b %Y")
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
# Sidebar: source picker (Upload / GitHub snapshot)
# ---------------------------------------------------------------------------

def _render_upload_source() -> Optional[pd.DataFrame]:
    uploaded = st.file_uploader(
        "Upload raw file (.xlsx, .xls, or .csv)",
        type=["xlsx", "xls", "csv"], key="uploader",
    )
    if uploaded is not None:
        st.session_state["_file_bytes"] = uploaded.getvalue()
        st.session_state["_file_name"] = uploaded.name

    content = st.session_state.get("_file_bytes")
    filename = st.session_state.get("_file_name")
    if content is None or filename is None:
        st.info("Upload a file to get started.")
        return None

    info_col, clear_col = st.columns([6, 1])
    info_col.caption(f"Using `{filename}` ({len(content) / 1024:.0f} KB)")
    if clear_col.button("Clear", help="Remove the uploaded file"):
        st.session_state.pop("_file_bytes", None)
        st.session_state.pop("_file_name", None)
        st.rerun()

    sheet_name = None
    if not is_csv(filename):
        try:
            sheets = list_sheets(content)
        except Exception as exc:
            st.error(f"Could not read workbook: {exc}")
            return None
        default_idx = sheets.index(DEFAULT_SHEET) if DEFAULT_SHEET in sheets else 0
        sheet_name = st.selectbox("Sheet", sheets, index=default_idx, key="sheet")

    try:
        df_full = parse_and_enrich(content, filename, sheet_name)
    except ValueError as exc:
        st.error(str(exc))
        return None
    except Exception as exc:
        st.error(f"Could not parse file: {exc}")
        return None

    return _apply_filters(df_full)


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
            format="DD-MM-YYYY", key="date_range",
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

def _source_picker() -> tuple[str, Optional[pd.DataFrame], Optional[dict]]:
    """Top-of-page source selector. Returns (mode, df, snapshot_tables)."""
    with st.expander("Data & Filters", expanded=True):
        modes = ["Upload new file"]
        if ghs.is_configured():
            modes.append("Load from GitHub")
        mode = st.radio("Source", modes, horizontal=True, key="source_mode")

        if mode == "Upload new file":
            df = _render_upload_source()
            if df is not None:
                _render_github_save_section(df)
            return mode, df, None
        else:
            tables = _render_github_load_source()
            return mode, None, tables


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


def render_pickup_page() -> None:
    st.title("Pickup Performance")
    st.caption(
        "D0..D4+ are **cumulative**: D1 includes D0, D2 includes D0-D1, etc. "
        "D4+ equals the total converted shipments per row. "
        "Pending = attempted but not yet picked up and not QC-failed."
    )

    mode, df, snap = _source_picker()

    if mode == "Load from GitHub":
        if snap is None:
            return
        _render_loaded_snapshot(snap, page="pickup")
        return

    if df is None:
        return

    sig = _df_signature(df)

    ctrl_col1, ctrl_col2 = st.columns([1, 1])
    with ctrl_col1:
        granularity = st.radio(
            "Granularity", tuple(GRANULARITY_CONFIG.keys()),
            horizontal=True, key="pickup_granularity",
        )
    with ctrl_col2:
        display_mode = st.radio(
            "Display values as", ("Counts", "% of row"),
            horizontal=True, key="pickup_display_mode",
        )
    as_percent = display_mode == "% of row"
    period_col, label_fn, index_label = GRANULARITY_CONFIG[granularity]

    st.subheader(f"{granularity}-wise pickup outcome (cumulative D-buckets)")
    day_outcome = cached_pivot(df, sig, period_col, "PickupOutcome", tuple(OUTCOME_ORDER))
    day_outcome = cumulate_d_buckets(day_outcome)
    day_outcome.index = day_outcome.index.map(label_fn)
    day_outcome.index.name = index_label
    display_pivot(day_outcome, as_percent)

    st.subheader("Seller-type-wise pickup outcome (cumulative D-buckets)")
    seller_outcome = cached_pivot(df, sig, "seller_type", "PickupOutcome", tuple(OUTCOME_ORDER))
    seller_outcome = cumulate_d_buckets(seller_outcome)
    seller_outcome.index.name = "seller_type"
    display_pivot(seller_outcome, as_percent)

    st.subheader("Return-pickup conversion by seller type")
    conv = cached_conversion(df, sig)
    conv.index.name = "seller_type"
    st.dataframe(conv, use_container_width=True)

    st.download_button(
        "Download pickup tables as Excel (counts)",
        data=build_pickup_workbook(day_outcome, seller_outcome, conv, granularity),
        file_name=f"rvp_pickup_performance_{granularity.lower()}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def render_attempt_page() -> None:
    st.title("Attempt Performance")
    st.caption(
        "D0..D4+ are **cumulative**: D1 includes D0, D2 includes D0-D1, etc. "
        "D4+ equals the total attempted shipments per row. "
        "QC-failed shipments are excluded here; see the Pickup Performance page."
    )

    mode, df, snap = _source_picker()

    if mode == "Load from GitHub":
        if snap is None:
            return
        _render_loaded_snapshot(snap, page="attempt")
        return

    if df is None:
        return

    sig = _df_signature(df)

    ctrl_col1, ctrl_col2 = st.columns([1, 1])
    with ctrl_col1:
        granularity = st.radio(
            "Granularity", tuple(GRANULARITY_CONFIG.keys()),
            horizontal=True, key="attempt_granularity",
        )
    with ctrl_col2:
        display_mode = st.radio(
            "Display values as", ("Counts", "% of row"),
            horizontal=True, key="attempt_display_mode",
        )
    as_percent = display_mode == "% of row"
    period_col, label_fn, index_label = GRANULARITY_CONFIG[granularity]

    st.subheader(f"{granularity}-wise attempt pivot (cumulative D-buckets)")
    day_pivot = cached_pivot(df, sig, period_col, "AttemptBucket", tuple(ATTEMPT_ORDER))
    day_pivot = cumulate_d_buckets(day_pivot)
    day_pivot.index = day_pivot.index.map(label_fn)
    day_pivot.index.name = index_label
    display_pivot(day_pivot, as_percent)

    st.subheader("Seller-type-wise attempt pivot (cumulative D-buckets)")
    seller_pivot = cached_pivot(df, sig, "seller_type", "AttemptBucket", tuple(ATTEMPT_ORDER))
    seller_pivot = cumulate_d_buckets(seller_pivot)
    seller_pivot.index.name = "seller_type"
    display_pivot(seller_pivot, as_percent)

    st.download_button(
        "Download attempt tables as Excel (counts)",
        data=build_attempt_workbook(day_pivot, seller_pivot, granularity),
        file_name=f"rvp_attempt_performance_{granularity.lower()}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


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

    mode, df, _snap = _source_picker()
    if mode == "Load from GitHub":
        st.info(
            "Pincode Performance needs raw shipment rows. Switch the source "
            "above to **Upload new file** to use this page."
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
            format="DD-MM-YYYY", key="pin_date_range",
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


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="RVP Pickup Dashboard", layout="wide")
    pages = [
        st.Page(render_pickup_page, title="Pickup Performance", default=True),
        st.Page(render_attempt_page, title="Attempt Performance"),
        st.Page(render_pincode_performance_page, title="Pincode Performance"),
        st.Page(render_trends_page, title="Trends"),
    ]
    try:
        nav = st.navigation(pages, position="top")
    except TypeError:
        nav = st.navigation(pages)
    nav.run()


if __name__ == "__main__":
    main()
