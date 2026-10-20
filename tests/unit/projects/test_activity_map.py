from types import SimpleNamespace
from uuid import uuid4

import pytest

from management.projects.activity import ACTIONS, LABEL_KEYS, LABEL_QUERIES, _target, actions_for
from management.research.models import StageDecision

pytestmark = pytest.mark.unit


def test_categories_split_the_allowlist_without_overlap() -> None:
    by_category = [
        set(actions_for(category)) for category in ("research", "data", "members", "settings")
    ]
    assert set().union(*by_category) == set(ACTIONS) == set(actions_for(None))
    assert sum(len(actions) for actions in by_category) == len(ACTIONS)


def test_only_names_are_read_from_audit_details() -> None:
    # Other keys hold old values, hosts, emails or hashes; widening this leaks them.
    assert LABEL_KEYS == ("name", "title", "filename", "topic")


def test_every_stage_decision_has_its_own_action() -> None:
    describe = ACTIONS["run.decision_recorded"].describe
    allowed = StageDecision.__table__.constraints
    check = next(c for c in allowed if c.name == "ck_stage_decisions_action")
    for decision in ("answer_gate", "approve", "comment", "accept", "send_back", "rerun", "stop"):
        assert f"'{decision}'" in str(check.sqltext)
        assert describe({"decision": decision})[0] == f"run.decision.{decision}"


def test_every_action_points_at_a_resource_that_can_be_named() -> None:
    row = SimpleNamespace(resource_id=uuid4(), details={})
    for action, entry in ACTIONS.items():
        kind, _ = _target(row, entry)
        assert kind == "project" or kind in LABEL_QUERIES, action
