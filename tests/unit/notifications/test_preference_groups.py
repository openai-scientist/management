from typing import get_args

import pytest

from management.notifications.preferences import GROUPS, KIND_GROUP, MANDATORY_KINDS
from management.notifications.router import NotificationKind

pytestmark = pytest.mark.unit


def test_every_notice_kind_has_a_group_or_always_arrives():
    assert set(KIND_GROUP) | MANDATORY_KINDS == set(get_args(NotificationKind))
    assert not set(KIND_GROUP) & MANDATORY_KINDS
    assert set(KIND_GROUP.values()) == set(GROUPS)
