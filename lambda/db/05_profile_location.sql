-- Added after the first release. The sign-in profile now includes where the person works,
-- so the assistant can answer a country-specific policy for the person's own country.
-- Setup runs this on every run, so databases created before the change get it too.
-- A function's columns cannot be changed in place, so it is dropped and created again.

DROP FUNCTION IF EXISTS employee_by_email(text);

CREATE FUNCTION employee_by_email(p_email text)
RETURNS TABLE (employee_id text, full_name text, role text, department text, job_title text, location text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public
AS $$
    SELECT e.employee_id, e.full_name, e.role, e.department, e.job_title, e.location
    FROM employees e
    WHERE e.email = lower(p_email)
$$;

REVOKE ALL ON FUNCTION employee_by_email(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION employee_by_email(text) TO hr_app;
