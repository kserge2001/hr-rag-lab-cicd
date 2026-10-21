"""Produce student sources without instructor solutions, credentials, state or artifacts."""
from pathlib import Path
import zipfile
import os

ROOT = Path(__file__).resolve().parents[1]
SKIP = {'instructor', '.git', '.terraform', '__pycache__', '.pytest_cache',
        'node_modules', 'build', 'dist', '.venv', 'venv'}
def allowed(path):
    student_path = path.relative_to(ROOT).as_posix()
    if 'instructor' in student_path.lower() or path.name in {'BASELINE-VERIFICATION.md', 'HR-LAB-CLASSROOM-PLAN.md', 'CLASS-TIMING.md', 'CLASSROOM-PLAN.md'}:
        return False
    if student_path.startswith('.github/workflows/'):
        return False
    if any(part in SKIP for part in path.relative_to(ROOT).parts):
        return False
    return not (path.name in {'tfplan', 'demo-sign-in.txt', 'prepare_lab.py', 'WEEK06-HANDOFF.md', 'HR-LAB-AGREED-PLAN.md', 'promptfoo.generated.json'} or (path.name.startswith('.env') and path.name != '.env.langfuse.example') or path.name.endswith(('.tfstate', '.tfplan', '.pyc', '.pyo'))
                or '.tfstate.' in path.name or
                (path.suffix == '.tfvars' and path.name != 'terraform.tfvars.example'))

if __name__ == '__main__':
    output = ROOT / 'build/hr-student-starter.zip'
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for directory, folders, files in os.walk(ROOT):
            folders[:] = sorted(folder for folder in folders if folder not in SKIP)
            for filename in sorted(files):
                path = Path(directory) / filename
                if allowed(path):
                    archive.write(path, path.relative_to(ROOT).as_posix())
    print(output.name)
