"""Tools of the inventory refactor (OEM line ← Reference ← Variant): one backfill CLI per stage (python -m mall.refactor.<stage>
--dry-run | --apply --user <name> | --rollback), the live-database guard they share (guard) and the operations its migrations start
with (migration_ops). Each stage's CLI runs with the catalog freeze on (mall.maintenance) and its connection shows in pg_stat_activity
as refactor:<stage>."""
