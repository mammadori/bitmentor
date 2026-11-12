"""The documents' links: every relative link in README.md and docs/ points to a file that exists, and every #anchor to a
heading of that file (as GitHub and GitLab make them). With FORBIDDEN_NAMES, a regular expression, no document names a
community either: the Django test does that for the image, which holds no documents.

Usage: python3 deploy/tests/test_documents.py   (from the root of the repository, or with it as the argument)
Exit code 0 when everything holds, 1 otherwise. Only the standard library: it runs without Django."""
import os
import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[2])
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def documents():
    yield ROOT / "README.md"
    yield from sorted((ROOT / "docs").rglob("*.md"))


def prose(path):
    """The lines outside fenced code blocks: a `# comment` in a shell example is not a heading."""
    inside = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if FENCE.match(line):
            inside = not inside
            continue
        if not inside:
            yield number, line


def slug(heading):
    """GitHub's anchor: lower case, markup and punctuation dropped, spaces to hyphens."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading).replace("`", "").strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path):
    seen = {}
    found = set()
    for _, line in prose(path):
        match = HEADING.match(line)
        if match:
            base = slug(match.group(2))
            count = seen.get(base, 0)
            seen[base] = count + 1
            found.add(base if not count else f"{base}-{count}")
    return found


def problems():
    cache = {}
    for doc in documents():
        for number, line in prose(doc):
            for target in LINK.findall(re.sub(r"`[^`]*`", "", line)):
                if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):
                    continue
                name, _, anchor = target.partition("#")
                linked = (doc.parent / name).resolve() if name else doc
                where = f"{doc.relative_to(ROOT)}:{number}: {target}"
                if not linked.exists():
                    yield f"{where}: no such file"
                    continue
                if ROOT.resolve() not in linked.parents and linked != ROOT.resolve():
                    yield f"{where}: outside the repository"
                    continue
                if anchor and linked.suffix == ".md":
                    if linked not in cache:
                        cache[linked] = anchors(linked)
                    if anchor not in cache[linked]:
                        yield f"{where}: no heading for #{anchor}"
    names = os.environ.get("FORBIDDEN_NAMES")
    if names:
        forbidden = re.compile(names, re.I)
        for doc in documents():
            text = doc.read_text(encoding="utf-8")
            if doc == ROOT / "README.md":  # the origin block names the community it comes from, on purpose: one, short
                blocks = re.findall("<!-- " + "origin -->.*?<!-- /origin -->", text, flags=re.S)
                if len(blocks) != 1 or blocks[0].count("\n") >= 6:
                    yield "README.md: the origin block must be one, under six lines"
                text = re.sub("<!-- " + "origin -->.*?<!-- /origin -->", lambda m: "\n" * m.group().count("\n"), text, flags=re.S)
            for number, line in enumerate(text.splitlines(), 1):
                if forbidden.search(line):
                    yield f"{doc.relative_to(ROOT)}:{number}: a forbidden name"


def main():
    found = list(problems())
    for problem in found:
        print(problem)
    print(f"{len(found)} problems" if found else "The documents' links hold")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
