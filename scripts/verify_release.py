"""Verify reviewed files and outputs without any Git metadata."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from bbp_demo.data import write_fixture
from bbp_demo.report import build_report, canonical, RESOURCES

manifest = json.loads((ROOT / "release-manifest.json").read_text(encoding="utf-8"))
ignored = {".git", ".venv", "__pycache__", "build", "dist", ".test-tmp"}
files = {p.relative_to(ROOT).as_posix(): p for p in ROOT.rglob("*") if p.is_file()
         and not any(part in ignored or part.endswith(".egg-info") for part in p.relative_to(ROOT).parts)
         and p.name != "release-manifest.json"}
if set(files) != set(manifest["files"]):
    raise SystemExit("Release file list changed; review manifest")
for name, path in files.items():
    if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["files"][name]:
        raise SystemExit("Reviewed file changed: " + name)
if canonical(build_report()) != (ROOT / "examples/report/report.json").read_text(encoding="utf-8"):
    raise SystemExit("Report no longer reproduces")
with tempfile.TemporaryDirectory() as folder:
    fixture = Path(folder) / "synthetic.csv"
    write_fixture(fixture)
    if fixture.read_bytes() != (RESOURCES / "synthetic_m5.csv").read_bytes():
        raise SystemExit("Fixture no longer reproduces")
print(f"PASS: {len(files)} reviewed files; exact report and dataset reproduction; no Git dependency")
