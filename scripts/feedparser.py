"""Minimal RSS parser shim for standalone scripts.

Provides the tiny subset of feedparser used by build_travel_candidate_prefilter.py.
This avoids depending on third-party feedparser API details in the probe script.
"""

from __future__ import annotations

from types import SimpleNamespace
from xml.etree import ElementTree as ET


def _text(node, tag: str) -> str:
    child = node.find(tag)
    return (child.text or "").strip() if child is not None else ""


def loads(data: bytes | str):
    if isinstance(data, str):
        data = data.encode("utf-8")
    root = ET.fromstring(data)
    entries = []
    for item in root.findall(".//item"):
        source_node = item.find("source")
        source = {"title": (source_node.text or "").strip()} if source_node is not None else {}
        entries.append(
            {
                "title": _text(item, "title"),
                "link": _text(item, "link"),
                "published": _text(item, "pubDate"),
                "source": source,
            }
        )
    return SimpleNamespace(entries=entries)


def parse(data: bytes | str):
    return loads(data)
