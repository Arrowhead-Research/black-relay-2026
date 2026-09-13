"""Credential-free assertions for the accepted Phase 6 completion boundary."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
ROADMAP = (ROOT / "ARCHITECTURE_ROADMAP.md").read_text(encoding="utf-8")
AGENTS = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def phase(number, next_number):
    start = ROADMAP.index(f"### Phase {number}:")
    end = ROADMAP.index(f"### Phase {next_number}:", start)
    return ROADMAP[start:end]


class Phase6CompletionTests(unittest.TestCase):
    def test_phase6_is_complete_and_phase7_is_next(self):
        phase6 = phase(6, 7)
        phase7 = phase(7, 8)
        self.assertIn("Status: complete.", phase6)
        self.assertIn("Exit criteria achieved:", phase6)
        self.assertIn("Status: next.", phase7)
        self.assertIn("Phases 1 through 6 are complete", AGENTS)
        self.assertIn("Phases 1 through 6 are complete", README)

    def test_deferred_work_has_an_explicit_phase(self):
        phase8 = phase(8, 9)
        phase11 = phase(11, 12)
        self.assertIn("Install pinned CrowdSec", phase8)
        self.assertIn("pending-reboot and disk-pressure", phase11)

    def test_phase6_runbooks_record_operator_completion(self):
        for name in (
            "phase6-access-bootstrap.md",
            "phase6-host-baseline.md",
            "phase6-host-firewall.md",
        ):
            runbook = (ROOT / "docs" / "runbooks" / name).read_text(
                encoding="utf-8"
            )
            self.assertIn("Status: complete.", runbook, name)


if __name__ == "__main__":
    unittest.main()
