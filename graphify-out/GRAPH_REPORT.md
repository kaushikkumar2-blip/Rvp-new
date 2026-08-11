# Graph Report - C:\Users\kaushik.kumar2\Desktop\Rvp-new-main  (2026-08-05)

## Corpus Check
- Corpus is ~7,293 words - fits in a single context window. You may not need a graph.

## Summary
- 128 nodes · 279 edges · 15 communities (14 shown, 1 thin omitted)
- Extraction: 96% EXTRACTED · 4% INFERRED · 0% AMBIGUOUS · INFERRED: 10 edges (avg confidence: 0.5)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Community 0
- Community 1
- Community 2
- Community 3
- Community 4
- Community 5
- Community 6
- Community 7
- Community 8
- Community 9
- Community 10
- Community 11
- Community 12
- Community 13
- Community 14

## God Nodes (most connected - your core abstractions)
1. `_config()` - 12 edges
2. `build_snapshot_tables()` - 11 edges
3. `save_snapshot()` - 11 edges
4. `render_pickup_page()` - 10 edges
5. `save_bic_list()` - 10 edges
6. `_source_picker()` - 9 edges
7. `render_attempt_page()` - 9 edges
8. `render_pincode_performance_page()` - 9 edges
9. `_put_file()` - 9 edges
10. `_render_upload_source()` - 8 edges

## Surprising Connections (you probably didn't know these)
- `main()` --indirect_call--> `render_trends_page()`  [INFERRED]
  app.py → app.py  _Bridges community 12 → community 3_
- `as_row_percent()` --references--> `DataFrame`  [EXTRACTED]
  app.py →   _Bridges community 0 → community 2_
- `cached_pivot()` --references--> `DataFrame`  [EXTRACTED]
  app.py →   _Bridges community 0 → community 3_
- `_concat_trend_table()` --references--> `DataFrame`  [EXTRACTED]
  app.py →   _Bridges community 0 → community 12_
- `_render_github_load_source()` --references--> `DataFrame`  [EXTRACTED]
  app.py →   _Bridges community 0 → community 11_

## Import Cycles
- None detected.

## Communities (15 total, 1 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.25
Nodes (14): _apply_filters(), compute_attempt(), compute_outcome(), is_csv(), list_sheets(), missing_cols(), parse_and_enrich(), _parse_rvp_completed() (+6 more)

### Community 1 - "Community 1"
Cohesion: 0.20
Nodes (12): bic_list_exists_for_label(), BicListMeta, delete_bic_list(), list_bic_lists(), make_bic_id(), Sortable, filesystem-safe id for a BIC list., Return saved BIC lists, newest first., Upload a BIC pincode list folder + update the BIC manifest.      Pincodes are st (+4 more)

### Community 2 - "Community 2"
Cohesion: 0.29
Nodes (11): as_row_percent(), build_snapshot_tables(), cached_conversion(), conversion_table(), _do_save(), format_date(), _month_label(), pivot_counts() (+3 more)

### Community 3 - "Community 3"
Cohesion: 0.29
Nodes (12): build_attempt_workbook(), build_pickup_workbook(), cached_pivot(), cumulate_d_buckets(), _df_signature(), display_pivot(), main(), Stable hash of a dataframe's identity (shape + a few key column hashes).      pd (+4 more)

### Community 4 - "Community 4"
Cohesion: 0.18
Nodes (11): _do_bic_save(), _get_bic_set(), load_bic_pincodes(), Load-from-GitHub picker. Returns (bic_set, label) or None., Resolve a BIC pincode set from either Upload or GitHub. Returns     `(bic_set, s, Parse a single-column pincode CSV into a normalized frozenset.      Tolerates a, Upload-and-save flow. Returns (bic_set, source_label) or None., Save-to-GitHub UI for the currently loaded BIC list. (+3 more)

### Community 5 - "Community 5"
Cohesion: 0.22
Nodes (11): _df_to_parquet(), load_snapshot(), _load_table_cached(), make_snapshot_id(), _parquet_to_df(), DataFrame, Build a sortable, filesystem-safe snapshot id from a label., Upload a snapshot folder + update the manifest.      `tables` keys must be a sub (+3 more)

### Community 6 - "Community 6"
Cohesion: 0.33
Nodes (7): delete_snapshot(), _fetch_raw(), GitHub-backed snapshot storage for the RVP dashboard.  Each snapshot is a folder, GET a file from raw.githubusercontent.com. Returns None on 404., _raw_url(), _read_index(), _write_index()

### Community 7 - "Community 7"
Cohesion: 0.25
Nodes (8): _config(), GitHubConfigError, is_configured(), _load_bic_cached(), load_bic_list(), Return the set of pincodes for the given BIC list id., Pull GitHub config from st.secrets. Raises if anything is missing., RuntimeError

### Community 8 - "Community 8"
Cohesion: 0.48
Nodes (7): _contents_url(), _delete_file(), _get_sha(), _headers(), _put_file(), Return the SHA of the file if it exists, else None., Create or update a file via the Contents API.

### Community 9 - "Community 9"
Cohesion: 0.38
Nodes (5): list_snapshots(), Return all snapshots, newest first., Return the most recent snapshot with the exact same date range, or None., snapshot_exists_for_range(), SnapshotMeta

### Community 10 - "Community 10"
Cohesion: 0.47
Nodes (6): _default_dataset(), Return the default shipment CSV path, if configured and present., Load bundled default CSV bytes. Cached by path mtime., _read_default_csv(), _resolve_default_csv_path(), Path

### Community 11 - "Community 11"
Cohesion: 0.33
Nodes (6): Save-to-GitHub UI shown under upload mode after a successful parse., Returns a dict of {table_name: % DataFrame} for the selected snapshot., Top-of-page source selector. Returns (mode, df, snapshot_tables)., _render_github_load_source(), _render_github_save_section(), _source_picker()

### Community 12 - "Community 12"
Cohesion: 0.40
Nodes (5): _concat_trend_table(), Pull `table_name` from every snapshot, concatenate, dedupe on the index.      Dr, Sort the index by the date it represents. Returns df unchanged on failure., render_trends_page(), _sort_index_chronologically()

### Community 13 - "Community 13"
Cohesion: 0.50
Nodes (4): Repair legacy snapshots: force the right index name, coerce to numeric., Display pre-computed % tables from a GitHub snapshot., _render_loaded_snapshot(), _repair_snapshot_table()

## Knowledge Gaps
- **1 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `save_snapshot()` connect `Community 5` to `Community 1`, `Community 6`, `Community 7`, `Community 8`, `Community 9`?**
  _High betweenness centrality (0.028) - this node is a cross-community bridge._
- **Why does `save_bic_list()` connect `Community 1` to `Community 8`, `Community 5`, `Community 6`, `Community 7`?**
  _High betweenness centrality (0.026) - this node is a cross-community bridge._
- **Why does `_config()` connect `Community 7` to `Community 1`, `Community 5`, `Community 6`, `Community 9`?**
  _High betweenness centrality (0.025) - this node is a cross-community bridge._
- **Are the 3 inferred relationships involving `build_snapshot_tables()` (e.g. with `format_date()` and `_month_label()`) actually correct?**
  _`build_snapshot_tables()` has 3 INFERRED edges - model-reasoned connections that need verification._