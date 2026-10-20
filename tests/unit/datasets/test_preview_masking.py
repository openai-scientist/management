import pytest

from management.datasets.preview import REDACTED, mask
from management.projects.settings_service import email_domain_allowed

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "value",
    ["ann@example.com", "write to a.b@sub.example.org", "+84 912 345 678", "(028) 3822-1234"],
)
def test_emails_and_phone_numbers_are_masked_in_any_column(value: str) -> None:
    assert mask(["notes"], [[value]]).rows == [[REDACTED]]


@pytest.mark.parametrize("value", ["12", "3.14", "2026-10-18", "Hanoi", "1234", "not@an email"])
def test_ordinary_values_are_kept(value: str) -> None:
    assert mask(["notes"], [[value]]).rows == [[value]]


@pytest.mark.parametrize(
    "header", ["name", "Full Name", "first_name", "ho_ten", "SSN", "passport_no", "Student ID"]
)
def test_columns_naming_a_person_or_identifier_are_masked(header: str) -> None:
    preview = mask([header], [["anything"], [""]])
    assert preview.rows == [[REDACTED], [""]]
    assert preview.masked_cells == 1


@pytest.mark.parametrize("header", ["id", "run_id", "city", "score", "username"])
def test_other_columns_are_kept(header: str) -> None:
    assert mask([header], [["x"]]).rows == [["x"]]


@pytest.mark.parametrize(
    ("email", "allowed"),
    [
        ("a@example.com", True),
        ("A@EXAMPLE.COM", True),
        ("a@sub.example.com", False),
        ("a@evil-example.com", False),
        ("a@example.com.evil.org", False),
    ],
)
def test_domains_match_exactly(email: str, allowed: bool) -> None:
    assert email_domain_allowed(email, ["example.com"]) is allowed
    assert email_domain_allowed(email, []) is True
