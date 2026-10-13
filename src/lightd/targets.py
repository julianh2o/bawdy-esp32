"""Targets: ``"<controller id>/<channel number>"``, e.g. ``wled_01/2`` (channel numbers are 1-based)."""

from __future__ import annotations
from dataclasses import dataclass

from lightd.config import ID_RE


class TargetError(ValueError):
    pass


@dataclass(frozen=True, order=True)
class Target:
    controller_id: str
    channel: int  # 0-based index

    @classmethod
    def parse(cls, text: str) -> Target:
        if not isinstance(text, str) or text.count("/") != 1:
            raise TargetError(f"Target '{text}' should look like 'wled_01/2'")
        cid, num = text.strip().split("/")
        if not ID_RE.match(cid):
            raise TargetError(f"Target '{text}' has an invalid controller id")
        if not num.isdigit() or int(num) < 1:
            raise TargetError(f"Target '{text}' needs a channel number starting at 1")
        return cls(cid, int(num) - 1)

    def __str__(self) -> str:
        return f"{self.controller_id}/{self.channel + 1}"


def parse_targets(values: object) -> list[Target]:
    """A list of target strings (or one string) -> unique Targets, in order."""
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list) or not values:
        raise TargetError("Expected 'targets': a non-empty list like ['wled_01/1', 'wled_01/2']")
    out: list[Target] = []
    for v in values:
        t = Target.parse(v)
        if t not in out:
            out.append(t)
    return out
