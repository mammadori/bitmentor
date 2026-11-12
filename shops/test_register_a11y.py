"""The register's pages for whoever uses them with a screen reader, a keyboard or a finger: each field has its label,
each button a name, the status lines are live regions, the screens have where focus goes, and the language is the
page's. The register itself, in node, in test_register_a11y_flow.py."""
from html.parser import HTMLParser

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import Shop, StaffRegister
from .test_staff import staff_page
from .tests import reset_limits

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class Element:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.children, self.text = tag, dict(attrs), parent, [], ""

    def ancestors(self):
        node = self.parent
        while node:
            yield node
            node = node.parent

    def all_text(self):
        return self.text + "".join(child.all_text() for child in self.children)


class Page(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.root = self.current = Element("#root", {}, None)
        self.elements = []
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        element = Element(tag, attrs, self.current)
        self.current.children.append(element)
        self.elements.append(element)
        if tag not in VOID:
            self.current = element

    def handle_endtag(self, tag):
        node = self.current
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.current = node.parent

    def handle_data(self, data):
        self.current.text += data

    def by_id(self, ident):
        return next((element for element in self.elements if element.attrs.get("id") == ident), None)

    def all(self, tag):
        return [element for element in self.elements if element.tag == tag]


class Accessible(TestCase):
    def setUp(self):
        reset_limits()
        self.owner = get_user_model().objects.create(username="owner")
        self.shop = Shop.objects.create(owner=self.owner, name="Bar", lightning_address="till@ln.example.org")
        self.client.force_login(self.owner)

    def page(self, url, client=None):
        response = (client or self.client).get(url)
        self.assertEqual(response.status_code, 200, url)
        return Page(response.content.decode())

    def staff(self):
        name = f"Anna {StaffRegister.objects.filter(shop=self.shop).count()}"
        self.client.post(f"/en/shops/registers/{self.shop.id}/", {"name": name})
        register = StaffRegister.objects.get(shop=self.shop, name=name)
        phone = self.client_class()
        phone.post("/en/shops/register/open/", {"code": register.code})
        register.refresh_from_db()
        return register, phone

    def registers(self):
        register, phone = self.staff()
        return {"owner": self.page(f"/en/shops/register/{self.shop.id}/"), "staff": self.page(staff_page(register), phone),
                "practice": self.page(f"/en/shops/practice/{self.shop.id}/")}

    def assert_fields_and_buttons_named(self, page, where):
        labelled = {element.attrs.get("for") for element in page.all("label")}
        for field in page.all("input") + page.all("select") + page.all("textarea"):
            if field.attrs.get("type") == "hidden":
                continue
            named = (field.attrs.get("id") in labelled or field.attrs.get("aria-label")
                     or any(node.tag == "label" for node in field.ancestors()))
            self.assertTrue(named, f"{where}: a field without a label: {field.attrs}")
        for button in page.all("button"):
            self.assertTrue(button.all_text().strip() or button.attrs.get("aria-label")
                            # Filled by the script before it is shown
                            or "hidden" in button.attrs or button.attrs.get("id") in {"discount"},
                            f"{where}: a button without a name: {button.attrs}")
        # Nothing clickable that is not a control
        for element in page.elements:
            if element.tag in {"div", "span", "p", "li"}:
                self.assertNotEqual(element.attrs.get("role"), "button", f"{where}: {element.attrs}")
                self.assertEqual(element.attrs.get("tabindex", "-1"), "-1", f"{where}: {element.attrs}")

    def test_every_field_has_a_label_and_every_button_a_name(self):
        for where, page in self.registers().items():
            self.assert_fields_and_buttons_named(page, where)

    def test_the_status_lines_are_live_and_the_screens_have_where_focus_goes(self):
        for where, page in self.registers().items():
            self.assertEqual(page.by_id("state").attrs.get("role"), "status", where)
            self.assertEqual(page.by_id("error").attrs.get("role"), "alert", where)
            self.assertEqual(page.by_id("history-say").attrs.get("role"), "status", where)
            self.assertEqual(page.by_id("amount").tag, "output", where)
            # Where static/register.js (FOCUS) puts the focus on each screen: reachable by the script, not by Tab
            for ident in ("amount", "step-text", "question", "qr", "history-title"):
                self.assertEqual(page.by_id(ident).attrs.get("tabindex"), "-1", f"{where}: {ident}")
            self.assertEqual(page.by_id("again").tag, "button", where)
            self.assertEqual(page.by_id("result").tag, "h1", where)
            self.assertEqual(page.all("html")[0].attrs.get("lang"), "en", where)
        owner = self.registers()["owner"]
        self.assertEqual(owner.by_id("settings-title").attrs.get("tabindex"), "-1")
        self.assertEqual(owner.by_id("settings-say").attrs.get("role"), "status")
        # The fieldset an error points to can take the focus
        self.assertEqual(owner.by_id("sources").attrs.get("tabindex"), "-1")

    def test_the_qr_code_has_a_text_alternative_and_the_request_can_be_copied(self):
        for where, page in self.registers().items():
            qr = page.by_id("qr")
            self.assertTrue(qr.attrs.get("alt"), where)
            # The invoice or address as text, inside the disclosure, with a button to copy it
            invoice = page.by_id("invoice")
            self.assertTrue(any(node.tag == "details" for node in invoice.ancestors()), where)
            copy = page.by_id("copy-invoice")
            self.assertEqual((copy.tag, copy.attrs.get("type")), ("button", "button"), where)
            self.assertEqual(copy.all_text().strip(), "Copy the text", where)
            self.assertEqual(copy.attrs.get("data-copied"), "Copied", where)
        # The decorative tick says nothing: the heading says how the sale ended
        self.assertEqual(self.registers()["owner"].by_id("done").children[0].attrs.get("aria-hidden"), "true")

    def test_the_headings_go_in_order(self):
        for where, page in self.registers().items():
            for section in page.all("section"):
                if "register-screen" not in section.attrs.get("class", ""):
                    continue
                levels = [int(node.tag[1]) for node in section_headings(section)]
                if levels:
                    self.assertEqual(levels[0], 1, f"{where}: {section.attrs.get('id')}")
                    for before, after in zip(levels, levels[1:]):
                        self.assertLessEqual(after - before, 1, f"{where}: {section.attrs.get('id')}")

    def test_the_language_is_the_page_s(self):
        for lang in [code for code, _name in settings.LANGUAGES if code != "en"]:
            page = self.page(f"/{lang}/shops/register/{self.shop.id}/")
            self.assertEqual(page.all("html")[0].attrs.get("lang"), lang)
            self.assertNotEqual(page.by_id("copy-invoice").all_text().strip(), "Copy the text", lang)

    def test_the_staff_pages_name_their_controls(self):
        register, phone = self.staff()
        self.client.post(f"/en/shops/registers/{self.shop.id}/{register.id}/")
        code = self.page(f"/en/shops/registers/{self.shop.id}/{register.id}/")
        self.assert_fields_and_buttons_named(code, "code")
        self.assertTrue(code.by_id("pair-qr").attrs.get("alt"))
        self.assertEqual(code.by_id("pair-error").attrs.get("role"), "alert")
        self.assertEqual(code.by_id("pair-say").attrs.get("role"), "status")
        self.assertEqual(code.by_id("pair-copy").all_text().strip(), "Copy the link")
        self.assert_fields_and_buttons_named(self.page(f"/en/shops/registers/{self.shop.id}/"), "registers")


def section_headings(section):
    for child in section.children:
        if child.tag in {"h1", "h2", "h3", "h4"}:
            yield child
        elif child.tag != "section":
            yield from section_headings(child)
