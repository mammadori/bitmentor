"""The blocks a community's template extends instead of copying the page (docs/architecture.md, "The community's own
code"): with nobody extending them the pages are byte for byte what they were, and an extension changes only its part."""
import re
import tempfile
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.template import engines
from django.template.loader import get_template
from django.test import RequestFactory, TestCase, override_settings

from .models import Shop

# Each page, the blocks it offers, and a context for each of its states
PAGES = {
    "shops/claim.html": (("claim_busy", "claim_gone", "claim_form", "claim_taken"),
                         [{"busy": True}, {"gone": True}, {"code": "c" * 32}, {"code": "c" * 32, "taken": "Taken & <b>"}]),
    "shops/register_locked.html": (("locked_owner", "locked_staff", "locked_others"),
                                   [{"owner": True}, {"staff": True}, {}]),
}
BLOCK = re.compile(r"\{% (?:block (\w+)|endblock) %\}")
CSRF = re.compile(r'name="csrfmiddlewaretoken" value="[^"]*"')


def source(name):
    return (Path(settings.BASE_DIR) / "templates" / name).read_text()


def without_blocks(text, names):
    """The template as it was before these blocks: their tags taken away, the content left where it is. Every
    {% endblock %} after the first line that opens the content is matched with the block it closes."""
    out, stack, position = [], [], 0
    for match in BLOCK.finditer(text):
        out.append(text[position:match.start()])
        position = match.end()
        if match[1] is not None:
            stack.append(match[1])
            if match[1] not in names:
                out.append(match[0])
        elif stack.pop() not in names:
            out.append(match[0])
    out.append(text[position:])
    return "".join(out)


class Blocks(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="Bar & Co", lightning_address="bar@example.org")
        self.request = RequestFactory().get("/en/")
        self.request.user = AnonymousUser()

    def render(self, template, context):
        return CSRF.sub("csrf", template.render({"shop": self.shop, **context}, self.request))

    def test_every_page_is_the_same_byte_for_byte_when_nobody_extends_it(self):
        engine = engines["django"]
        for name, (blocks, states) in PAGES.items():
            text = source(name)
            for block in blocks:
                self.assertIn(f"{{% block {block} %}}", text, name)
            before = engine.from_string(without_blocks(text, blocks))
            self.assertNotIn("{% block claim_", without_blocks(text, blocks))
            self.assertNotIn("{% block locked_", without_blocks(text, blocks))
            for context in states:
                with self.subTest(name, **{key: str(value) for key, value in context.items()}):
                    self.assertEqual(self.render(get_template(name), context), self.render(before, context))

    def test_a_community_changes_only_its_block(self):
        child = engines["django"].from_string(
            '{% extends "shops/claim.html" %}{% block claim_gone %}<p>Ask at the desk.</p>{% endblock %}')
        page = self.render(child, {"gone": True})
        self.assertIn("<p>Ask at the desk.</p>", page)
        self.assertNotIn("This link does not work", page)
        # The other states are the software's
        self.assertEqual(self.render(child, {"busy": True}), self.render(get_template("shops/claim.html"), {"busy": True}))
        child = engines["django"].from_string(
            '{% extends "shops/register_locked.html" %}{% block locked_staff %}<p>Call the owner.</p>{% endblock %}')
        self.assertIn("<p>Call the owner.</p>", self.render(child, {"staff": True}))
        self.assertNotIn("Call the owner", self.render(child, {"owner": True}))

    def test_a_template_of_the_same_name_in_the_communitys_folder_extends_the_softwares(self):
        """As docs/architecture.md shows it: COMMUNITY_DIR/templates/shops/claim.html extends "shops/claim.html"."""
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "shops").mkdir()
            (Path(folder) / "shops" / "claim.html").write_text(
                '{% extends "shops/claim.html" %}{% block claim_gone %}<p>Ask at our desk.</p>{% endblock %}')
            templates = [{**settings.TEMPLATES[0], "DIRS": [folder, *settings.TEMPLATES[0]["DIRS"][1:]]}]
            with override_settings(TEMPLATES=templates):
                self.client.force_login(get_user_model().objects.create(username="k"))
                page = self.client.get("/en/shops/claim/", {"code": "B" * 32})
        self.assertContains(page, "<p>Ask at our desk.</p>", status_code=404)
        self.assertNotContains(page, "This link does not work", status_code=404)
        self.assertContains(page, '<nav class="crumbs">', status_code=404)
