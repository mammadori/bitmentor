"""Team access to a site under construction (docs/features/team-access.md): the keys of the team's wallets, the one-time
invites that make them, and the sign-in challenges. None of them is an account: a team key signs in to nothing, owns
nothing and has no right in /admin/ (config/guard.py keeps the staff's accounts away from wallets on purpose). All it does is
give a browser a pass (team/access.py)."""
import hashlib
import logging
import secrets
from datetime import timedelta

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

# An invite is good for a day; its row stays a month more, for the staff to see what became of it
INVITE_LIFETIME = timedelta(hours=24)
INVITE_KEPT = timedelta(days=30)
CHALLENGE_LIFETIME = timedelta(minutes=5)
# Challenges alive at the same time, whoever asks for them: with the limits by address, the table cannot grow past
# these. The sign-in codes, which anybody can ask for, and the codes of the invites' pages, which only whoever holds an
# open invite can, are counted apart: strangers asking for sign-in codes cannot keep an invited person out
CHALLENGES_ALIVE = 5000
INVITE_CHALLENGES_ALIVE = 500
logger = logging.getLogger(__name__)


def token_hash(token):
    """What the database keeps of an invite's token. The token has 256 random bits: a plain SHA-256 is enough, and a
    lookup by it gives nothing away by its timing."""
    return hashlib.sha256(token.encode()).hexdigest()


def new_stamp():
    return secrets.token_hex(16)


class TeamKey(models.Model):
    """The key a team member's wallet made for this site (LNURL-auth's linking key), with the name and the role the
    invite gave it. The role is a label, for now."""

    class Role(models.TextChoices):
        TEAM = "team", _("team")
        STAFF = "staff", _("staff")
        MODERATOR = "moderator", _("moderator")

    linking_key = models.CharField(_("linking key"), max_length=66, unique=True, editable=False)
    name = models.CharField(_("name"), max_length=150)
    role = models.CharField(_("role"), max_length=20, choices=Role.choices, default=Role.TEAM)
    created = models.DateTimeField(_("created"), auto_now_add=True)
    # The last time a browser got a pass from this key
    last_pass = models.DateTimeField(_("last sign-in"), null=True, blank=True, editable=False)
    revoked = models.DateTimeField(_("revoked"), null=True, blank=True, editable=False)
    # Every pass names it (team/access.py): a new one ends the passes given so far, at their next request
    stamp = models.CharField(max_length=32, default=new_stamp, editable=False)

    class Meta:
        ordering = ("name",)
        verbose_name = _("team key")
        verbose_name_plural = _("team keys")

    def __str__(self):
        return f"{self.name} ({self.get_role_display()})"

    @classmethod
    def active(cls):
        return cls.objects.filter(revoked__isnull=True)

    @classmethod
    def revoke(cls, keys):
        """Revokes the keys of the queryset still active: they sign in no more, and their passes stop at the next
        request. Returns how many."""
        return keys.filter(revoked__isnull=True).update(revoked=timezone.now(), stamp=new_stamp())

    @classmethod
    def end_passes(cls, keys):
        """Ends the passes of each key of the queryset without revoking it: every browser has to sign in again."""
        return sum(cls.objects.filter(pk=pk).update(stamp=new_stamp()) for pk in keys.values_list("pk", flat=True))


class TeamInvite(models.Model):
    """A one-time invite: the first wallet that signs it becomes a TeamKey with this name and role. Only the hash of
    its token is kept: the link is shown once, to the admin who makes it."""
    name = models.CharField(_("name"), max_length=150)
    role = models.CharField(_("role"), max_length=20, choices=TeamKey.Role.choices, default=TeamKey.Role.TEAM)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    created = models.DateTimeField(_("created"), auto_now_add=True)
    expires = models.DateTimeField(_("expires"), editable=False)
    used = models.DateTimeField(_("used"), null=True, blank=True, editable=False)
    key = models.ForeignKey(TeamKey, on_delete=models.SET_NULL, null=True, blank=True, editable=False,
                            verbose_name=_("team key"))

    class Meta:
        ordering = ("-created",)
        verbose_name = _("team invite")
        verbose_name_plural = _("team invites")

    def __str__(self):
        return f"{self.name} ({self.get_role_display()})"

    def arm(self):
        """Gives a new invite its token and its day: returns the token, which is kept nowhere. The invites whose day
        passed INVITE_KEPT ago go, used or not, so the table does not grow for ever."""
        token = secrets.token_urlsafe(32)
        self.token_hash = token_hash(token)
        self.expires = timezone.now() + INVITE_LIFETIME
        TeamInvite.objects.filter(expires__lt=timezone.now() - INVITE_KEPT).delete()
        return token

    @classmethod
    def open(cls):
        return cls.objects.filter(used__isnull=True, expires__gt=timezone.now())

    @classmethod
    def by_token(cls, token):
        """The open invite of this token, or None."""
        if not 20 <= len(token) <= 100:
            return None
        return cls.open().filter(token_hash=token_hash(token)).first()

    @property
    def is_open(self):
        return self.used is None and self.expires > timezone.now()


class TeamChallenge(models.Model):
    """A challenge for a wallet to sign (LUD-04): the k1 in the QR code and the key that signed it, for five minutes.
    One made by an invite's page makes the team key; the others only give a pass to a key that is already one.

    The k1 of an invite's page is worth the invite until it is signed: it is not the primary key (Django's admin lets a
    list be filtered through a relation down to the related row's primary key), no admin shows it, and no page asks for
    an address that has it in the path."""
    k1 = models.CharField(max_length=64, unique=True)
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    linking_key = models.CharField(max_length=66, blank=True)
    invite = models.ForeignKey(TeamInvite, on_delete=models.CASCADE, null=True, blank=True)

    @classmethod
    def alive(cls):
        return cls.objects.filter(created__gte=timezone.now() - CHALLENGE_LIFETIME)

    @classmethod
    def new(cls, invite=None):
        """A new challenge, or None when as many as allowed of its kind are waiting already (the log of `web` says so).
        The expired ones go first: nothing else cleans the table."""
        cls.objects.filter(created__lt=timezone.now() - CHALLENGE_LIFETIME).delete()
        most = CHALLENGES_ALIVE if invite is None else INVITE_CHALLENGES_ALIVE
        if cls.objects.filter(invite__isnull=invite is None).count() >= most:
            logger.warning("team access: %d %s alive, the most at one time: no new one until some expire (five minutes)",
                           most, "sign-in codes" if invite is None else "codes of invite pages")
            return None
        return cls.objects.create(k1=secrets.token_hex(32), invite=invite)
