# RVP Pickup Dashboard

A Streamlit app that turns a raw RVP shipment export into operational pivots and a return-pickup conversion table. Supports saving percentage snapshots to GitHub so you can browse history without re-uploading large CSVs.

Upload supports `.xlsx`, `.xls`, and `.csv`.

## Pages

- **Pickup Performance** — summarised pickup outcome buckets + conversion % for the selected date range and seller.
- **Attempt Performance** — summarised attempt buckets for the same filters.

Both pages share **Date range** and **seller_type** (`All` or one seller) filters at the top.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

### Default dataset

On startup the dashboard loads any `.csv` from `data/` (prefers `data/default.csv` if present). Your file `3e77de04ce5192e2ef5930758dec56ba.csv` is a **pre-aggregated daily export** (`seller_type`, `day`, `pickup_*`, `attempt_*` columns) — Pickup and Attempt pages work out of the box. Pincode Performance still needs a raw shipment export with `src_pincode`.

Uploading a file replaces the default for that session; **Clear** switches back.

Override the path in `.streamlit/secrets.toml`:

```toml
[default_data]
path = "path/to/your/export.csv"
```

## Data sources (top of each page)

- **Upload new file** — same as before, plus a "💾 Save snapshot to GitHub" expander after parse.
- **Load from GitHub** — pick a previously-saved snapshot. Loads in milliseconds (small Parquet files).

If GitHub isn't configured, only the upload source is shown.

## GitHub snapshot storage

### Why

Re-uploading a 100–150 MB CSV every time you want to look at last month's numbers is slow. Instead, after computing the pivots once, the app uploads them as small Parquet files (typically <100 KB total per snapshot) to a private GitHub repo. Loading a snapshot is then near-instant and the **Trends** page can stitch any number of snapshots together.

### What gets stored

Per snapshot, seven Parquet files under `snapshots/<snapshot-id>/`:

| File | Contents |
|---|---|
| `daily_attempt_pct.parquet` | Day × `D0..D4+, Not Attempted` (row %) |
| `weekly_attempt_pct.parquet` | Week × same |
| `monthly_attempt_pct.parquet` | Month × same |
| `daily_outcome_pct.parquet` | Day × `D0..D4+, Pending, QC failed, Not Attempted` (row %) |
| `weekly_outcome_pct.parquet` | Week × same |
| `monthly_outcome_pct.parquet` | Month × same |
| `conversion_by_seller.parquet` | Conversion % by `seller_type` |

Plus a `meta.json` per snapshot and a single `snapshots/index.json` manifest at the root.

Stored values are **percentages only** (raw counts aren't kept in the snapshot; re-upload the raw file if you need counts back).

### Setup

1. Create a GitHub repo for snapshots. A private repo is fine and recommended.
2. Create a personal access token with `repo` scope (classic) or `contents: read+write` on a fine-grained token scoped to that repo.
3. Copy `secrets.toml.example` to `.streamlit/secrets.toml` and fill in `token`, `repo`, `branch`:

```toml
[github]
token = "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
repo  = "your-username/rvp-snapshots"
branch = "main"
```

4. For **Streamlit Cloud**: add the same `[github]` block under *App settings → Secrets*. Do not commit `secrets.toml`.

### Overwrite vs append

If you save a snapshot whose date range exactly matches an existing one, the app asks: **Confirm overwrite** (replace the older one), **Save as new** (keep both), or **Cancel**. Non-matching ranges always save as new.

To delete a snapshot, open *Load from GitHub*, pick it, expand *Danger zone*, and click *Delete*.

## What the app computes

Two row-level labels derived from each shipment:

**Attempt bucket** (excludes QC-failed):

1. `First_pickup_date` blank → **Not Attempted**
2. else → `(First_pickup_date - Request_created_date).days`, bucketed `D0..D4+`.

**Pickup outcome** (covers every shipment exactly once, priorities top-down):

1. `rvp_pickup_completed_date` filled → **Converted** (then split into `D0..D4+` by `(rvp_pickup_completed_date - Request_created_date).days`)
2. else `First_pickup_date` blank → **Not Attempted**
3. else `shipment_status_reason` in `PRODUCT_DAMAGED / PRODUCT_MISMATCH(ED)` → **QC failed**
4. else → **Pending**

`D0..D4+` columns are rendered **cumulatively** in all tables (D1 = D0+D1, etc., so D4+ = total).

## Performance notes

For large CSVs (100+ MB) the app:

- Reads **only the 6 required columns** via `usecols`.
- Uses **explicit dtypes** (`category` for low-cardinality columns) — ~50% memory cut.
- Caches the parsed+enriched dataframe with `@st.cache_data` so toggling filters doesn't reparse.
- Caches each pivot keyed on a digest of the filtered dataframe, so changing granularity or display mode is instant after the first render.
- Snapshot loads are cached for 5 minutes; force-refresh by clearing the snapshot picker.

`config.toml` raises Streamlit's upload cap to ~5 GB.
