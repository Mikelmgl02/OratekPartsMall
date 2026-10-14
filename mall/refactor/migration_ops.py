"""Operations the refactor's migrations use. Only Django is imported here, so a migration that names them never imports app code.

Every expand, tighten and contract migration of the refactor wraps its operations in with_lock_timeout, which puts a LockTimeout at
both ends: Django reverses a migration by running its operations last to first, so the closing one is what covers the DDL of a
rollback (rollback rung 3).

    operations = with_lock_timeout([migrations.AddField(...), ...])

Its DDL then waits at most that long for a table lock instead of queueing every request on that table behind a long transaction
(a matching pass, an import batch). When it times out (`canceling statement due to lock timeout`), the migration's transaction rolls
back whole and python -m mall.refactor.migrate_retry runs it again. SET LOCAL ends with the migration's transaction; a non-atomic
migration sets it for the rest of the migrate session instead."""
import re

from django.db.migrations.operations.base import Operation

TIMEOUT = re.compile(r'^\d+(us|ms|s|min|h|d)?$')


class LockTimeout(Operation):
    reversible = True
    reduces_to_sql = True
    elidable = True

    def __init__(self, timeout='5s'):
        if not TIMEOUT.match(str(timeout)):
            raise ValueError(f'lock_timeout no válido: {timeout!r} (por ejemplo 5s, 500ms o 1min).')
        self.timeout = str(timeout)

    def state_forwards(self, app_label, state):
        pass

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        self.set(schema_editor)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        self.set(schema_editor)

    def set(self, schema_editor):
        if schema_editor.connection.vendor != 'postgresql':
            return
        scope = 'LOCAL ' if schema_editor.atomic_migration else ''
        schema_editor.execute(f"SET {scope}lock_timeout = '{self.timeout}'")

    def describe(self):
        return f'Wait at most {self.timeout} for table locks'

    @property
    def migration_name_fragment(self):
        return 'lock_timeout'


def with_lock_timeout(operations, timeout='5s'):
    """The migration's operations between two LockTimeouts: the first covers applying it, the last unapplying it."""
    return [LockTimeout(timeout), *operations, LockTimeout(timeout)]
