"""Values of the admin that nobody may read, not even by asking for them in the address of a list.

Django's admin lets a list be filtered by the address on any field of the model (`?claim_hash__startswith=ab`), whatever
the page shows: it is how a link of a filter works. A value kept off the page and out of the search (the
claim of a shop) can then be read one character at a time,
by anyone who may see the list: the answer to each guess is one row or none.

`HidesSecrets` says no to a lookup on a field that the admin does not show, whatever follows the name of the field
(`__startswith`, `__regex`, `__in`, none), and Django answers 400. A field is shown when a page, a column, a filter, the
search or the date of the list names it. So the rule needs no list to keep: a field added tomorrow is hidden until the
admin shows it. The primary key is never hidden (it is in every address). `secret_fields` adds the ones that are shown
and must still not be asked for, if there is ever one.

Lookups through another model (`?shop__claim_hash__startswith=`) Django refuses itself, unless the list has a filter
on that path, with one exception: a lookup that ends on the related row's primary key is let through, also across a
reverse relation, which no admin names (`?teamchallenge__k1__startswith=` read the challenge of a team invite, when
that was its primary key). So the rule here is the other way round: a lookup is refused unless its first part is
something this admin shows, or the primary key. A reverse relation is never one of them. And no model of the site that
another one points to, or that points to another, keeps a secret as its primary key (config/test_admin_secrets.py)."""
from django.db.models.constants import LOOKUP_SEP


def names(entries):
    """The model fields that a list of admin entries names: strings (`^name`, `=name`, `name__lookup`), or filters."""
    found = set()
    for entry in entries:
        if isinstance(entry, (tuple, list)):
            found |= names(entry)
        elif isinstance(entry, str):
            found.add(entry.lstrip("^=@").split(LOOKUP_SEP)[0])
        elif getattr(entry, "parameter_name", None):
            found.add(entry.parameter_name)
    return found


class HidesSecrets:
    secret_fields = ()

    def shown_fields(self):
        """The fields of the model that this admin shows, by name, from its class (no request needed)."""
        model_fields = [field for field in self.model._meta.concrete_fields if field.editable]
        form = names(self.fields) if self.fields else {field.name for field in model_fields} - set(self.exclude or ())
        shown = form | names(self.readonly_fields) | names(self.list_display) | names(self.list_filter)
        shown |= names(self.search_fields) | names([self.date_hierarchy] if self.date_hierarchy else [])
        return shown - set(self.secret_fields)

    def hidden_fields(self):
        """What the model has and this admin does not show: not to be asked for."""
        pk = self.model._meta.pk.name
        return {field.name for field in self.model._meta.concrete_fields if field.name != pk} - self.shown_fields()

    def askable(self):
        """What an address may filter the list by: what the admin shows (fields, columns, filters, the search, the
        date) and the primary key. Anything else is refused: a hidden field, and a relation the admin does not name,
        reverse ones included."""
        return (self.shown_fields() | {self.model._meta.pk.name, "pk"}) - self.hidden_fields()

    def lookup_allowed(self, lookup, value, request=None):
        if lookup.split(LOOKUP_SEP)[0] not in self.askable():
            return False
        return super().lookup_allowed(lookup, value, request)
