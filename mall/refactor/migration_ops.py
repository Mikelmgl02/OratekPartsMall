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


def on_delete_in_database(keys):
    """A RunPython that gives each foreign key (table, column, action) its ON DELETE in PostgreSQL: the constraint Django made is dropped
    and made again, to the same table and column, with the action, still DEFERRABLE INITIALLY DEFERRED. A no-op elsewhere. An expand
    adds it to the keys of fields the code before it does not know: a row that code deletes then takes them along (CASCADE) or leaves
    them NULL (SET NULL), as Django would, instead of failing on the constraint (inventory refactor S6, a code-only revert). Its reverse
    is a no-op: the reverse of the expand drops the columns."""
    def forwards(apps, schema_editor):
        if schema_editor.connection.vendor != 'postgresql':
            return
        # Django creates the keys of the fields this migration added at its end (deferred SQL): created now, so they can be changed.
        for sql in list(schema_editor.deferred_sql):
            schema_editor.execute(sql)
        schema_editor.deferred_sql.clear()
        with schema_editor.connection.cursor() as cursor:
            for table, column, action in keys:
                cursor.execute(
                    "SELECT c.conname, c.confrelid::regclass::text, "
                    "(SELECT r.attname FROM pg_attribute r WHERE r.attrelid = c.confrelid AND r.attnum = c.confkey[1]) "
                    "FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
                    "WHERE c.conrelid = %s::regclass AND c.contype = 'f' AND a.attname = %s AND array_length(c.conkey, 1) = 1",
                    [table, column])
                for name, target, referenced in cursor.fetchall():
                    cursor.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{name}"')
                    cursor.execute(f'ALTER TABLE {table} ADD CONSTRAINT "{name}" FOREIGN KEY ("{column}") REFERENCES {target} ("{referenced}") '
                                   f'ON DELETE {action} DEFERRABLE INITIALLY DEFERRED')
    from django.db import migrations
    return migrations.RunPython(forwards, migrations.RunPython.noop)
