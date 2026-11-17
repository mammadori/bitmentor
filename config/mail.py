"""Every email of the site goes through `send_mail` here: the same plain text as always, and an HTML copy of it with
each web address a real link, so that a mail program that does not make links of the plain text still has one to tap.
A line that is only an address becomes a button. Nothing else in the HTML: no images, no remote content, no tracking,
no styles from outside; the text is escaped, so nothing a merchant wrote can become markup."""
import re

from django.core.mail import EmailMultiAlternatives, get_connection
from django.utils.html import escape

URL = re.compile(r"https?://[^\s<>\"']+")
# What ends a sentence is not part of the address: «…/claim/?code=abc.» links to «…/claim/?code=abc»
TRAILING = ".,;:!?)»”’]"
BUTTON = ("display:inline-block;padding:12px 20px;border-radius:6px;background:#224455;color:#ffffff;"
          "text-decoration:none;font-weight:bold")


def split(url):
    end = len(url)
    # A closing parenthesis is the address's own when it closes one opened in it: …/Bitcoin_(money)
    while end and url[end - 1] in TRAILING and not (url[end - 1] == ")" and url[:end].count("(") >= url[:end].count(")")):
        end -= 1
    return url[:end], url[end:]


def linked(line):
    """One line of text as HTML: escaped, its addresses as links."""
    out, last = [], 0
    for found in URL.finditer(line):
        url, rest = split(found.group())
        out += [escape(line[last:found.start()]), f'<a href="{escape(url)}">{escape(url)}</a>', escape(rest)]
        last = found.end()
    out.append(escape(line[last:]))
    return "".join(out)


def html(text):
    """The plain text as a small HTML page: paragraphs, line breaks, links, and a button for an address alone on its line."""
    paragraphs = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = []
        for line in block.split("\n"):
            alone = URL.fullmatch(line.strip())
            if alone and not split(alone.group())[1]:
                url = escape(alone.group())
                lines.append(f'<a href="{url}" style="{BUTTON}">{url}</a>')
            else:
                lines.append(linked(line))
        paragraphs.append("<p>" + "<br>\n".join(lines) + "</p>")
    return ('<!doctype html><html><head><meta charset="utf-8"></head>'
            '<body style="font-family:sans-serif;line-height:1.5">\n' + "\n".join(paragraphs) + "\n</body></html>")


def send_mail(subject, message, from_email, recipient_list, fail_silently=False, connection=None):
    """django.core.mail.send_mail with the same arguments and answer (1 if sent), plus the HTML copy."""
    connection = connection or get_connection(fail_silently=fail_silently)
    mail = EmailMultiAlternatives(subject, message, from_email, recipient_list, connection=connection)
    mail.attach_alternative(html(message), "text/html")
    return mail.send()
