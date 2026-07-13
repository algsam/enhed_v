"""Slack — identity-only marker for `BalanceModule`'s auto-injected
unserved-energy/over-generation pseudo-units (SPEC §5.6; CLAUDE.md
"Operational"; build order step 8).

`BalanceModule` creates and owns the actual solver variables directly (it
must, to couple them into its own balance row with independent penalties) —
this class is never added to a `ModelBuilder`'s entity list and contributes
nothing itself. It exists solely so slack has a first-class `resource_type`
and `is_system_generated=True` for operator-facing resource listings
(`ed.entities.registry`): "an operator must never be able to delete the
slack that keeps the solve feasible" is enforced there, and requires slack
to be identifiable as *a* resource in the first place, not a raw variable
with no identity.
"""

from __future__ import annotations

from typing import Literal

from ed.domain.enums import ResourceType

SlackDirection = Literal["unserved", "overgen"]


class Slack:
    """Identity marker for one of `BalanceModule`'s slack pseudo-units.

    `direction` distinguishes the deficit (`"unserved"`) and surplus
    (`"overgen"`) legs — CLAUDE.md: "Use separate up-slack (deficit) and
    down-slack (surplus) with independent penalties."
    """

    resource_type = ResourceType.SLACK
    is_system_generated = True
    emits_setpoint = False

    def __init__(self, slack_id: str, bus: str, direction: SlackDirection) -> None:
        self.slack_id = slack_id
        self.bus = bus
        self.direction = direction
