from types import SimpleNamespace

import pytest

from management.research.pi.context import prior_critique_for

pytestmark = pytest.mark.unit


class Db:
    def __init__(self, result):
        self.result = result

    async def scalar(self, query):
        return self.result


RUN = SimpleNamespace(id=1)
STAGE = SimpleNamespace(id=2, stage_key="hypothesis")


async def test_a_repeat_review_carries_the_earlier_report():
    result = {"report": {"verdict": "serious", "summary": "s", "points": []}}
    report = await prior_critique_for(Db(result), RUN, STAGE)
    assert report.verdict == "serious"


async def test_a_first_review_has_no_prior_critique():
    assert await prior_critique_for(Db(None), RUN, STAGE) is None
    assert await prior_critique_for(Db({"report": None}), RUN, STAGE) is None
    assert await prior_critique_for(Db({}), RUN, None) is None
