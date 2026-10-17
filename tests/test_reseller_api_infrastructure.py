import importlib.util
import pathlib
import unittest
from unittest.mock import Mock, patch


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME_DEPENDENCIES = ("flask", "werkzeug", "dotenv")
RUNTIME_AVAILABLE = all(importlib.util.find_spec(name) for name in RUNTIME_DEPENDENCIES)

if RUNTIME_AVAILABLE:
    from reseller_api import create_app
    from reseller_api.config import validate_database_config
else:
    create_app = None
    validate_database_config = None


class ResellerApiInfrastructureStaticTests(unittest.TestCase):
    def test_declared_entrypoint_and_routes(self):
        app_source = (PROJECT_ROOT / "reseller_api/app.py").read_text()
        route_source = (PROJECT_ROOT / "reseller_api/routes/health.py").read_text()
        self.assertIn("app = create_app()", app_source)
        self.assertIn('host="0.0.0.0"', app_source)
        self.assertIn('os.getenv("PORT", "5001")', app_source)
        self.assertIn('"/health"', route_source)
        self.assertIn('"/health/db"', route_source)

    def test_product_and_balance_routes_without_future_order_routes(self):
        route_sources = "\n".join(
            path.read_text()
            for path in (PROJECT_ROOT / "reseller_api/routes").rglob("*.py")
        )
        self.assertIn('"/products"', route_sources)
        self.assertIn('"/products/<string:product_id>"', route_sources)
        self.assertIn('"/balance"', route_sources)
        # Phases 7-11 added these routes intentionally; keep them pinned.
        for route in ("/orders", "/webhooks", "/my-products", "/pricing"):
            self.assertIn(route, route_sources)

    def test_existing_procfile_commands_are_unchanged(self):
        self.assertEqual(
            (PROJECT_ROOT / "Procfile").read_text(),
            "web: gunicorn --workers 1 --threads 8 --timeout 120 -b 0.0.0.0:$PORT web_admin.app:app\n"
            "worker: python bot.py\n"
            "webhook-worker: python -m reseller_api.workers.webhook_worker\n"
            "fulfillment-worker: python -m reseller_api.workers.fulfillment_worker\n"
            "reseller-deposit-worker: python -m reseller_api.workers.reseller_deposit_worker\n",
        )


@unittest.skipUnless(RUNTIME_AVAILABLE, "Flask/Werkzeug/python-dotenv runtime unavailable")
class ResellerApiInfrastructureRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({
            "TESTING": True,
            "INIT_DATABASE": False,
            "REQUIRE_POSTGRES": False,
            "DATABASE_BACKEND": "sqlite",
        })
        self.client = self.app.test_client()

    def test_lightweight_health_endpoint(self):
        with patch("reseller_api.middleware.request_context.persist_audit_record") as persist:
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "service": "reseller-api", "status": "ok"
        })
        persist.assert_not_called()
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    def test_database_health_success_is_sanitized(self):
        cursor = Mock()
        cursor.fetchone.return_value = (1,)
        connection = Mock()
        connection.cursor.return_value = cursor
        with patch("database.db.get_conn", return_value=connection):
            response = self.client.get("/health/db")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ok")
        cursor.execute.assert_called_once_with("SELECT 1")
        cursor.close.assert_called_once()

    def test_database_health_failure_is_sanitized(self):
        with patch("database.db.get_conn", side_effect=RuntimeError("postgresql://secret")):
            response = self.client.get("/health/db")
        self.assertEqual(response.status_code, 503)
        body = response.get_data(as_text=True)
        self.assertIn("unavailable", body)
        self.assertNotIn("secret", body)
        self.assertNotIn("postgresql", body)

    def test_v1_routes_are_initialized_but_still_protected(self):
        rules = {rule.rule for rule in self.app.url_map.iter_rules()}
        self.assertIn("/api/v1/", rules)
        self.assertIn("/api/v1/products", rules)
        self.assertIn("/api/v1/products/<string:product_id>", rules)
        self.assertIn("/api/v1/balance", rules)
        # Phases 7-11 routes exist and remain auth-protected (verified below).
        self.assertIn("/api/v1/orders", rules)
        self.assertIn("/api/v1/webhooks", rules)
        self.assertIn("/api/v1/my-products", rules)
        self.assertIn("/api/v1/pricing", rules)
        with patch(
            "reseller_api.middleware.security_controls.is_global_api_enabled",
            return_value=True,
        ):
            response = self.client.get("/api/v1/")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"]["code"], "authentication_failed")

    def test_postgres_requirement_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "requires a PostgreSQL DATABASE_URL"):
            validate_database_config({
                "REQUIRE_POSTGRES": True,
                "DATABASE_BACKEND": "sqlite",
            })
        validate_database_config({
            "REQUIRE_POSTGRES": True,
            "DATABASE_BACKEND": "postgresql",
        })


if __name__ == "__main__":
    unittest.main()
