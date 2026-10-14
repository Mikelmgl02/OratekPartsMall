"""Test runner of the old-code compatibility gate (inventory refactor plan §1.4): the previous main's suite against a test database the
worktree migrated up to its expand migration.

An expand adds tables that point at old ones (S2a: mall_referencesource → mall_partcode). The old code does not know them, so its
TransactionTestCase flushes (TRUNCATE without CASCADE) fail with "cannot truncate a table referenced in a foreign key constraint";
nothing on the live database truncates. This runner flushes with CASCADE and changes nothing else. It is copied into the old code's
copy, never imported by the app:

    git -C <worktree> archive <old main> manage.py config mall | docker exec -i partsmall-app-1 tar -x -C /tmp/<tag>-old
    docker cp scripts/refactor_compat_runner.py partsmall-app-1:/tmp/<tag>-old/config/compat_runner.py
    # the worktree's copy creates and migrates the database and keeps it (any one test, --keepdb):
    docker exec -w /tmp/<tag>-new -e TEST_DB_NAME=test_partsmall_<tag>_compat partsmall-app-1 \\
        python manage.py test mall.test_refactor_guards --settings=config.settings_test --noinput --keepdb
    # the old suite on it:
    docker exec -w /tmp/<tag>-old -e TEST_DB_NAME=test_partsmall_<tag>_compat partsmall-app-1 \\
        python manage.py test mall --settings=config.settings_test --testrunner=config.compat_runner.CompatRunner --noinput --keepdb
    docker exec partsmall-db-1 dropdb -U partsmall test_partsmall_<tag>_compat

Expected in S2a: the old suite passes except test_refactor_snapshot.CaptureTests.test_checks_run_what_today_has_and_skip_what_no_stage_added_yet
(Appendix C's I12 runs once 0073 exists, and the old fixture has no TOYOTA brand).
"""
from config.test_runner import GuardedTestRunner


class CompatRunner(GuardedTestRunner):
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        from django.db.backends.postgresql.operations import DatabaseOperations
        flush = DatabaseOperations.sql_flush

        def sql_flush(ops, style, tables, *, reset_sequences=False, allow_cascade=False):
            return flush(ops, style, tables, reset_sequences=reset_sequences, allow_cascade=True)

        DatabaseOperations.sql_flush = sql_flush
