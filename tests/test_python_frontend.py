import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

WORKDIR = Path(__file__).resolve().parents[1]
PY_FRONTEND_DIR = WORKDIR / "python_frontend"
if str(PY_FRONTEND_DIR) not in sys.path:
    sys.path.insert(0, str(PY_FRONTEND_DIR))

from app import create_app


class PythonFrontendTests(unittest.TestCase):
    def test_index_injects_backend_url_and_static_assets(self):
        client = TestClient(create_app(backend_url="http://127.0.0.1:8015"))

        response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("/static/app.css", response.text)
        self.assertIn("/static/app.js", response.text)
        self.assertIn("http://127.0.0.1:8015", response.text)
        self.assertIn("Easy Claude", response.text)

    def test_health_endpoint(self):
        client = TestClient(create_app())

        response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
