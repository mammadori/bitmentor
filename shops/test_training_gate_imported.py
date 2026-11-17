"""The shops of the community's list have their own gate (`training.gate_imported`, `reminder` unless written)."""
from django.test import SimpleTestCase

from .models import Shop
from .training import Config, gate_of


def config(**values):
    return Config(**{"gate": "unlock", "review_months": 12, "refresh_days": 30, "since": None, "public_sign": True,
                     **values})


class GateOf(SimpleTestCase):
    def test_an_imported_shop_is_reminded_a_new_one_locked(self):
        cfg = config()
        self.assertEqual(gate_of(cfg, Shop(external_id="12")), "reminder")
        self.assertEqual(gate_of(cfg, Shop()), "unlock")

    def test_the_community_can_lock_the_imported_ones_too(self):
        self.assertEqual(gate_of(config(gate_imported="unlock"), Shop(external_id="12")), "unlock")
