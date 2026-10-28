"""Shell scripts the docs tell you to run directly (`sudo ./deploy/upgrade.sh`,
`sudo deploy/backup/install-restic-backup.sh ...`, `deploy/deploy-hub.sh <ref>`) must be executable in the repo.

sudo runs only an executable file and says "command not found" otherwise: install-restic-backup.sh, deploy-hub.sh
and upgrade.sh were committed 0644, so the documented commands failed (2026-10-27, refreshing the backup's avm for
F-05). File modes, not git's index: the deploy runs this suite on a `git archive`, which keeps the modes.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def scripts():
    return sorted(ROOT.glob("deploy/**/*.sh")) + [ROOT / "tests" / "run.sh"]


def test_scripts_are_found():
    names = {p.name for p in scripts()}
    assert {"deploy-hub.sh", "upgrade.sh", "install-server.sh", "install-restic-backup.sh", "run.sh"} <= names


def test_shell_scripts_are_executable():
    not_x = [str(p.relative_to(ROOT)) for p in scripts() if not os.access(p, os.X_OK)]
    assert not not_x, f"not executable (chmod +x, commit the mode): {not_x}"


def test_shell_scripts_have_a_bash_shebang():
    bad = [str(p.relative_to(ROOT)) for p in scripts() if not p.read_text().startswith("#!/usr/bin/env bash")
           and not p.read_text().startswith("#!/bin/bash")]
    assert not bad, f"no bash shebang (an executable script runs with it): {bad}"
