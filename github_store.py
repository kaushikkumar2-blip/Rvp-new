"""GitHub-backed snapshot storage for the RVP dashboard.

Each snapshot is a folder under `snapshots/` in the target repo, containing
seven Parquet files (one per pivot table) plus a `meta.json` describing the
date range, row count, seller types, and upload timestamp.

A single `snapshots/index.json` manifest lists every snapshot, so the UI can
populate the picker with one API call instead of N.

Reads use the raw.githubusercontent.com CDN (no auth, fast). Writes use the
Contents API with a PAT from `st.secrets`.
"""

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests
import streamlit as st

API_ROOT = "https://api.github.com"
RAW_ROOT = "https://raw.githubusercontent.com"
SNAPSHOT_ROOT = "snapshots"
INDEX_PATH = f"{SNAPSHOT_ROOT}/index.json"

# Separate area for saved BIC pincode lists (independent of snapshots).
BIC_ROOT = "bic_lists"
BIC_INDEX_PATH = f"{BIC_ROOT}/index.json"

# The seven tables that make up a snapshot.
TABLES = (
    "daily_attempt_pct",
    "weekly_attempt_pct",
    "monthly_attempt_pct",
    "daily_outcome_pct",
    "weekly_outcome_pct",
    "monthly_outcome_pct",
    "conversion_by_seller",
)


@dataclass
class SnapshotMeta:
    snapshot_id: str          # folder name, e.g. "2026-05-22T093412Z__may"
    label: str                # human label, e.g. "May 2026 weekly upload"
    date_min: str             # earliest RequestDate, ISO YYYY-MM-DD
    date_max: str             # latest RequestDate
    row_count: int
    uploaded_at: str          # ISO 8601 UTC
    seller_types: list[str]

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "label": self.label,
            "date_min": self.date_min,
            "date_max": self.date_max,
            "row_count": self.row_count,
            "uploaded_at": self.uploaded_at,
            "seller_types": self.seller_types,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SnapshotMeta":
        return cls(
            snapshot_id=d["snapshot_id"],
            label=d.get("label", d["snapshot_id"]),
            date_min=d["date_min"],
            date_max=d["date_max"],
            row_count=int(d["row_count"]),
            uploaded_at=d["uploaded_at"],
            seller_types=list(d.get("seller_types", [])),
        )


# ---------------------------------------------------------------------------
# Config + auth
# ---------------------------------------------------------------------------

class GitHubConfigError(RuntimeError):
    pass


def _config() -> dict:
    """Pull GitHub config from st.secrets. Raises if anything is missing."""
    try:
        gh = st.secrets["github"]
    except (KeyError, FileNotFoundError):
        raise GitHubConfigError(
            "GitHub storage is not configured. Add a [github] block to "
            ".streamlit/secrets.toml with token, repo, and branch."
        )
    missing = [k for k in ("token", "repo", "branch") if k not in gh]
    if missing:
        raise GitHubConfigError(
            "Missing in [github] secrets: " + ", ".join(missing)
        )
    return {
        "token": gh["token"],
        "repo": gh["repo"],          # e.g. "alice/rvp-snapshots"
        "branch": gh["branch"],      # e.g. "main"
    }


def is_configured() -> bool:
    try:
        _config()
        return True
    except GitHubConfigError:
        return False


def _headers(cfg: dict) -> dict:
    return {
        "Authorization": f"Bearer {cfg['token']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


# ---------------------------------------------------------------------------
# Low-level file ops (Contents API)
# ---------------------------------------------------------------------------

def _contents_url(cfg: dict, path: str) -> str:
    return f"{API_ROOT}/repos/{cfg['repo']}/contents/{path}"


def _raw_url(cfg: dict, path: str) -> str:
    return f"{RAW_ROOT}/{cfg['repo']}/{cfg['branch']}/{path}"


def _get_sha(cfg: dict, path: str) -> Optional[str]:
    """Return the SHA of the file if it exists, else None."""
    r = requests.get(
        _contents_url(cfg, path),
        headers=_headers(cfg),
        params={"ref": cfg["branch"]},
        timeout=30,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json().get("sha")


def _put_file(cfg: dict, path: str, content: bytes, message: str) -> None:
    """Create or update a file via the Contents API."""
    sha = _get_sha(cfg, path)
    payload = {
        "message": message,
        "content": base64.b64encode(content).decode("ascii"),
        "branch": cfg["branch"],
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(
        _contents_url(cfg, path),
        headers=_headers(cfg),
        json=payload,
        timeout=60,
    )
    r.raise_for_status()


def _delete_file(cfg: dict, path: str, message: str) -> None:
    sha = _get_sha(cfg, path)
    if sha is None:
        return
    r = requests.delete(
        _contents_url(cfg, path),
        headers=_headers(cfg),
        json={"message": message, "sha": sha, "branch": cfg["branch"]},
        timeout=30,
    )
    r.raise_for_status()


def _fetch_raw(cfg: dict, path: str) -> Optional[bytes]:
    """GET a file from raw.githubusercontent.com. Returns None on 404."""
    headers = {"Authorization": f"Bearer {cfg['token']}"}  # private repo support
    r = requests.get(_raw_url(cfg, path), headers=headers, timeout=60)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.content


# ---------------------------------------------------------------------------
# Manifest (index.json)
# ---------------------------------------------------------------------------

def _read_index(cfg: dict) -> list[dict]:
    raw = _fetch_raw(cfg, INDEX_PATH)
    if raw is None:
        return []
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return []


def _write_index(cfg: dict, entries: list[dict], message: str) -> None:
    body = json.dumps(entries, indent=2, sort_keys=True).encode("utf-8")
    _put_file(cfg, INDEX_PATH, body, message)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def make_snapshot_id(label: str) -> str:
    """Build a sortable, filesystem-safe snapshot id from a label."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in label.lower()).strip("-")
    slug = slug[:40] or "snapshot"
    return f"{ts}__{slug}"


def _df_to_parquet(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    # Reset index so labels (which may be strings like "01-05-2026") survive,
    # and remember the original index name so the reader can restore it.
    out = df.reset_index()
    idx_name = df.index.name or "index"
    # First column after reset_index is the old index — rename for clarity.
    if out.columns[0] != idx_name:
        out = out.rename(columns={out.columns[0]: idx_name})
    # Stash the index-column name in pandas/arrow metadata via attrs is not
    # round-tripped, so we use a sentinel column name prefix instead.
    out.attrs["__index_col__"] = idx_name
    out.to_parquet(buf, engine="pyarrow", compression="zstd", index=False)
    return buf.getvalue()


# Known index column names per table — used on read to restore the index.
_INDEX_COLS = {
    "daily_attempt_pct": "Request_created_date",
    "daily_outcome_pct": "Request_created_date",
    "weekly_attempt_pct": "Week",
    "weekly_outcome_pct": "Week",
    "monthly_attempt_pct": "Month",
    "monthly_outcome_pct": "Month",
    "conversion_by_seller": "seller_type",
}


def _parquet_to_df(content: bytes, index_col: Optional[str] = None) -> pd.DataFrame:
    df = pd.read_parquet(io.BytesIO(content), engine="pyarrow")
    if index_col and index_col in df.columns:
        df = df.set_index(index_col)
    return df


def list_snapshots() -> list[SnapshotMeta]:
    """Return all snapshots, newest first."""
    cfg = _config()
    entries = _read_index(cfg)
    metas = [SnapshotMeta.from_dict(e) for e in entries]
    metas.sort(key=lambda m: m.uploaded_at, reverse=True)
    return metas


def snapshot_exists_for_range(date_min: str, date_max: str) -> Optional[SnapshotMeta]:
    """Return the most recent snapshot with the exact same date range, or None."""
    for m in list_snapshots():
        if m.date_min == date_min and m.date_max == date_max:
            return m
    return None


def save_snapshot(
    *,
    label: str,
    date_min: str,
    date_max: str,
    row_count: int,
    seller_types: list[str],
    tables: dict[str, pd.DataFrame],
    overwrite_id: Optional[str] = None,
) -> SnapshotMeta:
    """Upload a snapshot folder + update the manifest.

    `tables` keys must be a subset of TABLES. Missing tables are skipped
    (the trends view will just have gaps for them).

    If `overwrite_id` is given, files are written into that existing folder
    and the manifest entry is replaced; otherwise a new id is minted.
    """
    cfg = _config()
    snapshot_id = overwrite_id or make_snapshot_id(label)
    folder = f"{SNAPSHOT_ROOT}/{snapshot_id}"

    for name in TABLES:
        if name in tables:
            content = _df_to_parquet(tables[name])
            _put_file(cfg, f"{folder}/{name}.parquet", content,
                      f"snapshot {snapshot_id}: {name}")

    meta = SnapshotMeta(
        snapshot_id=snapshot_id,
        label=label,
        date_min=date_min,
        date_max=date_max,
        row_count=row_count,
        uploaded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        seller_types=sorted(seller_types),
    )
    _put_file(cfg, f"{folder}/meta.json",
              json.dumps(meta.to_dict(), indent=2).encode("utf-8"),
              f"snapshot {snapshot_id}: meta")

    # Update manifest (replace if id already present).
    entries = _read_index(cfg)
    entries = [e for e in entries if e.get("snapshot_id") != snapshot_id]
    entries.append(meta.to_dict())
    _write_index(cfg, entries, f"index: add/update {snapshot_id}")
    return meta


def delete_snapshot(snapshot_id: str) -> None:
    cfg = _config()
    folder = f"{SNAPSHOT_ROOT}/{snapshot_id}"
    for name in TABLES:
        try:
            _delete_file(cfg, f"{folder}/{name}.parquet", f"delete {snapshot_id}")
        except requests.HTTPError:
            pass
    try:
        _delete_file(cfg, f"{folder}/meta.json", f"delete {snapshot_id}")
    except requests.HTTPError:
        pass
    entries = [e for e in _read_index(cfg) if e.get("snapshot_id") != snapshot_id]
    _write_index(cfg, entries, f"index: remove {snapshot_id}")


@st.cache_data(show_spinner=False, ttl=300)
def _load_table_cached(repo: str, branch: str, snapshot_id: str, name: str,
                       token: str) -> Optional[bytes]:
    """Cached raw fetch. Keyed on repo/branch/snapshot/name so it survives reruns."""
    url = f"{RAW_ROOT}/{repo}/{branch}/{SNAPSHOT_ROOT}/{snapshot_id}/{name}.parquet"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.content


def load_snapshot(snapshot_id: str) -> dict[str, pd.DataFrame]:
    """Return a dict of table_name -> DataFrame. Missing tables are skipped."""
    cfg = _config()
    out: dict[str, pd.DataFrame] = {}
    for name in TABLES:
        raw = _load_table_cached(cfg["repo"], cfg["branch"], snapshot_id, name, cfg["token"])
        if raw is None:
            continue
        out[name] = _parquet_to_df(raw, index_col=_INDEX_COLS.get(name))
    return out


def clear_caches() -> None:
    """Invalidate the per-table fetch cache (call after save/delete)."""
    _load_table_cached.clear()


# ---------------------------------------------------------------------------
# BIC pincode lists (separate namespace from snapshots)
# ---------------------------------------------------------------------------

@dataclass
class BicListMeta:
    bic_id: str                # folder name, e.g. "2026-06-08T093412Z__bic-default"
    label: str                 # human label
    pincode_count: int
    uploaded_at: str           # ISO 8601 UTC
    source_filename: str       # original CSV filename, for traceability

    def to_dict(self) -> dict:
        return {
            "bic_id": self.bic_id,
            "label": self.label,
            "pincode_count": self.pincode_count,
            "uploaded_at": self.uploaded_at,
            "source_filename": self.source_filename,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BicListMeta":
        return cls(
            bic_id=d["bic_id"],
            label=d.get("label", d["bic_id"]),
            pincode_count=int(d.get("pincode_count", 0)),
            uploaded_at=d["uploaded_at"],
            source_filename=d.get("source_filename", ""),
        )


def make_bic_id(label: str) -> str:
    """Sortable, filesystem-safe id for a BIC list."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in label.lower()).strip("-")
    slug = slug[:40] or "bic"
    return f"{ts}__{slug}"


def _read_bic_index(cfg: dict) -> list[dict]:
    raw = _fetch_raw(cfg, BIC_INDEX_PATH)
    if raw is None:
        return []
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return []


def _write_bic_index(cfg: dict, entries: list[dict], message: str) -> None:
    body = json.dumps(entries, indent=2, sort_keys=True).encode("utf-8")
    _put_file(cfg, BIC_INDEX_PATH, body, message)


def list_bic_lists() -> list[BicListMeta]:
    """Return saved BIC lists, newest first."""
    cfg = _config()
    entries = _read_bic_index(cfg)
    metas = [BicListMeta.from_dict(e) for e in entries]
    metas.sort(key=lambda m: m.uploaded_at, reverse=True)
    return metas


def save_bic_list(
    *,
    label: str,
    pincodes: list[str],
    source_filename: str = "",
    overwrite_id: Optional[str] = None,
) -> BicListMeta:
    """Upload a BIC pincode list folder + update the BIC manifest.

    Pincodes are stored as a single-column Parquet file for compactness.
    """
    cfg = _config()
    bic_id = overwrite_id or make_bic_id(label)
    folder = f"{BIC_ROOT}/{bic_id}"

    cleaned = [str(p).strip() for p in pincodes if str(p).strip()]
    df = pd.DataFrame({"Pincode": cleaned})
    buf = io.BytesIO()
    df.to_parquet(buf, engine="pyarrow", compression="zstd", index=False)
    _put_file(cfg, f"{folder}/pincodes.parquet", buf.getvalue(),
              f"bic {bic_id}: pincodes")

    meta = BicListMeta(
        bic_id=bic_id,
        label=label,
        pincode_count=len(df),
        uploaded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source_filename=source_filename,
    )
    _put_file(cfg, f"{folder}/meta.json",
              json.dumps(meta.to_dict(), indent=2).encode("utf-8"),
              f"bic {bic_id}: meta")

    entries = _read_bic_index(cfg)
    entries = [e for e in entries if e.get("bic_id") != bic_id]
    entries.append(meta.to_dict())
    _write_bic_index(cfg, entries, f"bic index: add/update {bic_id}")
    return meta


@st.cache_data(show_spinner=False, ttl=300)
def _load_bic_cached(repo: str, branch: str, bic_id: str,
                     token: str) -> Optional[bytes]:
    url = f"{RAW_ROOT}/{repo}/{branch}/{BIC_ROOT}/{bic_id}/pincodes.parquet"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.content


def load_bic_list(bic_id: str) -> frozenset[str]:
    """Return the set of pincodes for the given BIC list id."""
    cfg = _config()
    raw = _load_bic_cached(cfg["repo"], cfg["branch"], bic_id, cfg["token"])
    if raw is None:
        return frozenset()
    df = pd.read_parquet(io.BytesIO(raw), engine="pyarrow")
    col = "Pincode" if "Pincode" in df.columns else df.columns[0]
    series = df[col].dropna().astype(str).str.strip()
    series = series[series != ""]
    return frozenset(series)


def delete_bic_list(bic_id: str) -> None:
    cfg = _config()
    folder = f"{BIC_ROOT}/{bic_id}"
    for fname in ("pincodes.parquet", "meta.json"):
        try:
            _delete_file(cfg, f"{folder}/{fname}", f"delete bic {bic_id}")
        except requests.HTTPError:
            pass
    entries = [e for e in _read_bic_index(cfg) if e.get("bic_id") != bic_id]
    _write_bic_index(cfg, entries, f"bic index: remove {bic_id}")


def bic_list_exists_for_label(label: str) -> Optional[BicListMeta]:
    """Return the most recent BIC list with this exact label, or None."""
    for m in list_bic_lists():
        if m.label == label:
            return m
    return None


def clear_bic_caches() -> None:
    _load_bic_cached.clear()
