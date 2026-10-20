import pytest

from management.infra.email_templates import notice_kinds, project_notice
from management.notifications.preferences import KIND_GROUP

pytestmark = pytest.mark.unit


def test_every_grouped_notice_kind_has_an_email() -> None:
    assert notice_kinds() == frozenset(KIND_GROUP)


def test_a_notice_email_names_the_project_actor_and_link_in_both_bodies() -> None:
    subject, text, html = project_notice(
        "run_commented",
        recipient_name="Binh",
        project_name="Soil <study>",
        actor_name="An",
        run_topic="Nitrogen uptake",
        project_url="https://app.example.com/projects/p1",
    )
    assert subject == "New comment: Soil <study>"
    for body in (text, html):
        assert "Binh" in body and "Nitrogen uptake" in body
        assert "https://app.example.com/projects/p1" in body
    assert "An commented on your research run in Soil <study>." in text
    assert "Soil &lt;study&gt;" in html and "<study>" not in html
