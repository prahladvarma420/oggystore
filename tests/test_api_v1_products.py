import importlib.util
import unittest


if importlib.util.find_spec("flask") is not None:
    from tests.api_v1_products_http_suite import ApiV1ProductsTests  # noqa: F401
else:
    class ApiV1ProductsTests(unittest.TestCase):
        @unittest.skip("Flask is not installed in this validation environment")
        def test_http_products_suite_requires_flask(self):
            pass
