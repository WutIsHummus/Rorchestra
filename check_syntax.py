import py_compile, sys, os
os.chdir(r"c:\Users\alper\CLIGraph\orchestrator")
files = [
    r"app\models\entities.py",
    r"app\models\schemas.py",
    r"app\config.py",
    r"app\storage\database.py",
    r"app\services\agents\orchestrator.py",
]
ok = True
for f in files:
    try:
        py_compile.compile(f, doraise=True)
        print(f"OK: {f}")
    except py_compile.PyCompileError as e:
        print(f"FAIL: {f}: {e}")
        ok = False
if ok:
    print("ALL SYNTAX OK")
else:
    print("SYNTAX ERRORS FOUND")
    sys.exit(1)
