from django.apps import AppConfig
from django.core import checks


def wrong_features(app_configs, **kwargs):
    """A name or a value written wrong in the `features` block: the feature stays on, and `manage.py check` and the log of
    `web` say so. A warning, not an error, which would stop `migrate` and the whole site."""
    from config import features

    return [checks.Warning(problem, hint="The block is documented in examples/community/community.yml",
                           id="shops.W003") for problem in features.problems()]


def wrong_experimental(app_configs, **kwargs):
    """A name or a value written wrong in the `experimental` block: the feature stays off, and `manage.py check` and the
    log of `web` say so. A warning, like the others."""
    from config import features

    return [checks.Warning(problem, hint="The block is documented in examples/community/community.yml",
                           id="shops.W011") for problem in features.experimental_problems()]


def wrong_limits(app_configs, **kwargs):
    """A wrong value in the `limits` block counts as the default: `manage.py check` and the log of `web` say so. A warning, like
    the others."""
    from config import limits

    return [checks.Warning(problem, hint="The block is documented in examples/community/community.yml",
                           id="shops.W009") for problem in limits.problems()]


def limits_near(app_configs, databases=None, **kwargs):
    """A total at 80% of its limit or full (config/limits.py), for `manage.py check --database default`: it reads the database,
    so the plain `check` (which runs before `migrate` and before the tests) does not run it. Warnings."""
    from config import limits

    from django.db import DatabaseError

    if not databases:
        return []
    try:
        notes = limits.notes()
    except DatabaseError:
        # `migrate` runs the database checks too, before the tables exist: a new database has nothing to count yet
        return []
    return [checks.Warning(note, hint="docs/configuration.md#limits", id="shops.W010") for note in notes]


def wrong_privacy(app_configs, **kwargs):
    """A number written wrong in the `privacy` block counts as its default: `manage.py check` and the log of `web` say so.
    A warning, like the others."""
    from .privacy import problems

    return [checks.Warning(problem, hint="The block is documented in examples/community/community.yml",
                           id="shops.W004") for problem in problems()]


def wrong_training(app_configs, **kwargs):
    """A part of the `training` block that cannot be used is left out, and the training goes on without it: `manage.py check`
    and the log of `web` say what and where. Warnings, not errors."""
    from .training import problems

    return [checks.Warning(problem, hint="The format is next to the `training` block in examples/community/community.yml",
                           id="shops.W005") for problem in problems()]


def wrong_explorers(app_configs, **kwargs):
    """An explorer in `register.explorers` that is not an https address of an API is left out (mempool.space when none is
    left): `manage.py check` and the log of `web` say so. A warning, like the others."""
    from .register import explorer_problems

    return [checks.Warning(problem, hint="The block is documented in examples/community/community.yml",
                           id="shops.W006") for problem in explorer_problems()]


def wrong_relays(app_configs, **kwargs):
    """A relay in `register.relays` or `register.public_relays` that is not a wss address is left out."""
    from .register import relay_problems

    return [checks.Warning(problem, hint="The block is documented in examples/community/community.yml",
                           id="shops.W007") for problem in relay_problems()]


def wrong_register_numbers(app_configs, **kwargs):
    """`register.tolerance` or `register.max_rate_age` the register cannot use as written: the default (or the most)
    applies."""
    from .register import number_problems

    return [checks.Warning(problem, hint="The block is documented in examples/community/community.yml",
                           id="shops.W008") for problem in number_problems()]


def wrong_hooks(app_configs, **kwargs):
    """`COMMUNITY_HOOKS` names a module that cannot be imported, or one of its hooks is not a function: nothing is
    called, and `manage.py check` and the log of `web` say so."""
    from config import hooks

    return [checks.Warning(problem, hint="The hooks are described in docs/architecture.md", id="shops.W012")
            for problem in hooks.problems()]


class ShopsConfig(AppConfig):
    name = "shops"

    def ready(self):
        checks.register(wrong_features)
        checks.register(wrong_experimental)
        checks.register(wrong_limits)
        checks.register(limits_near, checks.Tags.database)
        checks.register(wrong_privacy)
        checks.register(wrong_training)
        checks.register(wrong_explorers)
        checks.register(wrong_relays)
        checks.register(wrong_register_numbers)
        checks.register(wrong_hooks)
        from config import guard as staff_apart  # noqa: F401  (a new sign-in ends the admin's session)

        from django.db.models.signals import post_migrate

        from . import address_change  # noqa: F401  (every sign-in writes its time: the change asks a recent one)
        from . import guard  # noqa: F401  (its signals watch the accounts of the shops' owners)
        from .moderators import on_post_migrate
        from .public_cache import connect as public_list_cache

        # A shop saved or deleted rebuilds the public list of shops at the next request (shops/public_cache.py)
        public_list_cache()

        # The moderators' group is made, and put right, at the end of every migrate (shops/moderators.py)
        post_migrate.connect(on_post_migrate, sender=self, dispatch_uid="shops.moderators")
        # The rule on delete of every foreign key, in the database too, at the end of every migrate (shops/going_back.py)
        from .going_back import on_post_migrate as foreign_keys_on_delete

        post_migrate.connect(foreign_keys_on_delete, sender=self, dispatch_uid="shops.going_back")
