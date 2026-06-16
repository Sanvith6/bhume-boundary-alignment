import sys
import subprocess
from pathlib import Path

def main():
    root_dir = Path(__file__).resolve().parent.parent
    python_exe = root_dir / ".venv" / "Scripts" / "python.exe"
    if not python_exe.exists():
        python_exe = sys.executable

    test_files = sorted([
        f.name for f in root_dir.glob("test_*.py")
    ])

    print(f"Using python: {python_exe}")
    print(f"Found {len(test_files)} test files to run: {test_files}\n")

    passed_tests = []
    failed_tests = []

    for test_file in test_files:
        print(f"=== Running {test_file} ===")
        # Run test script as a subprocess in the root directory
        res = subprocess.run(
            [str(python_exe), test_file],
            cwd=str(root_dir),
            capture_output=True,
            text=True
        )
        
        # Print stdout and stderr
        if res.stdout:
            print(res.stdout)
        if res.stderr:
            print(res.stderr, file=sys.stderr)

        if res.returncode == 0:
            passed_tests.append(test_file)
            print(f"RESULT: {test_file} PASSED\n")
        else:
            failed_tests.append(test_file)
            print(f"RESULT: {test_file} FAILED (Exit Code: {res.returncode})\n")

    print("=== SUMMARY ===")
    print(f"Total tests run: {len(test_files)}")
    print(f"Passed: {len(passed_tests)}/{len(test_files)}")
    print(f"Failed: {len(failed_tests)}/{len(test_files)}")
    if failed_tests:
        print(f"Failed tests: {failed_tests}")
        sys.exit(1)
    else:
        print("All tests passed successfully!")
        sys.exit(0)

if __name__ == "__main__":
    main()
