"""Every foreign key of the site's tables does in the database what its model says, so that going back stays possible
(docs/releases.md, "Going back").

When a release does not answer, deploy/release.sh puts the previous image back on the database the release migrated.
That image deletes a shop or an account the way it knows (Django cascades in Python, over the tables it has), so a row of
a table the release added, with a foreign key to one of them, would stop the delete with an IntegrityError. Django
creates its foreign keys with no rule in the database (ON DELETE NO ACTION): after every `migrate`, `align` gives each
one the rule of its model, CASCADE or SET NULL. It reads pg_constraint and changes only the keys that differ, so a
second `migrate` changes nothing. A NOT NULL column added to an old table gets a `db_default` instead.
Only on PostgreSQL, which production runs: SQLite (development and tests) cannot change a constraint in place.
"""
from django.db import connections, models

ACTIONS = {models.CASCADE: "CASCADE", models.SET_NULL: "SET NULL"}
# pg_constraint.confdeltype of each rule
CODES = {"CASCADE": "c", "SET NULL": "n"}
# The apps whose tables the site owns
APPS = ("shops", "meetups", "team")


def foreign_keys_of(apps, *labels):
    """The (model, field) pairs of every foreign key of the models called `app_label.modelname`, in a migration's `apps`."""
    for label in labels:
        model = apps.get_model(label)
        for field in model._meta.local_fields:
            if isinstance(field, models.ForeignKey):
                yield model, field


def site_foreign_keys(apps):
    """Every foreign key of the site's own models (APPS), unmanaged and proxy models left out."""
    labels = [model._meta.label_lower for model in apps.get_models()
              if model._meta.app_label in APPS and model._meta.managed and not model._meta.proxy]
    return list(foreign_keys_of(apps, *labels))


def constraint_of(cursor, table, column):
    """(name, confdeltype) of the one foreign key constraint on `table`.`column`; RuntimeError if there is not one."""
    cursor.execute(
        "SELECT con.conname, con.confdeltype FROM pg_constraint con "
        "JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = con.conkey[1] "
        "WHERE con.contype = 'f' AND con.conrelid = to_regclass(%s) AND array_length(con.conkey, 1) = 1 "
        "AND att.attname = %s", [table, column])
    found = cursor.fetchall()
    if len(found) != 1:
        raise RuntimeError(f"{table}.{column}: expected one foreign key constraint, found {[row[0] for row in found]}")
    return found[0]


def align(connection, foreign_keys):
    """Gives each foreign key whose rule in the database differs from its model's (CASCADE or SET NULL; any other rule
    is left alone) that rule. Returns the "table.column" changed. Nothing on a database other than PostgreSQL."""
    if connection.vendor != "postgresql":
        return []
    quote = connection.ops.quote_name
    changed = []
    with connection.cursor() as cursor:
        for model, field in foreign_keys:
            action = ACTIONS.get(field.remote_field.on_delete)
            if not action:
                continue
            table, column, target = model._meta.db_table, field.column, field.target_field
            # `migrate` to an earlier target leaves some tables out: their keys come with the migration that makes them
            cursor.execute("SELECT to_regclass(%s) IS NOT NULL", [table])
            if not cursor.fetchone()[0]:
                continue
            name, code = constraint_of(cursor, table, column)
            if code == CODES[action]:
                continue
            if not changed:
                # The constraint is dropped and added again: a busy table must not make the release wait for ever
                cursor.execute("SET LOCAL lock_timeout = '30s'")
            cursor.execute(
                f"ALTER TABLE {quote(table)} DROP CONSTRAINT {quote(name)}, ADD CONSTRAINT {quote(name)} "
                f"FOREIGN KEY ({quote(column)}) REFERENCES {quote(target.model._meta.db_table)} ({quote(target.column)}) "
                f"ON DELETE {action} DEFERRABLE INITIALLY DEFERRED")
            changed.append(f"{table}.{column}")
    return changed


def on_post_migrate(sender, apps=None, using="default", plan=None, **kwargs):
    """After `migrate` (once, sent for the shops app): the rules of the site's foreign keys, in one transaction."""
    from django.db import transaction

    if apps is None:
        return
    connection = connections[using]
    with transaction.atomic(using=using):
        changed = align(connection, site_foreign_keys(apps))
    verbosity = kwargs.get("verbosity", 1)
    if changed and verbosity:
        print(f"  The database's rule on delete of {len(changed)} foreign keys made as their models say")
