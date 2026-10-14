"""Tools of the inventory refactor (OEM line ← Reference ← Variant): one backfill CLI per stage (python -m mall.refactor.<stage>
--dry-run | --apply --user <name> | --rollback), the live-database guard they share (guard), the lock timeout its migrations are
wrapped in (migration_ops) and the migrate that retries one that timed out (migrate_retry). Each stage's CLI runs with the catalog
freeze on (mall.maintenance) and its connection shows in pg_stat_activity as refactor:<stage>."""
