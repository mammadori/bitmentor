"""One count of on-chain addresses per shop and chain, shared by all the shop's registers (shops/chain_count.py)."""
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import chain_count
from .models import ChainCount, Shop, StaffRegister
from .test_experimental import OnChain

CHAIN = "a" * 64
OTHER = "b" * 64


class Counting(OnChain, TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner")
        self.shop = Shop.objects.create(name="Bar", owner=self.owner, lightning_address="bar@wallet.example")
        self.staff = StaffRegister.objects.create(shop=self.shop, name="Anna")

    def owner_asks(self, **body):
        return self.client.post(reverse("register_chain", args=[self.shop.pk]), json.dumps({"chain": CHAIN, **body}),
                                content_type="application/json")

    def staff_asks(self, key=None, **body):
        return self.client.post(reverse("staff_chain", args=[self.staff.pk]),
                                json.dumps({"chain": CHAIN, "k": key or self.staff.key, **body}), content_type="application/json")

    def test_every_register_gets_the_next_index_never_one_given(self):
        self.client.force_login(self.owner)
        chain_count.open_chain(self.shop, CHAIN)
        self.assertEqual(self.owner_asks(at_least=0).json(), {"index": 0})
        self.assertEqual(self.staff_asks(at_least=0).json(), {"index": 1})
        # A register whose own count is further on gets that one; the others go on from there
        self.assertEqual(self.owner_asks(at_least=5).json(), {"index": 5})
        self.assertEqual(self.staff_asks(at_least=2).json(), {"index": 6})
        # A staff register asking past the gap limit is refused
        self.assertEqual(self.staff_asks(at_least=7 + chain_count.STAFF_AHEAD + 1).status_code, 409)
        # Another chain (a new key or path) has its own count
        self.assertEqual(chain_count.claim(self.shop, OTHER, 0), 0)
        self.assertEqual(ChainCount.objects.get(shop=self.shop, chain=CHAIN).next, 7)

    def test_an_index_never_goes_back(self):
        self.assertEqual([chain_count.claim(self.shop, CHAIN, 0) for _ in range(3)], [0, 1, 2])
        self.assertEqual(self.staff_asks(back=2).status_code, 400)
        self.assertEqual(chain_count.claim(self.shop, CHAIN, 0), 3)

    def test_only_the_owner_opens_a_chain(self):
        # A staff register cannot fill the shop's chains, nor count on one the owner never opened
        self.assertEqual(self.staff_asks(at_least=0).status_code, 412)
        self.assertEqual(self.staff_asks(open=True).status_code, 403)
        self.assertFalse(ChainCount.objects.exists())
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(reverse("register_chain", args=[self.shop.pk]),
                                          json.dumps({"chain": CHAIN, "open": True}), content_type="application/json").json(), {})
        self.assertEqual(ChainCount.objects.get().next, 0)
        self.assertEqual(self.staff_asks(at_least=0).json(), {"index": 0})

    def test_a_staff_register_cannot_push_the_count_far(self):
        chain_count.open_chain(self.shop, CHAIN)
        self.assertEqual(self.staff_asks(at_least=chain_count.STAFF_AHEAD).json(), {"index": chain_count.STAFF_AHEAD})
        # Each jump within the gap limit of the count, and the jumps of one register are few an hour
        self.assertEqual(self.staff_asks(at_least=2 * chain_count.STAFF_AHEAD + 2).status_code, 409)
        with patch.object(chain_count, "CLAIMS", 3):
            statuses = [self.staff_asks(at_least=0).status_code for _ in range(3)]
        self.assertEqual(statuses[-1], 429)
        # The owner's register has its own budget, and sets the start of the addresses where the wallet is
        self.client.force_login(self.owner)
        self.assertEqual(self.owner_asks(at_least=5000).json(), {"index": 5000})
        self.assertEqual(self.owner_asks(at_least=chain_count.MAX_INDEX - 1).json(), {"index": chain_count.MAX_INDEX - 1})
        self.assertEqual(self.owner_asks(at_least=0).status_code, 409)

    def test_only_the_owner_signed_in_or_a_register_with_its_key(self):
        self.assertEqual(self.owner_asks(at_least=0).status_code, 403)
        self.client.force_login(get_user_model().objects.create_user("other"))
        self.assertEqual(self.owner_asks(at_least=0).status_code, 403)
        self.client.logout()
        self.assertEqual(self.staff_asks(key="x" * 40, at_least=0).status_code, 403)
        StaffRegister.objects.filter(pk=self.staff.pk).update(retired=timezone.now())
        chain_count.open_chain(self.shop, CHAIN)
        self.assertEqual(self.staff_asks(at_least=0).status_code, 403)
        self.assertEqual(ChainCount.objects.get().next, 0)

    def test_the_owner_needs_the_csrf_token(self):
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        url = reverse("register_chain", args=[self.shop.pk])
        body = json.dumps({"chain": CHAIN, "at_least": 0})
        self.assertEqual(client.post(url, body, content_type="application/json").status_code, 403)
        page = client.get(reverse("register", args=[self.shop.pk]))
        token = page.context["register"]["chain_claim"]["csrf"]
        self.assertEqual(client.post(url, body, content_type="application/json", HTTP_X_CSRFTOKEN=token).json(), {"index": 0})

    def test_only_a_name_and_one_number(self):
        self.client.force_login(self.owner)
        for body in ({"chain": "A" * 64, "at_least": 0}, {"chain": CHAIN}, {"chain": CHAIN, "at_least": -1},
                     {"chain": CHAIN, "at_least": 1.5}, {"chain": CHAIN, "at_least": True}, {"chain": CHAIN, "back": 1}, {"chain": CHAIN, "open": 1},
                     {"chain": CHAIN, "at_least": chain_count.MAX_INDEX}):
            with self.subTest(body=body):
                response = self.client.post(reverse("register_chain", args=[self.shop.pk]), json.dumps(body), content_type="application/json")
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post(reverse("register_chain", args=[self.shop.pk]), "x" * 2000, content_type="application/json").status_code, 400)

    def test_a_shop_has_few_chains(self):
        for n in range(chain_count.CHAINS_PER_SHOP):
            chain_count.claim(self.shop, f"{n:064x}", 0)
        with self.assertRaises(chain_count.Far):
            chain_count.claim(self.shop, CHAIN, 0)

    def test_the_pages_carry_where_to_ask(self):
        self.client.force_login(self.owner)
        data = self.client.get(reverse("register", args=[self.shop.pk])).context["register"]
        self.assertEqual(data["chain_claim"]["url"], reverse("register_chain", args=[self.shop.pk]))
        self.client.logout()
        self.assertNotIn("chain_claim", self.client.get(reverse("register", args=[self.shop.pk])).context["register"])
