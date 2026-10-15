import pytest

from management.research.contracts import Anchor, Finding, ReviewFindings
from management.research.pi.review_gate import MAX_RULE_REVISIONS, comment_findings

pytestmark = pytest.mark.unit


def review(*severities):
    findings = [
        Finding(
            finding_id=f"F1-{n}",
            anchor=Anchor(locator="hypothesis:H1"),
            severity=severity,
            issue="Issue",
            author="reviewer",
        )
        for n, severity in enumerate(severities, 1)
    ]
    return ReviewFindings(
        round=1,
        reviewed_artifact_id="a1",
        reviewer_model="m",
        findings=findings,
        passed="blocking" not in severities,
    )


def test_a_passed_review_is_approved():
    assert comment_findings(review("major", "minor"), set(), 0) is None


def test_new_blocking_findings_are_sent_back_whole():
    sent = comment_findings(review("blocking", "major", "blocking"), set(), 0)
    assert [f.finding_id for f in sent] == ["F1-1", "F1-3"]


def test_a_subset_of_what_was_sent_is_approved():
    assert comment_findings(review("blocking", "blocking"), {"F1-1", "F1-2", "F9-9"}, 1) is None


def test_one_new_blocking_finding_sends_them_all_again():
    sent = comment_findings(review("blocking", "blocking"), {"F1-1"}, 1)
    assert [f.finding_id for f in sent] == ["F1-1", "F1-2"]


def test_the_cap_ends_the_comments():
    assert comment_findings(review("blocking"), set(), MAX_RULE_REVISIONS) is None
    assert comment_findings(review("blocking"), set(), MAX_RULE_REVISIONS - 1) is not None
