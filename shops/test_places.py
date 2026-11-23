"""A shop with several branches: one shop, one register, a point on the map for each place (ShopPlace)."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from . import places
from .models import Shop, ShopPlace
from .tests import reset_limits


class Places(TestCase):
    def setUp(self):
        reset_limits()
        self.owner = get_user_model().objects.create(username="email:" + "a" * 32)
        self.shop = Shop.objects.create(
            name="Driving school",
            owner=self.owner,
            address="Main street 3",
            city="Northtown",
            latitude=45.9,
            longitude=11.0,
            approved_on=timezone.now(),
            lightning=True,
        )
        self.client.force_login(self.owner)
        self.page = f"/en/shops/places/{self.shop.id}/"

    def add(self, **fields):
        return self.client.post(
            self.page,
            {
                "address": "Side road 90",
                "city": "Southvillage",
                "latitude": "45,913861",
                "longitude": "11.032598",
                "on_map": "on",
                **fields,
            },
        )

    def approve(self):
        """Someone of the community approves what the shop's places wait for (shops/places.py)."""
        return places.approve(self.shop, "tester")

    def listed(self):
        return {shop["name"]: shop for shop in self.client.get("/shops.json").json()["shops"]}

    def test_the_owner_adds_a_place_and_takes_it_away(self):
        self.assertContains(self.client.get(f"/en/shops/edit/{self.shop.id}/"), self.page)
        self.assertContains(self.client.get(self.page), "No other place yet.")
        self.assertRedirects(self.add(), self.page)
        place = ShopPlace.objects.get()
        self.assertEqual(
            (place.shop, place.address, place.city, place.latitude, place.longitude),
            (self.shop, "Side road 90", "Southvillage", 45.913861, 11.032598),
        )
        self.assertContains(self.client.get(self.page), "Side road 90, Southvillage")
        self.client.post(self.page, {"remove": "²"})
        self.client.post(self.page, {"remove": place.pk})
        # Taken away: no longer among the owner's places, kept for a while to take back (PlaceTrash below)
        self.assertFalse(places.kept(self.shop).exists())
        self.assertNotContains(self.client.get(self.page), 'href="?edit=')

    def test_a_place_needs_its_point_and_a_name_and_takes_no_secret(self):
        for fields in (
            {"latitude": ""},
            {"latitude": "200"},
            {"address": "", "city": ""},
            {"address": " ".join(["abandon"] * 11 + ["about"])},
        ):
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
        self.assertNotIn("places", self.listed()["Driving school"])  # it waits for an approval (PlaceApproval below)
        self.approve()
        after = self.listed()["Driving school"]  # the list is built again when a place is saved
        self.assertEqual(
            after.pop("places"),
            [{"address": "Side road 90", "city": "Southvillage", "latitude": 45.913861, "longitude": 11.032598}],
        )
        self.assertEqual(after, before)
        self.assertEqual(len(self.listed()), 1)  # one shop, however many places
        # An owner who hides the address hides the places' too
        Shop.objects.filter(pk=self.shop.pk).update(show_address=False)
        self.shop.refresh_from_db()
        self.shop.save()
        self.assertEqual(
            self.listed()["Driving school"]["places"],
            [{"city": "Southvillage", "latitude": 45.913861, "longitude": 11.032598}],
        )
        ShopPlace.objects.all().delete()
        self.assertNotIn("places", self.listed()["Driving school"])

    def test_the_map_list_names_the_towns_and_counts_the_shop_once(self):
        self.add()
        self.add(address="Garden way 17", city="Northtown", latitude="45.92", longitude="10.88")
        self.approve()
        page = self.client.get("/en/shops/map/")
        self.assertContains(page, "<span>Northtown, Southvillage</span>")
        self.assertEqual(page.content.decode().count("<strong>Driving school</strong>"), 1)

    def test_the_shops_page_shows_the_other_places(self):
        self.add()
        self.approve()
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
    approve = Places.approve
    listed = Places.listed

    def change(self, place=None, **fields):
        return self.client.post(
            self.page,
            {
                "place": self.place.pk if place is None else place,
                "address": "New road 1",
                "city": "Southvillage",
                "latitude": "45.95",
                "longitude": "11.05",
                "on_map": "on",
                **fields,
            },
        )

    def mine(self):
        self.add()
        self.approve()
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
        # The new address waits beside the approved one, which the public keeps reading
        place.refresh_from_db()
        self.assertEqual(
            (place.address, place.asked_address, place.asked_latitude), ("Side road 90", "New road 1", 45.95)
        )
        self.assertEqual(self.listed()["Driving school"]["places"][0]["address"], "Side road 90")
        self.assertContains(self.client.get(f"{self.page}?edit={place.pk}"), 'value="New road 1"')
        self.assertEqual(self.approve(), 1)
        place.refresh_from_db()
        self.assertEqual(
            (place.address, place.latitude, place.longitude, place.osm_ref), ("New road 1", 45.95, 11.05, "node/42")
        )
        self.assertEqual((place.asked_address, place.asked_on), ("", None))
        self.assertEqual(ShopPlace.objects.count(), 1)
        # The public list is built again, as when a place is added
        self.assertEqual(self.listed()["Driving school"]["places"][0]["address"], "New road 1")

    def test_a_change_is_checked_like_a_new_place(self):
        place = self.mine()
        for fields in (
            {"latitude": ""},
            {"latitude": "200"},
            {"address": "", "city": ""},
            {"address": " ".join(["abandon"] * 11 + ["about"])},
        ):
            self.assertEqual(self.change(**fields).status_code, 400, fields)
        place.refresh_from_db()
        self.assertEqual(place.address, "Side road 90")
        self.change(address="New\u202e road", city="")
        place.refresh_from_db()
        self.assertEqual(place.asked_address, "New road")

    def test_the_map_of_a_new_place_opens_on_the_shop_and_close_to_it(self):
        from unittest.mock import patch

        from config.community import community

        # The community's own zoom shows its whole area: a country here
        with patch.dict(community()["map"], {"zoom": 6}):
            page = self.client.get(self.page)
            self.assertContains(page, 'data-lat="45.9"')
            self.assertContains(page, 'data-zoom="13"')
            Shop.objects.filter(pk=self.shop.pk).update(latitude=None, longitude=None)
            self.assertContains(self.client.get(self.page), 'data-zoom="6"')
        # A community whose own map is already closer keeps its zoom
        with patch.dict(community()["map"], {"zoom": 16}):
            Shop.objects.filter(pk=self.shop.pk).update(latitude=45.9, longitude=11.0)
            self.assertContains(self.client.get(self.page), 'data-zoom="16"')

    def test_a_full_shop_still_changes_its_places(self):
        place = self.mine()
        ShopPlace.objects.bulk_create(
            [ShopPlace(shop=self.shop, city="X", latitude=1, longitude=1)] * (ShopPlace.MAX - 1)
        )
        self.assertContains(self.client.get(f"{self.page}?edit={place.pk}"), 'id="pick-map"')
        self.assertRedirects(self.change(), self.page)
        self.assertEqual(self.shop.places.count(), ShopPlace.MAX)

    def test_only_a_place_of_the_owners_shop_is_changed(self):
        place = self.mine()
        other = get_user_model().objects.create(username="email:" + "b" * 32)
        theirs = ShopPlace.objects.create(
            shop=Shop.objects.create(name="Other", owner=other), city="Far", latitude=1, longitude=1
        )
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
        self.assertEqual(
            client.post(
                self.page,
                {"place": place.pk, "address": "New road 1", "city": "", "latitude": "45.95", "longitude": "11.05"},
            ).status_code,
            403,
        )
        place.refresh_from_db()
        self.assertEqual(place.address, "Side road 90")


class PlaceApproval(TestCase):
    """A place its owner writes, and a new address of one, go on the map when someone of the community approved them
    (shops/places.py); everything one owner asked is approved in one go on /admin/today/."""

    setUp = Places.setUp
    add = Places.add
    approve = Places.approve
    listed = Places.listed
    change = EditPlaces.change

    def seen(self):
        """The code of what /admin/today/ says the shop's places ask, as its form carries it."""
        return places.seal([place for place in places.kept(self.shop) if place.waits])

    def staff(self, *codes):
        from django.contrib.auth.models import Permission

        from config.urls import ADMIN_SESSION

        staff = get_user_model().objects.create(username="staff", is_staff=True)
        staff.user_permissions.add(*Permission.objects.filter(content_type__app_label="shops", codename__in=codes))
        self.client.force_login(staff)
        session = self.client.session
        session[ADMIN_SESSION] = True
        session.save()
        return staff

    def test_a_place_brought_by_anyone_but_the_owner_is_approved(self):
        """An import, /admin/, a community's own code: as before these columns, the place is on the map at once."""
        place = ShopPlace.objects.create(shop=self.shop, city="Southvillage", latitude=45.9, longitude=11.0)
        self.assertTrue(place.shown)
        self.assertFalse(place.waits)
        self.assertEqual(len(self.listed()["Driving school"]["places"]), 1)

    def test_a_new_place_waits_and_the_owner_reads_so(self):
        response = self.add()
        self.assertContains(self.client.get(self.page), "Waiting for the community to approve it: not on the map yet.")
        self.assertIn("approved it", [str(m) for m in response.wsgi_request._messages][0])
        place = ShopPlace.objects.get()
        self.assertIsNone(place.approved_on)
        self.assertNotIn("places", self.listed()["Driving school"])
        self.assertNotContains(self.client.get(f"/en/shops/s/{self.shop.id}/"), "Southvillage")
        self.assertNotContains(self.client.get("/en/shops/map/"), "Southvillage")

    def test_a_community_that_approves_nothing_has_no_wait(self):
        from .test_approval import approval_off

        with approval_off():
            self.add()
            self.place = ShopPlace.objects.get()
            self.assertEqual((self.place.approved_by, self.place.shown), ("-", True))
            self.assertEqual(self.listed()["Driving school"]["places"][0]["address"], "Side road 90")
            self.change()
            self.place.refresh_from_db()
            self.assertEqual((self.place.address, self.place.asked_on), ("New road 1", None))

    def test_the_places_of_a_shop_that_waits_are_approved_with_it(self):
        from . import approval, dashboard

        Shop.objects.filter(pk=self.shop.pk).update(approved_on=None)
        self.add()
        self.assertEqual(dashboard.place_items(), [])  # asked about as a shop, once
        staff = self.staff("change_shop")
        with self.captureOnCommitCallbacks(execute=False):
            approval.approve_or_hold(Shop.objects.filter(pk=self.shop.pk), staff, confirmed=True)
        self.assertTrue(ShopPlace.objects.get().shown)

    def test_everything_one_owner_asked_is_one_line_and_one_click(self):
        from django.contrib.admin.models import LogEntry

        ShopPlace.objects.create(shop=self.shop, address="Old road 5", city="Northtown", latitude=45.9, longitude=11.0)
        self.place = ShopPlace.objects.get()
        self.change()  # a new address of an approved place
        self.add()  # and a new place
        self.add(address="Garden way 17", city="Westfield", on_map="")  # and one the owner keeps off the map
        self.staff("view_shop", "change_shop")
        page = self.client.get("/admin/today/")
        self.assertContains(page, "3 places of this shop wait for your approval before they go on the map.")
        # Each with its point: the point is what goes on the map
        self.assertContains(
            page,
            "<li>new address New road 1, Southvillage (45.95000, 11.05000) "
            "(it was Old road 5, Northtown (45.90000, 11.00000))</li>",
        )
        self.assertContains(page, "<li>new place Side road 90, Southvillage (45.91386, 11.03260)</li>")
        self.assertContains(page, f'action="/admin/today/approve-places/{self.shop.id}/"', count=1)
        response = self.client.post(f"/admin/today/approve-places/{self.shop.id}/", {"seen": self.seen()})
        self.assertRedirects(response, "/admin/today/", fetch_redirect_response=False)
        self.assertEqual(
            [(place.address, place.approved_by, place.waits) for place in self.shop.places.all()],
            [("New road 1", "staff", False), ("Side road 90", "staff", False), ("Garden way 17", "staff", False)],
        )
        self.assertEqual(LogEntry.objects.get().change_message, "Places approved: 3")
        # On the map: the two the owner wants there
        self.assertEqual(
            [place["address"] for place in self.listed()["Driving school"]["places"]], ["New road 1", "Side road 90"]
        )
        self.assertNotContains(self.client.get("/admin/today/"), "approve-places")
        again = self.client.post(f"/admin/today/approve-places/{self.shop.id}/", {"seen": "x"}, follow=True)
        self.assertContains(again, "has no place waiting")

    def test_only_who_may_change_shops_approves_and_only_with_a_post(self):
        self.add()
        self.staff("view_shop")
        self.assertContains(self.client.get("/admin/today/"), "1 place of this shop waits for your approval")
        self.assertNotContains(self.client.get("/admin/today/"), "approve-places")
        self.assertEqual(self.client.post(f"/admin/today/approve-places/{self.shop.id}/").status_code, 403)
        self.assertIsNone(ShopPlace.objects.get().approved_on)
        self.client.logout()
        self.client.force_login(self.owner)
        self.assertNotEqual(self.client.post(f"/admin/today/approve-places/{self.shop.id}/").status_code, 200)
        self.assertIsNone(ShopPlace.objects.get().approved_on)

    def test_a_get_approves_nothing(self):
        self.add()
        self.staff("view_shop", "change_shop")
        self.assertEqual(self.client.get(f"/admin/today/approve-places/{self.shop.id}/").status_code, 405)
        self.assertIsNone(ShopPlace.objects.get().approved_on)

    def test_the_places_of_a_shop_not_approved_are_not_approved_alone(self):
        Shop.objects.filter(pk=self.shop.pk).update(approved_on=None)
        self.add()
        self.staff("view_shop", "change_shop")
        self.assertEqual(self.client.post(f"/admin/today/approve-places/{self.shop.id}/").status_code, 404)
        self.assertIsNone(ShopPlace.objects.get().approved_on)

    def test_the_approved_address_written_again_withdraws_the_request(self):
        ShopPlace.objects.create(shop=self.shop, address="Old road 5", city="Northtown", latitude=45.9, longitude=11.0)
        self.place = ShopPlace.objects.get()
        self.change()
        self.change(address="Old road 5", city="Northtown", latitude="45.9", longitude="11.0")
        self.place.refresh_from_db()
        self.assertEqual((self.place.address, self.place.asked, self.place.waits), ("Old road 5", "", False))

    def test_a_place_not_approved_yet_is_changed_as_it_is(self):
        self.add()
        self.place = ShopPlace.objects.get()
        self.change()
        self.place.refresh_from_db()
        self.assertEqual((self.place.address, self.place.asked_on, self.place.approved_on), ("New road 1", None, None))

    def test_the_phone_and_the_map_are_the_owners_at_once(self):
        ShopPlace.objects.create(shop=self.shop, address="Old road 5", city="Northtown", latitude=45.9, longitude=11.0)
        self.place = ShopPlace.objects.get()
        same = {"address": "Old road 5", "city": "Northtown", "latitude": "45.9", "longitude": "11.0"}
        self.change(**same, phone="0461 000000")
        self.place.refresh_from_db()
        self.assertEqual((self.place.phone, self.place.waits), ("0461 000000", False))
        self.assertEqual(self.listed()["Driving school"]["places"][0]["phone"], "0461 000000")
        self.assertContains(self.client.get(f"/en/shops/s/{self.shop.id}/"), 'href="tel:0461000000"')
        # Shown at once with no approval: a number, not a line of text
        for words in ("call me maybe", "pay@example.org", "https://x.example"):
            self.assertEqual(self.change(**same, phone=words).status_code, 400, words)
        self.assertEqual(self.change(**same, phone="+39 (0461) 00.00-00/1").status_code, 302)
        self.change(**same, phone="0461 000000")
        # Off the map: nowhere for the public, still the owner's, and nobody is asked
        self.change(**same, on_map="")
        self.place.refresh_from_db()
        self.assertEqual((self.place.on_map, self.place.waits, self.place.shown), (False, False, False))
        self.assertNotIn("places", self.listed()["Driving school"])
        self.assertNotContains(self.client.get(f"/en/shops/s/{self.shop.id}/"), "Old road 5")
        self.assertContains(self.client.get(self.page), "Not on the map: you chose so.")
        self.change(**same)
        self.assertEqual(len(self.listed()["Driving school"]["places"]), 1)

    def test_a_new_place_starts_on_the_map_when_the_shop_is_in_the_lists(self):
        self.assertContains(self.client.get(self.page), 'id="id_on_map" checked')
        Shop.objects.filter(pk=self.shop.pk).update(listed=False)
        self.assertNotContains(self.client.get(self.page), 'id="id_on_map" checked')

    def test_nobody_approves_what_they_have_not_read(self):
        """The owner changes a place, or adds one, after the page of the day was opened: the click approves nothing."""
        self.add()
        self.staff("view_shop", "change_shop")
        self.assertContains(self.client.get("/admin/today/"), f'name="seen" value="{self.seen()}"')
        seen = self.seen()
        self.place = ShopPlace.objects.get()
        self.client.force_login(self.owner)
        self.change(address="Elsewhere 1", city="Faraway", latitude="10", longitude="10")
        self.add(address="Second unseen", city="Faraway")
        self.staff_again = get_user_model().objects.get(username="staff")
        self.client.force_login(self.staff_again)
        session = self.client.session
        from config.urls import ADMIN_SESSION

        session[ADMIN_SESSION] = True
        session.save()
        for sent in ({"seen": seen}, {}, {"seen": ""}):
            response = self.client.post(f"/admin/today/approve-places/{self.shop.id}/", sent, follow=True)
            self.assertContains(response, "changed its places while you were reading")
            self.assertFalse(ShopPlace.objects.filter(approved_on__isnull=False).exists())
        self.client.post(f"/admin/today/approve-places/{self.shop.id}/", {"seen": self.seen()})
        self.assertEqual(ShopPlace.objects.filter(approved_on__isnull=False).count(), 2)

    def test_a_point_moved_under_the_same_words_is_said(self):
        ShopPlace.objects.create(shop=self.shop, address="Old road 5", city="Northtown", latitude=45.9, longitude=11.0)
        self.place = ShopPlace.objects.get()
        self.change(address="Old road 5", city="Northtown", latitude="-33", longitude="151")
        self.staff("view_shop", "change_shop")
        self.assertContains(
            self.client.get("/admin/today/"),
            "<li>new address Old road 5, Northtown (-33.00000, 151.00000) "
            "(it was Old road 5, Northtown (45.90000, 11.00000))</li>",
        )

    def test_the_line_of_a_new_shop_names_the_places_approved_with_it(self):
        Shop.objects.filter(pk=self.shop.pk).update(approved_on=None)
        self.add()
        self.staff("view_shop", "change_shop")
        self.assertContains(
            self.client.get("/admin/today/"),
            "<li>new place Side road 90, Southvillage (45.91386, 11.03260)</li>",
        )

    def test_only_the_phone_changed_asks_nobody_whatever_wrote_the_address(self):
        """An address brought with a space at its end, or an invisible character: the form would save it without, and
        that is not a new address."""
        ShopPlace.objects.create(
            shop=self.shop, address="Old road 5 ", city="North\u200btown", latitude=45.9, longitude=11.0
        )
        self.place = ShopPlace.objects.get()
        self.change(address="Old road 5", city="Northtown", latitude="45.9", longitude="11.0", phone="0461 000000")
        self.place.refresh_from_db()
        self.assertEqual(
            (self.place.waits, self.place.phone, self.place.address), (False, "0461 000000", "Old road 5 ")
        )

    def test_an_approval_that_arrives_while_the_owner_saves_is_kept(self):
        """The owner's form was built on a place that waited; someone approves it; the owner saves another address: the
        approved one stays public and the new one waits, it does not go on the map unread."""
        self.add()
        place = ShopPlace.objects.get()
        stale = ShopPlace.objects.get()
        approved = places.for_form(stale)
        self.approve()
        stale.address = "Never read 1"
        self.assertEqual(places.write(stale, self.shop, approved), "waits")
        place.refresh_from_db()
        self.assertEqual((place.address, place.asked_address, place.shown), ("Side road 90", "Never read 1", True))
        # And the other way: the form showed the address that waited, it is approved meanwhile, and the owner saves
        # only a phone: the address just approved is not asked again, nor put back to the one before
        stale = ShopPlace.objects.get()
        approved = places.for_form(stale)
        self.approve()
        stale.phone = "0461 000000"
        places.write(stale, self.shop, approved)
        place.refresh_from_db()
        self.assertEqual((place.address, place.asked_address, place.waits), ("Never read 1", "", False))
        self.assertEqual(place.phone, "0461 000000")

    def test_an_address_the_staff_corrects_in_the_admin_drops_the_owners_request(self):
        ShopPlace.objects.create(shop=self.shop, address="Old road 5", city="Northtown", latitude=45.9, longitude=11.0)
        self.place = ShopPlace.objects.get()
        self.change()
        staff = self.staff("view_shop", "change_shop")
        staff.is_superuser = True
        staff.save()
        url = f"/admin/shops/shop/{self.shop.pk}/change/"
        page = self.client.get(url)
        self.assertContains(page, "approved; a new address waits: New road 1, Southvillage (45.95000, 11.05000)")
        data = {**page.context["adminform"].form.initial, "_save": "1"}
        data = {key: ("" if value is None else value) for key, value in data.items()}
        row = {"id": self.place.pk, "shop": self.shop.pk, "city": "Northtown", "latitude": "45.9", "longitude": "11.0"}

        def post(**fields):
            sent = {f"places-0-{key}": value for key, value in {**row, "on_map": "on", **fields}.items()}
            counts = {"places-TOTAL_FORMS": "1", "places-INITIAL_FORMS": "1"}
            response = self.client.post(
                url, {**data, **sent, **counts, "closures-TOTAL_FORMS": "0", "closures-INITIAL_FORMS": "0"}
            )
            self.assertEqual(response.status_code, 302, getattr(response, "context", None))
            self.place.refresh_from_db()

        # Saved as it is, or with the phone alone: the request still waits
        post(address="Old road 5", phone="0461 000000")
        self.assertEqual((self.place.asked_address, self.place.phone), ("New road 1", "0461 000000"))
        post(address="Old road 5/A")
        self.assertEqual((self.place.address, self.place.asked, self.place.waits), ("Old road 5/A", "", False))

    def test_a_key_of_the_team_is_not_asked_about_places(self):
        from team.area import waiting_items

        self.add()
        self.assertEqual(waiting_items(), [])

    def test_an_address_that_waits_is_only_for_whoever_approves(self):
        """The team's area shows the day's lines to every key (`what`, `do`, `say`): the addresses of the places that
        wait are not in them, for a place of an approved shop nor for those of a shop that waits."""
        from team.area import area_items, waiting_items

        self.add()
        other = Shop.objects.create(name="Bakery", owner=self.owner, latitude=45.9, longitude=11.0, approved_on=None)
        ShopPlace.objects.create(
            shop=other, address="Hidden lane 4", city="Eastham", latitude=1, longitude=1, approved_on=None
        )
        items = [item for item, _ in area_items()[1]]
        self.assertEqual({item.code for item in items} >= {"approve", "approve_places"}, True)
        for item in items:
            for text in (item.what, item.do, item.say, item.wrote):
                self.assertNotIn("Side road", str(text))
                self.assertNotIn("Hidden lane", str(text))
        # A key that approves shops reads the places that go with the shop it approves
        [waiting] = waiting_items()
        self.assertEqual(waiting.asks, ["new place Hidden lane 4, Eastham (1.00000, 1.00000)"])


class PlaceTrash(TestCase):
    """A place its owner takes away is hidden at once and kept for a month: taken back, it is as it was."""

    setUp = Places.setUp
    add = Places.add
    approve = Places.approve
    listed = Places.listed

    def test_taken_away_it_is_hidden_and_taken_back_it_needs_no_new_approval(self):
        self.add()
        self.approve()
        place = ShopPlace.objects.get()
        self.client.post(self.page, {"remove": place.pk})
        place.refresh_from_db()
        self.assertFalse(place.shown)
        self.assertNotIn("places", self.listed()["Driving school"])
        page = self.client.get(self.page)
        self.assertContains(page, "Places you took away")
        self.assertContains(page, f'name="restore" value="{place.pk}"')
        self.client.post(self.page, {"restore": place.pk})
        place.refresh_from_db()
        self.assertEqual((place.removed_on, place.waits, place.shown), (None, False, True))
        self.assertEqual(len(self.listed()["Driving school"]["places"]), 1)

    def test_a_place_that_waited_still_waits_when_taken_back(self):
        self.add()
        place = ShopPlace.objects.get()
        self.client.post(self.page, {"remove": place.pk})
        from . import dashboard

        self.assertEqual(dashboard.place_items(), [])  # nobody is asked about a place taken away
        self.client.post(self.page, {"restore": place.pk})
        place.refresh_from_db()
        self.assertTrue(place.waits)

    def test_the_page_of_the_day_empties_the_trash_of_every_shop(self):
        from datetime import timedelta

        from . import dashboard

        old = ShopPlace.objects.create(
            shop=self.shop, city="X", latitude=1, longitude=1, removed_on=timezone.now() - timedelta(days=31)
        )
        fresh = ShopPlace.objects.create(shop=self.shop, city="Y", latitude=1, longitude=1, removed_on=timezone.now())
        dashboard.place_items()
        self.assertEqual(list(ShopPlace.objects.values_list("pk", flat=True)), [fresh.pk])
        self.assertFalse(ShopPlace.objects.filter(pk=old.pk).exists())

    def test_after_a_month_it_is_deleted(self):
        from datetime import timedelta

        self.add()
        place = ShopPlace.objects.get()
        self.client.post(self.page, {"remove": place.pk})
        ShopPlace.objects.update(removed_on=timezone.now() - timedelta(days=ShopPlace.TRASH_DAYS - 1))
        self.assertContains(self.client.get(self.page), f'name="restore" value="{place.pk}"')
        ShopPlace.objects.update(removed_on=timezone.now() - timedelta(days=ShopPlace.TRASH_DAYS, minutes=1))
        self.client.post(self.page, {"restore": place.pk})
        self.assertNotContains(self.client.get(self.page), "Places you took away")
        self.assertFalse(ShopPlace.objects.exists())

    def test_the_trash_does_not_count_and_a_full_shop_takes_nothing_back(self):
        self.add()
        place = ShopPlace.objects.get()
        self.client.post(self.page, {"remove": place.pk})
        ShopPlace.objects.bulk_create([ShopPlace(shop=self.shop, city="X", latitude=1, longitude=1)] * ShopPlace.MAX)
        self.assertContains(self.client.post(self.page, {"restore": place.pk}, follow=True), "cannot be taken back")
        place.refresh_from_db()
        self.assertIsNotNone(place.removed_on)

    def test_only_the_owners_own_place_moves(self):
        other = get_user_model().objects.create(username="email:" + "b" * 32)
        theirs = ShopPlace.objects.create(
            shop=Shop.objects.create(name="Other", owner=other),
            city="Far",
            latitude=1,
            longitude=1,
            removed_on=timezone.now(),
        )
        for key in (theirs.pk, "x", "²", "9" * 30):
            self.client.post(self.page, {"restore": key})
        theirs.refresh_from_db()
        self.assertIsNotNone(theirs.removed_on)

    def test_a_staff_register_is_not_offered_a_place_taken_away(self):
        from .staff import StaffRegisterForm

        place = ShopPlace.objects.create(shop=self.shop, city="Southvillage", latitude=45.9, longitude=11.0)
        self.assertIn("place", StaffRegisterForm(shop=self.shop).fields)
        places.remove(self.shop, place.pk)
        self.assertNotIn("place", StaffRegisterForm(shop=self.shop).fields)
        # But a register used there keeps it through a rename: the place may come back
        from .models import StaffRegister

        register = StaffRegister.objects.create(shop=self.shop, name="Anna", place=place)
        form = StaffRegisterForm({"name": "Anna B", "place": place.pk}, instance=register, shop=self.shop)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().place, place)

    def test_the_public_list_is_built_again_when_a_place_is_taken_away_or_back(self):
        from . import public_cache

        place = ShopPlace.objects.create(shop=self.shop, city="Southvillage", latitude=45.9, longitude=11.0)
        for move in (places.remove, places.restore):
            before = public_cache.generation()
            with self.captureOnCommitCallbacks(execute=True):
                self.assertTrue(move(self.shop, place.pk))
            self.assertNotEqual(public_cache.generation(), before, move.__name__)
