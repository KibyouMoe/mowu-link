"""Run acceptance regressions from an installed rewrite-only package."""
import json
import os
from pathlib import Path
import sys
import unittest


def cases(suite):
    for item in suite:
        if isinstance(item,unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def select(suite):
    active = unittest.TestSuite()
    archival = []
    for test in cases(suite):
        test_id = test.id()
        method = getattr(test,test_id.rsplit(".",1)[-1],None)
        if getattr(type(test),"archival_compatibility",False) or getattr(method,"archival_compatibility",False):
            archival.append(test_id)
        else:
            active.addTest(test)
    return active,sorted(archival)


def main():
    root = Path(__file__).resolve().parents[1]
    if Path.cwd()!=root or any((root/name).exists() or (root/name).is_symlink() for name in ("server","sources")):
        raise SystemExit("Run from the installed rewrite package without the old server and sources trees.")
    if any(os.environ.get(name) for name in ("MOWULINK_TEST_LEGACY_ROOT","MOWULINK_TEST_LEGACY_WORKER_ROOT")):
        raise SystemExit("Remove the old source fixture overrides before running rewrite acceptance.")
    discovered = unittest.defaultTestLoader.discover("rewrite",top_level_dir=".")
    total = discovered.countTestCases()
    active,archival = select(discovered)
    if not active.countTestCases():
        raise SystemExit("No rewrite regression tests were discovered.")
    result = unittest.TextTestRunner().run(active)
    print(json.dumps({"discovered":total,"tests_run":result.testsRun,"archival_excluded":archival,
        "skipped":[test.id() for test,_ in result.skipped],"failed":len(result.failures),
        "errors":len(result.errors),"expected_failures":len(result.expectedFailures),
        "unexpected_successes":len(result.unexpectedSuccesses)}))
    return 0 if result.wasSuccessful() else 1


if __name__=="__main__":
    sys.exit(main())
