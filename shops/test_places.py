"""A shop with several branches: one shop, one register, a point on the map for each place (ShopPlace)."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from .models import Shop, ShopPlace
from .tests import reset_limits


class Places(TestCase):
    def setUp(self):
        reset_limits()
        self.owner = get_user_model().objects.create(username="email:" + "a" * 32)
        self.shop = Shop.objects.create(name="Driving school", owner=self.owner, address="Main street 3", city="Northtown",
                                        latitude=45.9, longitude=11.0, approved_on=timezone.now(), lightning=True)
        self.client.force_login(self.owner)
        self.page = f"/en/shops/places/{self.shop.id}/"

    def add(self, **fields):
        return self.client.post(self.page, {"address": "Side road 90", "city": "Southvillage", "latitude": "45,913861",
                                            "longitude": "11.032598", **fields})

    def listed(self):
        return {shop["name"]: shop for shop in self.client.get("/shops.json").json()["shops"]}

    def test_the_owner_adds_a_place_and_takes_it_away(self):
        self.assertContains(self.client.get(f"/en/shops/edit/{self.shop.id}/"), self.page)
        self.assertContains(self.client.get(self.page), "No other place yet.")
        self.assertRedirects(self.add(), self.page)
        place = ShopPlace.objects.get()
        self.assertEqual((place.shop, place.address, place.city, place.latitude, place.longitude),
                         (self.shop, "Side road 90", "Southvillage", 45.913861, 11.032598))
        self.assertContains(self.client.get(self.page), "Side road 90, Southvillage")
        self.client.post(self.page, {"remove": "²"})
        self.client.post(self.page, {"remove": place.pk})
        self.assertFalse(ShopPlace.objects.exists())

    def test_a_place_needs_its_point_and_a_name_and_takes_no_secret(self):
        for fields in ({"latitude": ""}, {"latitude": "200"}, {"address": "", "city": ""},
                       {"address": " ".join(["abandon"] * 11 + ["about"])}):
            self.assertEqual(self.add(**fields).status_code, 400, fields)
        self.assertFalse(ShopPlace.objects.exists())
        self.add(address="Side‮ road", city="")
        self.assertEqual(ShopPlace.objects.get().address, "Side road")

    def test_only_the_owner_writes_the_places_and_they_have_a_limit(self):
        other = get_user_model().objects.create(username="email:" + "b" * 32)
        self.client.force_login(other)
        self.assertEqual(self.client.get(self.page).status_code, 404)
        self.assertEqual(self.add().status_code, 404)
        self.client.logout()
        self.assertEqual(self.add().status_code, 302)
        self.assertFalse(ShopPlace.objects.exists())
        self.client.force_login(self.owner)
        mine = ShopPlace.objects.create(shop=Shop.objects.create(name="Other", owner=other), latitude=1, longitude=1)
        self.client.post(self.page, {"remove": mine.pk})  # a place of somebody else's shop stays
        self.assertTrue(ShopPlace.objects.filter(pk=mine.pk).exists())
        ShopPlace.objects.bulk_create([ShopPlace(shop=self.shop, city="X", latitude=1, longitude=1)] * ShopPlace.MAX)
        self.assertEqual(self.add().status_code, 400)
        self.assertEqual(self.shop.places.count(), ShopPlace.MAX)
        self.assertNotContains(self.client.get(self.page), 'id="pick-map"')

    def test_the_json_gives_the_other_places_and_keeps_todays_fields(self):
        before = self.listed()["Driving school"]
        self.assertNotIn("places", before)
        self.add()
        after = self.listed()["Driving school"]  # the list is built again when a place is saved
        self.assertEqual(after.pop("places"), [{"address": "Side road 90", "city": "Southvillage",
                                               "latitude": 45.913861, "longitude": 11.032598}])
        self.assertEqual(after, before)
        self.assertEqual(len(self.listed()), 1)  # one shop, however many places
        # An owner who hides the address hides the places' too
        Shop.objects.filter(pk=self.shop.pk).update(show_address=False)
        self.shop.refresh_from_db()
        self.shop.save()
        self.assertEqual(self.listed()["Driving school"]["places"],
                         [{"city": "Southvillage", "latitude": 45.913861, "longitude": 11.032598}])
        ShopPlace.objects.all().delete()
        self.assertNotIn("places", self.listed()["Driving school"])

    def test_the_map_list_names_the_towns_and_counts_the_shop_once(self):
        self.add()
        self.add(address="Garden way 17", city="Northtown", latitude="45.92", longitude="10.88")
        page = self.client.get("/en/shops/map/")
        self.assertContains(page, "<span>Northtown, Southvillage</span>")
        self.assertEqual(page.content.decode().count("<strong>Driving school</strong>"), 1)

    def test_the_shops_page_shows_the_other_places(self):
        self.add()
        page = self.client.get(f"/en/shops/s/{self.shop.id}/")
        self.assertContains(page, "Side road 90, Southvillage")
        self.assertContains(page, 'data-places="45.913861,11.032598"')
        self.assertContains(page, "to=45.913861%2C11.032598")
        # Hidden address: the town alone; a suspended shop: no other place
        Shop.objects.filter(pk=self.shop.pk).update(show_address=False)
        page = self.client.get(f"/en/shops/s/{self.shop.id}/")
        self.assertNotContains(page, "Side road 90")
        self.assertContains(page, "Southvillage")
        Shop.objects.filter(pk=self.shop.pk).update(suspended=True)
        self.assertNotContains(self.client.get(f"/en/shops/s/{self.shop.id}/"), "Southvillage")

    def test_the_places_go_with_the_shop(self):
        self.add()
        self.shop.delete()
        self.assertFalse(ShopPlace.objects.exists())


class EditPlaces(TestCase):
    """The owner changes a place in the same form, instead of removing it and adding it again."""
    setUp = Places.setUp
    add = Places.add
    listed = Places.listed

    def change(self, place=None, **fields):
        return self.client.post(self.page, {"place": self.place.pk if place is None else place, "address": "New road 1",
                                            "city": "Southvillage", "latitude": "45.95", "longitude": "11.05", **fields})

    def mine(self):
        self.add()
        self.place = ShopPlace.objects.get()
        return self.place

    def test_the_owner_changes_a_place_and_its_link_stays(self):
        place = self.mine()
        ShopPlace.objects.filter(pk=place.pk).update(osm_ref="node/42")
        page = self.client.get(f"{self.page}?edit={place.pk}")
        self.assertContains(page, 'value="Side road 90"')
        self.assertContains(page, f'name="place" value="{place.pk}"')
        self.assertContains(page, "Save this place")
        self.assertContains(self.client.get(self.page), f'href="?edit={place.pk}"')
        self.assertEqual(self.listed()["Driving school"]["places"][0]["address"], "Side road 90")
        self.assertRedirects(self.change(), self.page)
        place.refresh_from_db()
        self.assertEqual((place.address, place.latitude, place.longitude, place.osm_ref),
                         ("New road 1", 45.95, 11.05, "node/42"))
        self.assertEqual(ShopPlace.objects.count(), 1)
        # The public list is built again, as when a place is added
        self.assertEqual(self.listed()["Driving school"]["places"][0]["address"], "New road 1")

    def test_a_change_is_checked_like_a_new_place(self):
        place = self.mine()
        for fields in ({"latitude": ""}, {"latitude": "200"}, {"address": "", "city": ""},
                       {"address": " ".join(["abandon"] * 11 + ["about"])}):
            self.assertEqual(self.change(**fields).status_code, 400, fields)
        place.refresh_from_db()
        self.assertEqual(place.address, "Side road 90")
        self.change(address="New\u202e road", city="")
        place.refresh_from_db()
        self.assertEqual(place.address, "New road")

    def test_a_full_shop_still_changes_its_places(self):
        place = self.mine()
        ShopPlace.objects.bulk_create([ShopPlace(shop=self.shop, city="X", latitude=1, longitude=1)] * (ShopPlace.MAX - 1))
        self.assertContains(self.client.get(f"{self.page}?edit={place.pk}"), 'id="pick-map"')
        self.assertRedirects(self.change(), self.page)
        self.assertEqual(self.shop.places.count(), ShopPlace.MAX)

    def test_only_a_place_of_the_owners_shop_is_changed(self):
        place = self.mine()
        other = get_user_model().objects.create(username="email:" + "b" * 32)
        theirs = ShopPlace.objects.create(shop=Shop.objects.create(name="Other", owner=other), city="Far", latitude=1,
                                          longitude=1)
        for key in (theirs.pk, "9999", "x", "1e3", "²", "9" * 30):
            self.assertEqual(self.client.get(f"{self.page}?edit={key}").status_code, 404, key)
            self.assertEqual(self.change(place=key).status_code, 404, key)
        theirs.refresh_from_db()
        self.assertEqual(theirs.city, "Far")
        self.assertEqual(ShopPlace.objects.count(), 2)  # nothing added either
        # Somebody else, on my shop's page: not found; signed out: to the sign in
        self.client.force_login(other)
        self.assertEqual(self.change(place=place.pk).status_code, 404)
        self.client.logout()
        self.assertEqual(self.change(place=place.pk).status_code, 302)
        place.refresh_from_db()
        self.assertEqual(place.address, "Side road 90")

    def test_a_change_needs_the_csrf_token(self):
        from django.test import Client
        place = self.mine()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(self.page, {"place": place.pk, "address": "New road 1", "city": "",
                                                 "latitude": "45.95", "longitude": "11.05"}).status_code, 403)
        place.refresh_from_db()
        self.assertEqual(place.address, "Side road 90")
