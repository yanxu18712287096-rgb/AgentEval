import copy
import unittest
from client.config import resolve
from client.request import build_request
from client.service import DEFAULTS, prepare


class Requirements(unittest.TestCase):
    def test_R1_precedence(self):
        config = resolve(DEFAULTS, {"retries": 7, "label": "file"},
                         {"APP_RETRIES": "2", "APP_CACHE": "false", "APP_LABEL": "env"},
                         {"retries": 0, "label": ""})
        self.assertEqual((config.retries, config.cache, config.label), (0, False, ""))
        self.assertEqual(resolve(DEFAULTS, {"retries": 0}, {}, {}).retries, 0)

    def test_R2_env_validation(self):
        for value in ("-1", "1.5", "bad", "", "+2", " 2", "٢"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve(DEFAULTS, {}, {"APP_RETRIES": value}, {})
        for value in ("yes", "0", "", " false "):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve(DEFAULTS, {}, {"APP_CACHE": value}, {})
        self.assertFalse(resolve(DEFAULTS, {}, {"APP_CACHE": "FaLsE"}, {}).cache)
        self.assertTrue(resolve(DEFAULTS, {}, {"APP_CACHE": "TRUE"}, {}).cache)
        self.assertEqual(resolve(DEFAULTS, {}, {"APP_LABEL": " a ", "OTHER": "x"}, {}).label, " a ")

    def test_R3_caller_and_immutability(self):
        args = [dict(DEFAULTS), {"cache": False}, {"APP_RETRIES": "0"}, {"label": ""}]
        original = copy.deepcopy(args)
        config = resolve(*args)
        self.assertEqual(args, original)
        self.assertEqual(build_request(config, "/x"), {"url": "/x", "retries": 0, "cache": False, "label": ""})
        self.assertEqual(prepare("/x", *args[1:]), build_request(config, "/x"))

    def test_R4_defaults(self):
        self.assertEqual(prepare("/old"), {"url": "/old", "retries": 3, "cache": True, "label": "default"})


if __name__ == "__main__":
    unittest.main()
