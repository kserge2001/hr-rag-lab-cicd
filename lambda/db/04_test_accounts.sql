-- Test accounts for the pipeline and the automated tests. Applied on every setup run.
--
-- People sign in to the app as the demo employees in 03_seed_data.sql. Automated tests never
-- use those accounts: they sign in as these instead, so a test can never read, change or delete
-- what a person did. The pipeline owns these accounts and deletes their chats after each run.
--
-- One account per role, arranged so every access rule has a case:
--   Avery  exec      sees their direct reports, but is not HR
--   Riley  hr        sees everyone
--   Morgan manager   sees Casey and Jordan, who report to them, but not Quinn
--   Casey  employee  sees only their own record; the evaluation signs in as Casey most often
--   Jordan, Quinn    never sign in: they are the records the others try to read
--
-- Upserting keeps the values exact, because the evaluation cases check them.

INSERT INTO employees
    (employee_id, email, full_name, role, department, job_title, job_level,
     manager_id, location, hire_date, base_salary, bonus_target_percent, pto_days_remaining)
VALUES
    ('NW-9001', 'qa.exec@northwind.example',     'Avery Test',  'exec',     'Executive',         'Chief Operating Officer',   'L8', NULL,      'New York, NY', '2018-01-08', 301500, 20, 14.0),
    ('NW-9002', 'qa.hr@northwind.example',       'Riley Test',  'hr',       'People Operations', 'HR Manager',                'L6', 'NW-9001', 'Chicago, IL',  '2019-04-15', 187500, 15, 11.5),
    ('NW-9003', 'qa.manager@northwind.example',  'Morgan Test', 'manager',  'Engineering',       'Engineering Manager',       'L6', 'NW-9001', 'Austin, TX',   '2018-10-01', 203500, 15,  9.0),
    ('NW-9004', 'qa.employee@northwind.example', 'Casey Test',  'employee', 'Engineering',       'Software Engineer II',      'L3', 'NW-9003', 'Austin, TX',   '2023-03-06', 117500,  5, 13.5),
    ('NW-9005', 'qa.peer@northwind.example',     'Jordan Test', 'employee', 'Engineering',       'Senior Software Engineer',  'L4', 'NW-9003', 'Austin, TX',   '2021-06-07', 143500, 10, 10.0),
    ('NW-9006', 'qa.outsider@northwind.example', 'Quinn Test',  'employee', 'Sales',             'Account Executive',         'L3', 'NW-9001', 'Chicago, IL',  '2022-09-12', 104500, 10,  8.5)
ON CONFLICT (employee_id) DO UPDATE SET
    email = EXCLUDED.email, full_name = EXCLUDED.full_name, role = EXCLUDED.role,
    department = EXCLUDED.department, job_title = EXCLUDED.job_title, job_level = EXCLUDED.job_level,
    manager_id = EXCLUDED.manager_id, location = EXCLUDED.location, hire_date = EXCLUDED.hire_date,
    base_salary = EXCLUDED.base_salary, bonus_target_percent = EXCLUDED.bonus_target_percent,
    pto_days_remaining = EXCLUDED.pto_days_remaining;
