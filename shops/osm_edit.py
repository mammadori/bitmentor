"""The edits for OpenStreetMap, ready to be sent by a person: which changes, as osmChange, and the links to make them by hand.

Only `manage.py osm_upload` uses this, and it sends nothing unless a person has set a token and types the confirmation
(docs/features/openstreetmap.md, "Automatic edits"). What is written is decided here, by rules the tests pin down:
- only the tags of the bitcoin settings (`Settings.tags`): never a name, an address, a category, a position;
- only for shops linked to an element the reading just found, with the version it has now (a change made there in the
  meantime makes the server refuse the upload, which is what we want);
- the proposals that need a person's judgement (`REVIEW`) only when the person asks for them;
- a few elements at a time: one changeset is a thing a person can read through."""
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from django.utils import timezone

from . import certified
from .models import OsmRequest, Shop, real
from .osm import REVIEW, Element, changes_for, changeset_comment, editor_url, element_url

# A changeset a person can read: more elements wait for the next run
MAX_ELEMENTS = 100
CREATED_BY = "bitmentor"


@dataclass
class Edit:
    shop: Shop
    element: Element
    changes: list

    @property
    def tags(self):
        """The element's tags after the changes."""
        found = dict(self.element.tags)
        for tag, _old, new, _kind in self.changes:
            if new is None:
                found.pop(tag, None)
            else:
                found[tag] = new
        return found


def plan(config, elements, now=None, review=False, limit=MAX_ELEMENTS):
    """The edits to send: [Edit], most careful first (by shop name), and how many were left for later. `elements` come
    from a reading with `meta` (versions, nodes, members)."""
    today = certified.local_day(now or timezone.now())
    by_ref = {element.ref: element for element in elements}
    edits = []
    for shop in real(Shop.objects.exclude(osm_ref="")).order_by("name", "pk"):
        element = by_ref.get(shop.osm_ref)
        # An element that was not read whole is never given back: the edit would take away what was left out
        if element is None or element.version is None or element.unreliable:
            continue
        found = [change for change in changes_for(shop, element, config, today)
                 if change[0] in config.tags and (review or change[3] != REVIEW)]
        if found:
            edits.append(Edit(shop, element, found))
    return edits[:limit], max(len(edits) - limit, 0)


def osm_change(edits, changeset=None):
    """The osmChange document for the edits: bytes. Every element is given back whole, as the server has it now, with its
    tags changed: that is what a `modify` is."""
    root = ET.Element("osmChange", version="0.6", generator=CREATED_BY)
    modify = ET.SubElement(root, "modify")
    for edit in edits:
        element = edit.element
        kind, _slash, number = element.ref.partition("/")
        attributes = {"id": number, "version": str(element.version)}
        if changeset:
            attributes["changeset"] = str(changeset)
        if kind == "node":
            attributes.update(lat=element.text_lat, lon=element.text_lon)
        node = ET.SubElement(modify, kind, attributes)
        for ref in element.nodes or []:
            ET.SubElement(node, "nd", ref=str(ref))
        for member in element.members or []:
            ET.SubElement(node, "member", type=member["type"], ref=str(member["ref"]), role=member["role"])
        for tag, value in sorted(edit.tags.items()):
            ET.SubElement(node, "tag", k=tag, v=value)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def changeset_xml(config, site):
    """The tags of the changeset: what it is, who made it, and where the information comes from: the site, which checks the
    shops' Lightning addresses (the comment says so). Not `survey`: nobody went to look."""
    root = ET.Element("osm")
    changeset = ET.SubElement(root, "changeset")
    for key, value in (("created_by", CREATED_BY), ("comment", changeset_comment(config)), ("source", site)):
        ET.SubElement(changeset, "tag", k=key, v=value)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def lines(edit):
    """What changes on one element, a tag a line, for a person to read before anything is sent."""
    found = []
    for tag, old, new, kind in edit.changes:
        before = "(missing)" if old is None else old
        after = "(remove the tag)" if new is None else new
        found.append(f"    {tag}: {before} -> {after}" + ("   [needs your judgement]" if kind == REVIEW else ""))
    return found


def links(edit):
    """Where to make the same edit by hand, in the editor (iD), and where to see the element."""
    return editor_url(edit.shop, edit.element.ref), element_url(edit.element.ref)


def finish_requests(shops):
    """The merchants' requests that this upload did: marked done. Only the shops with nothing left to review."""
    now = timezone.now()
    return OsmRequest.objects.filter(shop__in=shops, state=OsmRequest.State.OPEN, kind=OsmRequest.Kind.UPDATE).update(
        state=OsmRequest.State.DONE, done=now, done_by="osm_upload")
