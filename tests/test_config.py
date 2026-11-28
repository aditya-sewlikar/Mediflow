"""Configuration tests for the MariaDB environment."""
import unittest

import config


class MariaDbConfigTests(unittest.TestCase):
    def test_default_environment_uses_mariadb_alias(self):
        self.assertEqual(config.config.backend, "mysql")
        self.assertEqual(config.config.driver, "pymysql")

    def test_mariadb_url_maps_to_mysql_adapter(self):
        self.assertEqual(
            config._backend_from_url("mariadb+pymysql://u:p@127.0.0.1:3306/db"),
            ("mysql", "pymysql"),
        )
        self.assertEqual(
            config._backend_from_url("mariadb://u:p@127.0.0.1:3306/db"),
            ("mysql", "pymysql"),
        )

    def test_mariadb_url_parses_connection_parts(self):
        values = config._parse_url("mariadb+pymysql://u:p@127.0.0.1:3307/mediflow?charset=utf8mb4")
        self.assertEqual(values["backend"], "mysql")
        self.assertEqual(values["host"], "127.0.0.1")
        self.assertEqual(values["port"], 3307)
        self.assertEqual(values["database"], "mediflow")
        self.assertEqual(values["user"], "u")
        self.assertEqual(values["password"], "p")
        self.assertEqual(values["charset"], "utf8mb4")


if __name__ == "__main__":
    unittest.main()
