"""Switches for the optional features of a community: `features:` in `community.yml`.

A community that writes nothing sees no change: every feature is on. Writing `false` turns one off, and turning it on
again brings everything back, because nothing here deletes data: the rows stay in the database, only the pages, the
buttons, the notices and the commands of the feature go quiet. What each switch takes away is in docs/features/README.md
("Turning features off") and next to the block in examples/community/community.yml.

    features:
      certified: false         # the badge of the certified shops
      vacation: false          # shops closed for a while
      practice: false          # the practice register

Experimental features are the other way round: `experimental:` in `community.yml`, all off unless the community writes
`true`. They work and are tested, but had less review and less real use, and may change or go: not what a community
starts with (docs/features/README.md, "Experimental features"). Off, one is as quiet as a feature turned off; on, it is
the whole feature.

    experimental:
      onchain: true            # on-chain payments at the register
      osm_upload: true         # writing to OpenStreetMap with `manage.py osm_upload`

Code asks `enabled("name")`; a page that must not exist when its feature is off is wrapped with `gated` in the URLs
(a 404 for everybody, signed in or not, before any other check); templates read `features.<name>` and
`experimental.<name>`."""
from functools import wraps

from django.http import Http404

from config.community import community

CERTIFIED, VACATION, PRACTICE = "certified", "vacation", "practice"
# Every switch, with the value a community that writes nothing gets
FEATURES = {CERTIFIED: True, VACATION: True, PRACTICE: True}
ONCHAIN, OSM_UPLOAD = "onchain", "osm_upload"
# Every experimental feature: off for a community that writes nothing
EXPERIMENTAL = {ONCHAIN: False, OSM_UPLOAD: False}


def block():
    data = community().get("features")
    return data if isinstance(data, dict) else {}


def experimental_block():
    data = community().get("experimental")
    return data if isinstance(data, dict) else {}


def enabled(name):
    """True unless `community.yml` says `false` for this feature. A value that is not true or false (a typo, a text)
    counts as the default, on, and `manage.py check` says so: a feature never disappears by mistake. An experimental
    feature is the other way round: on only when `experimental` says `true`, and anything else leaves it off, so that
    one never appears by mistake."""
    if name in EXPERIMENTAL:
        return experimental_block().get(name) is True
    if name not in FEATURES:
        raise KeyError(name)
    value = block().get(name, FEATURES[name])
    return value if isinstance(value, bool) else FEATURES[name]


def states():
    """{name: bool} for the templates (`features.practice`)."""
    return {name: enabled(name) for name in FEATURES}


def switched_off():
    return [name for name in FEATURES if not enabled(name)]


def experimental_states():
    """{name: bool} for the templates (`experimental.onchain`)."""
    return {name: enabled(name) for name in EXPERIMENTAL}


def experimental_on():
    return [name for name in EXPERIMENTAL if enabled(name)]


def problems():
    """What is wrong in the `features` block, for `manage.py check`. Nothing here stops the site: a wrong value counts as
    the default (on), an unknown name does nothing."""
    found = []
    data = community().get("features")
    if data is None:
        return found
    if not isinstance(data, dict):
        return [f"`features` is a block with the names {', '.join(FEATURES)}, each true or false: {data!r}; "
                "every feature stays on"]
    for name, value in data.items():
        if name not in FEATURES:
            found.append(f"`features.{name}` is not a feature of the software ({', '.join(FEATURES)}): it does nothing")
        elif not isinstance(value, bool):
            found.append(f"`features.{name}` must be true or false, not {value!r}: it stays on")
    return found


def experimental_problems():
    """What is wrong in the `experimental` block, for `manage.py check`. Nothing here stops the site: a wrong value counts
    as the default (off), an unknown name does nothing."""
    data = community().get("experimental")
    if data is None:
        return []
    if not isinstance(data, dict):
        problem = (f"`experimental` is a block with the names {', '.join(EXPERIMENTAL)}, each true or false: {data!r}; "
                   "every experimental feature stays off")
        return [problem]
    found = []
    for name, value in data.items():
        if name not in EXPERIMENTAL:
            found.append(f"`experimental.{name}` is not an experimental feature of the software "
                         f"({', '.join(EXPERIMENTAL)}): it does nothing")
        elif not isinstance(value, bool):
            found.append(f"`experimental.{name}` must be true or false, not {value!r}: it stays off")
    return found


def gated(name, view):
    """`view` as long as the feature is on; a 404 for everybody otherwise, before the view's own checks (a visitor who is
    not signed in learns nothing either: the page does not exist)."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not enabled(name):
            raise Http404
        return view(request, *args, **kwargs)

    return wrapper
