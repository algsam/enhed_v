"""Tests for the partition assertion (SPEC §5.1, §11)."""

from __future__ import annotations

import pytest

from ed.domain.partition import PartitionError, assert_partition


def test_standalone_units_partition_cleanly() -> None:
    online = ["CT1", "CT2", "ST1"]
    entities = {
        "CT1": frozenset({"CT1"}),
        "CT2": frozenset({"CT2"}),
        "ST1": frozenset({"ST1"}),
    }
    assert_partition(online, entities)


def test_cc_block_partitions_with_standalone_units() -> None:
    # SPEC §5.2 CT1-simple-cycle case: roster {CT1, CT2, ST1}; CT1 runs
    # standalone while CT2+ST1 form the active CC block config — two entities.
    online = ["CT1", "CT2", "ST1"]
    entities = {
        "CT1": frozenset({"CT1"}),
        "BLOCK_CC1": frozenset({"CT2", "ST1"}),
    }
    assert_partition(online, entities)


def test_offline_units_are_excluded_from_the_online_set() -> None:
    online = ["CT1"]  # CT2 is offline and not passed in
    entities = {"CT1": frozenset({"CT1"})}
    assert_partition(online, entities)


def test_missing_unit_raises() -> None:
    online = ["CT1", "CT2"]
    entities = {"CT1": frozenset({"CT1"})}  # CT2 dispatched by nobody
    with pytest.raises(PartitionError, match="dispatched by no entity"):
        assert_partition(online, entities)


def test_double_claimed_unit_raises() -> None:
    online = ["CT1", "CT2", "ST1"]
    entities = {
        "CT1": frozenset({"CT1"}),
        "BLOCK_A": frozenset({"CT2", "ST1"}),
        "BLOCK_B": frozenset({"ST1"}),  # ST1 claimed by both BLOCK_A and BLOCK_B
    }
    with pytest.raises(PartitionError, match="claimed by both"):
        assert_partition(online, entities)


def test_entity_claiming_a_unit_not_in_the_online_set_raises() -> None:
    online = ["CT1"]
    entities = {
        "CT1": frozenset({"CT1"}),
        "GHOST": frozenset({"CT_OFFLINE"}),  # not online, not known
    }
    with pytest.raises(PartitionError, match="not in the online set"):
        assert_partition(online, entities)


def test_empty_system_partitions_trivially() -> None:
    assert_partition([], {})
