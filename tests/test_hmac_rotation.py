"""deal-nudge mints deal-update-form links with HMAC_SECRET only; an empty key
sends nothing. Fake keys only; no AWS, Pipeline or email."""
import base64
import hashlib
import hmac
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("FORM_LAMBDA_URL", "https://form.example/")
os.environ.setdefault("NUDGE_KEY", "test-nudge-key")
os.environ.setdefault("HMAC_SECRET", "test-hmac-key")
if "boto3" not in sys.modules:
    try:
        import boto3  # noqa: F401
    except ImportError:
        sys.modules["boto3"] = types.SimpleNamespace(client=lambda *a, **k: None)
import lambda_function as lf  # noqa: E402

DEAL = {"id": 9, "company": {"name": "Acme"},
        "primary_contact": {"id": 7, "email": "client@example.com", "first_name": "Cy"}}


def sign(key, v):
    return base64.urlsafe_b64encode(hmac.new(key.encode(), str(v).encode(), hashlib.sha256).digest()).decode().rstrip("=")


def event():
    return {"requestContext": {"http": {"method": "POST"}},
            "queryStringParameters": {"key": lf.NUDGE_KEY, "deal_id": "9"}}


class Nudge(unittest.TestCase):
    def setUp(self):
        mock.patch.object(lf, "get_jwt", return_value="jwt").start()
        mock.patch.object(lf, "pipeline_get", return_value=(200, dict(DEAL))).start()
        mock.patch.object(lf, "recently_nudged", return_value=None).start()
        mock.patch.object(lf, "write_lock").start()
        mock.patch.object(lf, "TEST_EMAIL_OVERRIDE", "").start()
        self.send = mock.patch.object(lf, "send_email").start()
        self.addCleanup(mock.patch.stopall)

    def test_link_signed_with_current_key(self):
        with mock.patch.object(lf, "HMAC_SECRET", "test-new-hmac"):
            r = lf.lambda_handler(event(), None)
        self.assertEqual(r["statusCode"], 200)
        link = self.send.call_args[0][2]
        self.assertTrue(link.endswith("token=" + sign("test-new-hmac", 9)))
        self.assertNotIn(sign("test-old-hmac", 9), link)

    def test_empty_key_sends_nothing(self):
        with mock.patch.object(lf, "HMAC_SECRET", ""):
            self.assertEqual(lf.make_token(9), "")
            r = lf.lambda_handler(event(), None)
        self.assertEqual(r["statusCode"], 503)
        self.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
