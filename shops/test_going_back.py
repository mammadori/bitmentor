"""Every foreign key of the site's tables does in the database what its model says (shops/going_back.py), so that an
earlier image put back on a migrated database can still delete a shop or an account. The rules are set after every
`migrate` by a post_migrate handler that changes only the keys that differ: PostgreSQL only."""
from unittest import skipIf, skipUnless
from unittest.mock import MagicMock

from django.apps import apps
from django.db import connection, models
from django.test import SimpleTestCase, TestCase

from . import going_back


def fake_connection(rules, vendor="postgresql"):
    """A connection whose cursor answers pg_constraint with `rules` ({(table, column): confdeltype}) and records the
    statements."""
    connection_ = MagicMock()
    connection_.vendor = vendor
    connection_.ops.quote_name = lambda name: f'"{name}"'
    cursor = connection_.cursor.return_value.__enter__.return_value
    asked = {}

    def execute(sql, params=None):
        if params:
            asked["last"] = tuple(params)

    cursor.execute.side_effect = execute
    cursor.fetchall.side_effect = lambda: ([(f"{asked['last'][0]}_{asked['last'][1]}_fk", rules[asked["last"]])]
                                           if asked["last"] in rules else [])
    return connection_, cursor


def statements(cursor):
    return [call.args[0] for call in cursor.execute.call_args_list if not call.args[1:]]


class ForeignKeys(SimpleTestCase):
    def test_every_foreign_key_of_the_sites_apps_and_only_those(self):
        found = going_back.site_foreign_keys(apps)
        labels = {model._meta.app_label for model, _field in found}
        self.assertLessEqual(labels, set(going_back.APPS))
        self.assertTrue(all(isinstance(field, models.ForeignKey) for _model, field in found))
        everything = {(model._meta.label_lower, field.name) for model in apps.get_models()
                      if model._meta.app_label in going_back.APPS for field in model._meta.local_fields
                      if isinstance(field, models.ForeignKey)}
        self.assertEqual({(model._meta.label_lower, field.name) for model, field in found}, everything)
        for pair in (("shops.staffregister", "shop"), ("shops.closure", "shop"), ("shops.chaincount", "shop")):
            self.assertIn(pair, everything)
        # Every one of them has a rule the database can hold: none would be skipped by mistake
        self.assertEqual([(model._meta.label_lower, field.name) for model, field in found
                          if field.remote_field.on_delete not in going_back.ACTIONS], [])

    def test_only_the_keys_that_differ_are_changed(self):
        pairs = going_back.site_foreign_keys(apps)
        rules = {(model._meta.db_table, field.column): going_back.CODES[going_back.ACTIONS[field.remote_field.on_delete]]
                 for model, field in pairs}
        wrong_model, wrong_field = pairs[0]
        rules[(wrong_model._meta.db_table, wrong_field.column)] = "a"  # NO ACTION, as Django makes them
        connection_, cursor = fake_connection(rules)
        changed = going_back.align(connection_, pairs)
        self.assertEqual(changed, [f"{wrong_model._meta.db_table}.{wrong_field.column}"])
        done = statements(cursor)
        self.assertEqual(done[0], "SET LOCAL lock_timeout = '30s'")
        self.assertEqual(len(done), 2)
        action = going_back.ACTIONS[wrong_field.remote_field.on_delete]
        self.assertRegex(done[1], r'^ALTER TABLE "\w+" DROP CONSTRAINT "\w+", ADD CONSTRAINT "\w+" FOREIGN KEY \("\w+"\) '
                                  rf'REFERENCES "\w+" \("\w+"\) ON DELETE {action} DEFERRABLE INITIALLY DEFERRED$')
        # The constraint keeps its name
        self.assertEqual(done[1].split("DROP CONSTRAINT ")[1].split(",")[0], done[1].split("ADD CONSTRAINT ")[1].split(" FOREIGN")[0])

    def test_keys_already_right_change_nothing(self):
        pairs = going_back.site_foreign_keys(apps)
        rules = {(model._meta.db_table, field.column): going_back.CODES[going_back.ACTIONS[field.remote_field.on_delete]]
                 for model, field in pairs}
        connection_, cursor = fake_connection(rules)
        self.assertEqual(going_back.align(connection_, pairs), [])
        self.assertEqual(statements(cursor), [])

    def test_a_rule_other_than_cascade_or_set_null_is_left_alone(self):
        protect = MagicMock()
        protect.remote_field.on_delete = models.PROTECT
        connection_, cursor = fake_connection({})
        self.assertEqual(going_back.align(connection_, [(MagicMock(), protect)]), [])
        cursor.execute.assert_not_called()

    def test_a_key_with_no_constraint_stops(self):
        pairs = going_back.site_foreign_keys(apps)[:1]
        connection_, _cursor = fake_connection({})
        with self.assertRaisesMessage(RuntimeError, "expected one foreign key constraint"):
            going_back.align(connection_, pairs)

    def test_nothing_is_touched_where_the_database_cannot_change_a_constraint(self):
        connection_, _cursor = fake_connection({}, vendor="sqlite")
        self.assertEqual(going_back.align(connection_, going_back.site_foreign_keys(apps)), [])
        connection_.cursor.assert_not_called()


class InTheDatabase(TestCase):
    @skipIf(connection.vendor == "postgresql", "SQLite only")
    def test_sqlite_is_left_as_it_is(self):
        self.assertEqual(going_back.align(connection, going_back.site_foreign_keys(apps)), [])

    @skipUnless(connection.vendor == "postgresql", "only PostgreSQL changes a constraint in place")
    def test_after_migrate_every_key_has_the_rule_of_its_model(self):
        pairs = going_back.site_foreign_keys(apps)
        with connection.cursor() as cursor:
            for model, field in pairs:
                _name, code = going_back.constraint_of(cursor, model._meta.db_table, field.column)
                with self.subTest(key=f"{model._meta.db_table}.{field.column}"):
                    self.assertEqual(code, going_back.CODES[going_back.ACTIONS[field.remote_field.on_delete]])
        # A second run changes nothing
        self.assertEqual(going_back.align(connection, pairs), [])

    @skipUnless(connection.vendor == "postgresql", "only PostgreSQL changes a constraint in place")
    def test_a_key_put_back_to_no_action_is_put_right_alone(self):
        from .models import ChainCount

        field = ChainCount._meta.get_field("shop")
        table = ChainCount._meta.db_table
        with connection.cursor() as cursor:
            name, _code = going_back.constraint_of(cursor, table, field.column)
            cursor.execute(f'ALTER TABLE "{table}" DROP CONSTRAINT "{name}", ADD CONSTRAINT "{name}" FOREIGN KEY '
                           f'("{field.column}") REFERENCES "shops_shop" ("id") DEFERRABLE INITIALLY DEFERRED')
        self.assertEqual(going_back.align(connection, going_back.site_foreign_keys(apps)), [f"{table}.{field.column}"])
        with connection.cursor() as cursor:
            self.assertEqual(going_back.constraint_of(cursor, table, field.column)[1], "c")

    @skipUnless(connection.vendor == "postgresql", "only PostgreSQL changes a constraint in place")
    def test_the_database_deletes_the_chain_counts_of_a_deleted_shop(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT confdeltype FROM pg_constraint WHERE conrelid = 'shops_chaincount'::regclass AND contype = 'f'")
            self.assertEqual([row[0] for row in cursor.fetchall()], ["c"])
