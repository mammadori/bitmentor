"""The register on a wide screen (a PC or a tablet on the counter): the day beside the keypad, the amount typed on the
keyboard, the keys of the other screens, the history and the settings beside the keypad (static/register.css,
static/register.js, templates/shops/register.html)."""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

ROOT = Path(settings.BASE_DIR)
CSS = (ROOT / "static" / "register.css").read_text()
JS = (ROOT / "static" / "register.js").read_text()
PAGE = (ROOT / "templates" / "shops" / "register.html").read_text()
WIDE = "screen and (min-width: 900px) and (min-height: 560px)"


class WideRegister(SimpleTestCase):
    def test_the_script_and_the_styles_agree_on_what_is_wide(self):
        self.assertIn(f"@media {WIDE} {{", CSS)
        self.assertIn(f'matchMedia("{WIDE}")', JS)

    def test_the_wide_layout_leaves_the_practice_register_alone(self):
        block = CSS[CSS.index(f"@media {WIDE} {{") :]
        rules = re.findall(r"^  ([^{}/\n][^{}]*)\{", block, flags=re.MULTILINE)
        self.assertGreater(len(rules), 30)
        for rule in rules:
            self.assertTrue(rule.startswith((".register:not(.practising)", ".today")), rule)

    def test_the_day_and_the_keyboard_line_show_only_on_a_wide_screen(self):
        self.assertIn(".today, .keys-line { display: none; }", CSS.split("@media " + WIDE)[0])
        self.assertIn('<aside id="today" class="today no-print"', PAGE)

    def test_the_day_is_not_next_to_the_code_the_customer_scans(self):
        # The panel has a place only in the page of the keypad
        for rule in re.findall(r"^  ([^{}\n]*\.today[^{}]*)\{[^}]*display: flex", CSS, flags=re.MULTILINE):
            self.assertIn("main:has(#keypad:not([hidden]))", rule)

    def test_a_staff_register_has_less_beside_the_keypad(self):
        # No totals of the day, no choice on the keys: the page does not give them, and the script does not look for them
        self.assertIn('{% if not staff %}<ul id="today-totals" class="day-totals"></ul>{% endif %}', PAGE)
        self.assertIn('{% if not staff %} <button type="button" id="keys-toggle"', PAGE)
        self.assertIn("const choice = staff ? null : load(KEYS, null);", JS)
        self.assertIn('if (!staff) $("today-totals")?.replaceChildren(...lines);', JS)

    def test_every_key_shown_on_a_button_is_announced_too(self):
        shown = re.findall(r"<button[^>]*data-key=\"[^\"]+\"[^>]*>", PAGE)
        self.assertEqual(len(shown), 6)
        for button in shown:
            self.assertIn("aria-keyshortcuts=", button)

    def test_escape_cancels_through_the_button_that_asks_first(self):
        # Esc presses Cancel, whose click asks before cancelling (data-cancel-warning): never a path of its own
        self.assertIn('if (event.key === "Escape" && shown === "pay") tap("cancel");', JS)
        self.assertIn(
            '$("cancel").addEventListener("click", async () => {\n    if (!confirm(text.cancelWarning)) return;', JS
        )

    def test_what_is_typed_in_a_field_beside_the_keypad_is_not_an_amount(self):
        keypad = JS[JS.index('if ($("keypad").hidden || menu.open') :]
        self.assertLess(
            keypad.index('closest?.("input, select, textarea, #history button'),
            keypad.index("press(Number(event.key))"),
        )
