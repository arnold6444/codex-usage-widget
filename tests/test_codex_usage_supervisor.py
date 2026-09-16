import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_usage_supervisor as supervisor


class SupervisorLaunchTests(unittest.TestCase):
    def test_source_mode_uses_current_python_without_a_console_shell(self):
        expected_source = Path(supervisor.__file__).resolve().with_name(
            "codex_usage_widget.py"
        )

        with patch.object(sys, "frozen", False, create=True):
            command = supervisor._widget_command()

        self.assertEqual(command, [sys.executable, str(expected_source)])
        self.assertEqual(supervisor._widget_path(), expected_source)

    def test_packaged_mode_targets_neighboring_widget_exe(self):
        with patch.object(sys, "frozen", True, create=True):
            command = supervisor._widget_command()

        expected = Path(sys.executable).resolve().with_name("CodexUsageWidget.exe")
        self.assertEqual(command, [str(expected)])


if __name__ == "__main__":
    unittest.main()
