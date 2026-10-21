"""Row-level security in PostgreSQL decides whose records come back.

These tests need a real database, because row-level security is a database
feature. Point DATABASE_URL and DATABASE_ADMIN_URL at Aurora and run them from
your machine. See "Run the tests" in the README.

They sign in as the test accounts in db/04_test_accounts.sql, never as the demo employees people use.
"""

from app import database, employees


def names_seen_by(email):
    viewer = employees.find_by_email(email)
    return {record["full_name"] for record in employees.visible_records(viewer)}


def test_login_finds_known_emails_only():
    assert employees.find_by_email("qa.employee@northwind.example")["full_name"] == "Casey Test"
    assert employees.find_by_email("  QA.Employee@northwind.example ") is not None
    assert employees.find_by_email("someone@example.com") is None


def test_login_lookup_has_no_salary():
    assert "base_salary" not in employees.find_by_email("qa.employee@northwind.example")


def test_employee_sees_only_their_own_record():
    viewer = employees.find_by_email("qa.employee@northwind.example")
    records = employees.visible_records(viewer)
    assert [r["full_name"] for r in records] == ["Casey Test"]
    assert records[0]["base_salary"] == 117500
    assert records[0]["manager"] == "Morgan Test"  # a manager's name is not secret


def test_manager_sees_their_direct_reports():
    assert names_seen_by("qa.manager@northwind.example") == {"Morgan Test", "Casey Test", "Jordan Test"}


def test_hr_sees_everyone():
    everyone = database.run_admin_query("SELECT count(*) AS n FROM employees")[0]["n"]
    assert len(names_seen_by("qa.hr@northwind.example")) == everyone


def test_exec_is_not_hr():
    assert names_seen_by("qa.exec@northwind.example") == {"Avery Test", "Riley Test", "Morgan Test", "Quinn Test"}


def test_the_database_blocks_even_a_query_without_a_where_clause():
    # Someone "forgets" to say who is asking: the app user sees nothing at all
    assert database.query("SELECT * FROM employees") == []
    # Someone tries to read another person's row directly: still filtered
    rows = database.query("SELECT * FROM employees WHERE employee_id = 'NW-9005'", viewer_id="NW-9004")
    assert rows == []


def test_the_setting_does_not_leak_into_the_next_query():
    database.query("SELECT 1", viewer_id="NW-9002")  # HR, in its own transaction
    assert database.query("SELECT * FROM employees") == []


def test_the_app_user_cannot_change_salaries():
    import psycopg
    import pytest

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        database.query("UPDATE employees SET base_salary = 1", viewer_id="NW-9004")


def test_every_lookup_is_logged():
    def count():
        return database.run_admin_query("SELECT count(*) AS n FROM employee_lookup_log")[0]["n"]

    before = count()
    names_seen_by("qa.employee@northwind.example")
    assert count() == before + 1
