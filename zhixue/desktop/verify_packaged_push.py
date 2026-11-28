"""Run the push receipt regressions against modules embedded in a packaged service."""
from argparse import ArgumentParser
from hashlib import file_digest
from importlib.abc import Loader, MetaPathFinder
from importlib.util import spec_from_loader
import json
from pathlib import Path
import sys

from PyInstaller.archive.readers import CArchiveReader
import pytest


PROJECT = Path(__file__).resolve().parents[1]
TEST = "tests/test_mobile_push.py::test_late_push_completion_for_a_retired_target_releases_reminder"
CASES = ("unsubscribe-accepted", "replacement-uncertain")
MODULES = ("backend.mobile_push", "backend.mobile_pairing")


class PackagedModules(MetaPathFinder, Loader):
    def __init__(self, service):
        archive = CArchiveReader(str(service))
        payload, = (name for name, entry in archive.toc.items() if entry[-1] == "z")
        modules = archive.open_embedded_archive(payload)
        self.code = {name: modules.extract(name) for name in MODULES}
        self.loaded = set()

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.code:
            return spec_from_loader(fullname, self)
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        name = module.__spec__.name
        exec(self.code[name], module.__dict__)
        self.loaded.add(name)


class Results:
    def __init__(self):
        self.passed = set()

    def pytest_runtest_logreport(self, report):
        if report.when == "call" and report.passed:
            self.passed.add(report.nodeid)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("service", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    service = args.service.resolve(strict=True)
    with service.open("rb") as file:
        service_sha = file_digest(file, "sha256").hexdigest()
    modules = PackagedModules(service)
    results = Results()
    cases = {case: f"{TEST}[{case}]" for case in CASES}
    sys.path.insert(0, str(PROJECT))
    sys.meta_path.insert(0, modules)
    try:
        exit_code = pytest.main(["-q", "-p", "no:cacheprovider", *cases.values()], plugins=[results])
    finally:
        sys.meta_path.remove(modules)
    with service.open("rb") as file:
        if file_digest(file, "sha256").hexdigest() != service_sha:
            raise RuntimeError("Packaged service changed during the push receipt test")
    if modules.loaded != set(MODULES):
        raise RuntimeError(f"Did not execute both packaged modules: {sorted(modules.loaded)}")
    passed = [case for case in CASES if cases[case] in results.passed]
    with args.report.open("x", encoding="utf-8") as report:
        json.dump({"serviceSHA256": service_sha, "passed": passed}, report, indent=2)
        report.write("\n")
    return int(exit_code) or (0 if tuple(passed) == CASES else 1)


if __name__ == "__main__":
    raise SystemExit(main())

