from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from omh.coding import skill_load_observation as observation  # noqa: E402
from omh.coding import skill_load_process  # noqa: E402
from omh.coding._hermes_child_process import (  # noqa: E402
    process_absent,
    terminate_process_group,
)


class SkillLoadProcessCleanupTests(unittest.TestCase):
    def test_setup_failure_after_popen_reaps_child(self) -> None:
        # Given: a real long-lived child and a subscription at post-Popen setup.
        with TemporaryDirectory(prefix="omh-skill-cleanup-") as temporary:
            executable = Path(temporary) / "hermes.py"
            executable.write_text(
                "#!/usr/bin/env python3\nfrom threading import Event\nEvent().wait()\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            setup_entered = Event()
            created = []

            def fail_setup(process):
                created.append(process)
                setup_entered.set()
                raise OSError("synthetic drainer setup failure")

            request = observation.SkillLoadProbeRequest(
                expected_skills=("alpha",),
                hermes=str(executable),
                timeout_seconds=2.0,
                termination_grace_seconds=0.05,
                env={"PATH": "/usr/bin:/bin"},
            )

            # When: setup raises synchronously after the child has been created.
            try:
                with patch.object(skill_load_process, "start_pipe_drainers", fail_setup):
                    payload = observation.probe_skill_load(request, confirmed=True)
                self.assertTrue(setup_entered.wait(1.0))

                # Then: the fail-closed result is returned only after child reap.
                self.assertEqual(payload["reason_code"], "inventory_process_error")
                self.assertEqual(len(created), 1)
                self.assertTrue(process_absent(created[0].pid))
                self.assertIsNotNone(created[0].returncode)
            finally:
                for process in created:
                    if process.poll() is None:
                        terminate_process_group(process, 0.05, 15)


if __name__ == "__main__":
    unittest.main()
