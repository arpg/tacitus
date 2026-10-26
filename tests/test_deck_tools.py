#!/usr/bin/env python3
"""deck_tools.py: the preview viewport follows the deck's headmatter.

What must hold: a deck with no `aspectRatio` renders at 1280x720 as before;
a 3:2 poster renders at 1280x853, so the slide fills the frame instead of
sitting letterboxed between black bars; a malformed value falls back to
16:9 rather than failing the render.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import deck_tools  # noqa: E402


def _deck(head: str) -> str:
    return f"---\n{head}\n---\n\n# One\n\n---\n\n# Two\n"


@pytest.mark.parametrize("head, expected", [
    ("title: Plain", (1280, 720)),
    ("title: Poster\naspectRatio: 3/2\ncanvasWidth: 2160", (1280, 853)),
    ("aspectRatio: '4/3'", (1280, 960)),
    ("aspectRatio: 1.5", (1280, 853)),
    ("aspectRatio: 16/9  # the default, spelled out", (1280, 720)),
])
def test_viewport_follows_aspect_ratio(head, expected):
    assert deck_tools.preview_viewport(_deck(head)) == expected


@pytest.mark.parametrize("head", ["aspectRatio: wide", "aspectRatio: 0", "aspectRatio: 3/0"])
def test_bad_aspect_ratio_falls_back_to_16_9(head):
    assert deck_tools.preview_viewport(_deck(head)) == (1280, 720)


def test_no_headmatter_is_16_9():
    assert deck_tools.preview_viewport("# Just a slide\n") == (1280, 720)


def test_count_slides_unchanged_by_print_keys():
    text = _deck("aspectRatio: 3/2\nprintWidthIn: 36\nprintDpi: 240")
    assert deck_tools.count_slides(text) == 2
