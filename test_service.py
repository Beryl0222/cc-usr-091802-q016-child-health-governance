"""核对基础服务和领域样例。"""

import json
import unittest
from pathlib import Path

from service import SERVICE_ID, health_payload


class BaselineContractTest(unittest.TestCase):
    def test_service_identity(self):
        self.assertEqual(health_payload()["service"], SERVICE_ID)

    def test_fixture_matches_project(self):
        data = json.loads(Path("fixtures/sample.json").read_text(encoding="utf-8"))
        self.assertEqual(data["service"], SERVICE_ID)
        self.assertIsInstance(data["context"], dict)
        self.assertTrue(data["context"])


if __name__ == "__main__":
    unittest.main()
