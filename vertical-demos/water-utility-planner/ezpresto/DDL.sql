-- =============================================================================
-- Water Utility Ranker — EzPresto Hive catalog DDL
-- =============================================================================
--
-- Creates two external tables in the `waterutility` Hive catalog:
--   1. waterutility.default.pipes        — 2,000 sewer pipes with 19 attributes
--   2. waterutility.default.work_orders  — ~340 historical inspection / repair records
--
-- Prerequisites:
--   - The `waterutility` catalog must already be registered via
--     AIE UI → Data Sources → + Add New Data Source → Hive with:
--         Name:                        waterutility
--         Hive Metastore:              file
--         Hive Metastore Catalog Dir:  file:/data/shared/dcao/water-utility/hive-metastore
--         Hive Metastore User:         presto
--     (Adjust the catalog dir path to your EzPresto coordinator's mount point.)
--
--   - The two parquet files must be present in per-table subdirectories
--     under the same shared PVC path (see README Step 3, 6.1):
--         /data/shared/dcao/water-utility/pipes/pipes_blended.parquet
--         /data/shared/dcao/water-utility/work_orders/work_orders.parquet
--
--   - The coordinator must have write access to the `hive-metastore/`
--     subdirectory (chmod 777 from Jupyter after mkdir).
--
-- Execution:
--   Run these blocks one at a time in AIE UI → Query Editor.
--   Do NOT append terminating semicolons when running via the MCP
--   `execute_query` tool — it rejects them. The semicolons here are for
--   Query Editor use.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. Pipe register — one row per sewer pipe
-- -----------------------------------------------------------------------------

CREATE TABLE waterutility.default.pipes (
  pipe_id                     VARCHAR,
  district_code               VARCHAR,
  material                    VARCHAR,
  lat                         DOUBLE,
  lon                         DOUBLE,
  year_installed              BIGINT,
  dominant_species            VARCHAR,
  soil_class                  VARCHAR,
  traffic_load_class          VARCHAR,
  age_years                   DOUBLE,
  material_ordinal            BIGINT,
  diameter_mm                 DOUBLE,
  slope_pct                   DOUBLE,
  depth_m                     DOUBLE,
  nearest_tree_dist_m         DOUBLE,
  trees_within_15m            BIGINT,
  dominant_species_riskscore  DOUBLE,
  historical_incident_count   BIGINT,
  has_root_incident_5y        BIGINT
)
WITH (
  external_location = 'file:/data/shared/dcao/water-utility/pipes/',
  format = 'PARQUET'
);


-- -----------------------------------------------------------------------------
-- 2. Work-order history — inspection and repair records, joinable by pipe_id
-- -----------------------------------------------------------------------------

CREATE TABLE waterutility.default.work_orders (
  work_order_id  VARCHAR,
  pipe_id        VARCHAR,
  opened_date    VARCHAR,
  closed_date    VARCHAR,
  work_type      VARCHAR,
  crew           VARCHAR,
  cost_aud       BIGINT
)
WITH (
  external_location = 'file:/data/shared/dcao/water-utility/work_orders/',
  format = 'PARQUET'
);


-- =============================================================================
-- Sanity checks — run these to confirm the tables are queryable
-- =============================================================================

-- Expected: 2000
SELECT COUNT(*) AS pipe_count
FROM waterutility.default.pipes;

-- Expected: ~340 (varies slightly with the notebook's synth seed)
SELECT COUNT(*) AS work_order_count
FROM waterutility.default.work_orders;


-- -----------------------------------------------------------------------------
-- The federation query — this is the JOIN the LLM writes for Beat 4 of Demo 1
-- ("Of the pipes I just ranked, how many have never had a work order logged?")
--
-- Expected: ~1660 (i.e. ~83% of all pipes have never been inspected)
-- -----------------------------------------------------------------------------

SELECT COUNT(*) AS never_inspected
FROM waterutility.default.pipes p
LEFT JOIN waterutility.default.work_orders w
  ON p.pipe_id = w.pipe_id
WHERE w.work_order_id IS NULL;


-- =============================================================================
-- Presto SQL gotcha — do NOT use USING (pipe_id) on this pattern
-- =============================================================================
--
-- The following looks equivalent but FAILS with:
--     line X:Y: 'w.pipe_id' cannot be resolved
--
--   SELECT COUNT(*) FROM waterutility.default.pipes p
--     LEFT JOIN waterutility.default.work_orders w USING (pipe_id)
--     WHERE w.pipe_id IS NULL;
--
-- USING (col) merges the join key into a single column, so after the join
-- there is no separate w.pipe_id to filter on. Use explicit ON and filter
-- on a non-key right-side column (w.work_order_id) instead.
--
-- The tool file's ezpresto_sql docstring surfaces this pattern to the LLM,
-- so it writes the join correctly.
-- =============================================================================
