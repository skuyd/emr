"""Run a mandatory pytest selection; skipped or empty selections fail CI."""

from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    with tempfile.TemporaryDirectory(prefix="phr-required-tests-") as directory:
        report = Path(directory) / "pytest.xml"
        result = subprocess.run([sys.executable, "-m", "pytest", *arguments, "--junitxml", str(report)])
        if result.returncode:
            return result.returncode
        try:
            root = ET.parse(report).getroot()
        except (OSError, ET.ParseError):
            print("Required tests failed: no valid test report.", file=sys.stderr)
            return 1
        cases = list(root.iter("testcase"))
        if not cases or any(case.find("skipped") is not None for case in cases):
            print("Required tests failed: selection was empty or contained skipped tests.", file=sys.stderr)
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
