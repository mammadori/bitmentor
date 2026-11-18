"""Every email of the site goes through `send_mail` here: the same plain text as always, and an HTML copy of it with
each web address a real link, so that a mail program that does not make links of the plain text still has one to tap.
A line that is only an address becomes a button. Nothing else in the HTML: no images, no remote content, no tracking,
no styles from outside; the text is escaped, so nothing a merchant wrote can become markup."""
import logging
import re
import threading

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.db import connections
from django.utils.html import escape

logger = logging.getLogger(__name__)
# How many emails may be on their way to the mail server at once, outside the requests that asked for them
# (`send_later`). Each waits EMAIL_TIMEOUT at most; one more than these is sent by its own request, as before
WAITING = threading.BoundedSemaphore(8)

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


def merchant_sender():
    """The sender of the emails to a shop's owner (a reply reaches someone), or None for DEFAULT_FROM_EMAIL."""
    return settings.MERCHANT_FROM_EMAIL or None


# What an email is, for a community's mail backend that sends each kind its own way (message.kind)
KINDS = ("link", "staff", "merchant")


def send_mail(subject, message, from_email, recipient_list, fail_silently=False, connection=None, kind=""):
    """django.core.mail.send_mail with the same arguments and answer (1 if sent), plus the HTML copy and its kind."""
    connection = connection or get_connection(fail_silently=fail_silently)
    shown = from_email or settings.DEFAULT_FROM_EMAIL
    # The envelope (Return-Path) can be another address than the one shown in From, see EMAIL_ENVELOPE_FROM
    envelope = settings.EMAIL_ENVELOPE_FROM or shown
    mail = EmailMultiAlternatives(subject, message, envelope, recipient_list, connection=connection,
                                  headers={"From": shown} if envelope != shown else None)
    mail.attach_alternative(html(message), "text/html")
    mail.kind = kind
    return mail.send()


def _deliver(subject, message, recipient_list, kind, failed):
    """True if the email left. One that did not is an error in the log, never a silent loss: the page has already answered."""
    try:
        left = bool(send_mail(subject, message, None, recipient_list, kind=kind))
        if not left:
            # The email budget kept it back (config/mailbudget.py warned with its reason)
            logger.error("email: a \"%s\" email (%s) was not sent", kind, subject)
    except Exception as error:  # noqa: BLE001 (a thread: whatever breaks must reach the log, not vanish)
        # Only the kind of error and the mail server's code: its text, and a traceback with it, can quote the recipient
        left = False
        code = getattr(error, "smtp_code", None)
        logger.error("email: a \"%s\" email (%s) could not be sent: %s%s", kind, subject, type(error).__name__,
                     f" {code}" if isinstance(code, int) else "")
    if not left and failed:
        try:
            failed()
        except Exception:
            logger.exception("email: what follows an email not sent broke")
    return left


def _behind(*arguments):
    try:
        _deliver(*arguments)
    finally:
        WAITING.release()
        # The thread's own database connections (the email count): nobody else would close them
        connections.close_all()


def send_later(subject, message, recipient_list, kind="", failed=None):
    """Sends without making the page wait for the mail server: the answer is the same whether the email leaves or not. `subject` and `message` are plain text already (the thread does not have the request's
    language). `failed` is called when the email did not leave. Nothing is returned: the caller cannot know.

    Not a daemon thread: a process that is asked to stop waits for the emails on their way. With MAIL_IN_BACKGROUND off
    (the tests), or with too many already waiting, the email is sent here before returning."""
    arguments = (str(subject), str(message), list(recipient_list), kind, failed)
    if settings.MAIL_IN_BACKGROUND and WAITING.acquire(blocking=False):
        try:
            threading.Thread(target=_behind, args=arguments, name="mail").start()
        except RuntimeError:
            # No thread to be had: this request sends it
            WAITING.release()
            _deliver(*arguments)
        return
    _deliver(*arguments)
