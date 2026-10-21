"""Scan the code itself and report what the tools find. Runs in the code-scan workflow.

    python ci/code_scan.py lint                 Ruff (Python) and ESLint (React)
    python ci/code_scan.py python-security      Bandit (risky Python code) and pip-audit (Python packages)
    python ci/code_scan.py frontend-security    npm audit (React app packages)

The findings go to the run's summary page on GitHub, or to the terminal on your computer.
In this lab the scans only report: this script always exits with 0, so a finding never
stops the pipeline. A company would usually block a release on high findings.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_CODE = ['lambda/app', 'ci', 'scripts']
REQUIREMENTS = ['lambda/requirements.txt', 'ci/requirements.txt']


def run(command, folder=ROOT):
    """Run a tool and return what it printed. A tool exits with 1 when it finds something, so the exit code is ignored."""
    program = shutil.which(command[0]) or command[0]   # finds npm.cmd and npx.cmd on Windows too
    result = subprocess.run([program, *command[1:]], cwd=folder, capture_output=True, text=True,
                            encoding='utf-8', errors='replace')   # tools print UTF-8, also on Windows
    return result.stdout


def report(title, summary, details):
    """Write one tool's result: a one-line summary, and the full output folded underneath."""
    text = f'### {title}\n\n{summary}\n\n'
    if details.strip():
        text += f'<details><summary>Details</summary>\n\n```\n{details.strip()}\n```\n\n</details>\n\n'
    summary_file = os.environ.get('GITHUB_STEP_SUMMARY')   # GitHub sets this on its runners
    if summary_file:
        with open(summary_file, 'a', encoding='utf-8') as page:
            page.write(text)
    print(text)


def lint():
    # Ruff: style problems and likely bugs in the Python code.
    output = run(['ruff', 'check', *PYTHON_CODE, '--output-format', 'concise'])
    findings = [line for line in output.splitlines() if ': ' in line and line.split(':')[0].endswith('.py')]
    report('Ruff (Python lint)', f'{len(findings)} findings.', output)

    # ESLint: the same for the React app. Needs `npm ci` in frontend/ first.
    output = run(['npx', 'eslint', '.', '--format', 'json'], folder=ROOT / 'frontend')
    files = json.loads(output or '[]')
    errors = sum(file['errorCount'] for file in files)
    warnings = sum(file['warningCount'] for file in files)
    details = '\n'.join(f"{Path(file['filePath']).name}:{message['line']}  {message['message']}"
                        for file in files for message in file['messages'])
    report('ESLint (React lint)', f'Errors: {errors}. Warnings: {warnings}.', details)


def python_security():
    # Bandit: risky patterns in Python code, such as running shell commands or opening URLs.
    output = run(['bandit', '-q', '-r', *PYTHON_CODE, '-x', '*/tests/*', '-f', 'json'])
    issues = json.loads(output)['results']
    counts = {level: sum(1 for issue in issues if issue['issue_severity'] == level) for level in ('HIGH', 'MEDIUM', 'LOW')}
    details = '\n'.join(f"{issue['issue_severity']:6}  {issue['filename']}:{issue['line_number']}  {issue['issue_text']}"
                        for issue in issues if issue['issue_severity'] != 'LOW')
    report('Bandit (risky Python code)',
           f"{counts['HIGH']} high, {counts['MEDIUM']} medium, {counts['LOW']} low. Details list high and medium only.", details)

    # pip-audit: Python packages with known vulnerabilities.
    for requirements in REQUIREMENTS:
        output = run(['pip-audit', '-r', requirements, '--progress-spinner', 'off', '-f', 'json'])
        packages = json.loads(output or '{"dependencies": []}')['dependencies']
        vulnerable = [package for package in packages if package.get('vulns')]
        details = '\n'.join(f"{package['name']} {package['version']}: " + ', '.join(vuln['id'] for vuln in package['vulns'])
                            for package in vulnerable)
        report(f'pip-audit ({requirements})', f'{len(vulnerable)} packages with known vulnerabilities.', details)


def frontend_security():
    # npm audit: React app packages with known vulnerabilities. It reads package-lock.json.
    output = run(['npm', 'audit', '--json'], folder=ROOT / 'frontend')
    counts = json.loads(output)['metadata']['vulnerabilities']
    summary = ', '.join(f'{counts[level]} {level}' for level in ('critical', 'high', 'moderate', 'low'))
    report('npm audit (React app packages)', summary + '.', run(['npm', 'audit'], folder=ROOT / 'frontend'))


if __name__ == '__main__':
    scans = {'lint': lint, 'python-security': python_security, 'frontend-security': frontend_security}
    if len(sys.argv) != 2 or sys.argv[1] not in scans:
        sys.exit(f'Usage: python ci/code_scan.py {"|".join(scans)}')
    try:
        scans[sys.argv[1]]()
    except Exception as error:   # a tool that crashes is reported, and still never fails the run
        report('Scan error', f'The {sys.argv[1]} scan could not finish: {error}', '')
