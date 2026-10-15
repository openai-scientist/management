import pytest

from management.research.contracts import Anchor, Finding, ReviewFindings
from management.research.pi.review_gate import MAX_RULE_REVISIONS, comment_findings

pytestmark = pytest.mark.unit


def review(*severities, round=1, kinds=None):
    findings = [
        Finding(
            finding_id=f"F1-{n}",
            anchor=Anchor(locator="hypothesis:H1"),
            severity=severity,
            kind=(kinds or {}).get(n, "fix"),
            issue="Issue",
            author="reviewer",
        )
        for n, severity in enumerate(severities, 1)
    ]
    return ReviewFindings(
        round=round,
        reviewed_artifact_id="a1",
        reviewer_model="m",
        findings=findings,
        passed="blocking" not in severities,
    )


def test_a_passed_review_is_approved():
    assert comment_findings(review("major", "minor"), 0) is None


def test_first_round_fix_findings_are_sent_back_alone():
    sent = comment_findings(review("blocking", "major", "blocking", kinds={3: "needs_evidence"}), 0)
    assert [f.finding_id for f in sent] == ["F1-1"]


def test_blocking_findings_that_need_evidence_never_comment():
    assert comment_findings(review("blocking", kinds={1: "needs_evidence"}), 0) is None


def test_a_later_review_round_is_approved():
    assert comment_findings(review("blocking", round=2), 0) is None


def test_the_rule_comments_once_per_stage_run():
    assert MAX_RULE_REVISIONS == 1
    assert comment_findings(review("blocking"), MAX_RULE_REVISIONS) is None
    assert comment_findings(review("blocking"), MAX_RULE_REVISIONS - 1) is not None
