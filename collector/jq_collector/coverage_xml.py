"""Read line coverage out of the zipped ``coverage.xml`` CI publishes.

Split out of github.py, which fetches the artifact: nothing here touches the
network, and nothing in the REST client needs to know what a zip bomb is. The
blob is another repo's CI output, so it is untrusted input - every guard on it
lives in this module, next to the parsing it protects.
"""

from __future__ import annotations

import io
import logging
import zipfile

# coverage.xml comes out of another repo's CI artifact, so it is untrusted
# input. defusedxml refuses entity declarations and external references, and
# raises a ValueError subclass for them, which read() already treats as a
# malformed report rather than a failed refresh.
from defusedxml import ElementTree

# Under github.py's name, not this module's: the coverage reader was split out of it, and the
# logger name is printed on every line of `docker logs`, where a refactor
# should not move anything somebody greps for.
log = logging.getLogger("jq_collector.github")

# Guards on an archive we did not build. Coverage reports for a fleet this size
# are tens of kilobytes; anything near these is a bug or a bomb, and unpacking
# it would be the collector's problem rather than CI's.
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_UNPACKED_BYTES = 64 * 1024 * 1024


def read(blob: bytes, label: str) -> tuple[float, int] | None:
    """``(percent, lines measured)`` from ``blob``, or None if it cannot be read.

    ``label`` names the repo in the warning. A malformed report is CI's problem,
    not a reason to fail a refresh that has already gathered everything else,
    so nothing here raises.
    """
    if len(blob) > MAX_ARTIFACT_BYTES:
        log.warning("%s: coverage artifact is %d bytes, skipping", label, len(blob))
        return None
    try:
        return parse(blob)
    except (zipfile.BadZipFile, ElementTree.ParseError, ValueError) as exc:
        log.warning("%s: could not read coverage artifact: %s", label, exc)
        return None


def parse(blob: bytes) -> tuple[float, int] | None:
    """``(percent, lines measured)`` out of the coverage.xml in a zipped artifact.

    None when the zip holds no coverage.xml, or one without a ``line-rate``.
    Raises on a blob that is not a zip, XML that does not parse, and a member
    that unpacks past MAX_UNPACKED_BYTES.
    """
    with zipfile.ZipFile(io.BytesIO(blob)) as bundle:
        members = [m for m in bundle.infolist() if m.filename.endswith("coverage.xml")]
        if not members:
            return None
        member = members[0]
        if member.file_size > MAX_UNPACKED_BYTES:
            raise ValueError(f"coverage.xml unpacks to {member.file_size} bytes")
        root = ElementTree.fromstring(bundle.read(member))
    rate = root.get("line-rate")
    if rate is None:
        return None
    return round(float(rate) * 100, 1), int(root.get("lines-valid") or 0)
