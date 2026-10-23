"""Local scaffold checks. These tests never provision or configure a real VM."""
import importlib.util
from pathlib import Path
import tempfile
import unittest


HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


config = load("configure")
backup = load("backup_daily")


class VMScaffoldTests(unittest.TestCase):
    def test_hostname_accepts_real_dns_syntax_and_rejects_urls_injection_and_placeholders(self):
        self.assertEqual(config.hostname("203-0-113-5.sslip.io"), "203-0-113-5.sslip.io")
        self.assertEqual(config.hostname("DESK.personal-domain.in"), "desk.personal-domain.in")
        for value in ("", "localhost", "example.com", "desk.invalid", "127.0.0.1", "https://desk.host.in",
                      "desk.host.in:443", "desk.host.in/path", "desk.host.in\nadmin off", "*.host.in", "-bad.host.in"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                config.hostname(value)

    def test_staging_requires_built_source_and_rejects_private_files_or_escaping_paths(self):
        with tempfile.TemporaryDirectory(prefix=".test-vm-", dir=HERE.parents[1]) as temporary:
            base = Path(temporary).resolve() / "app"
            release = base / "releases" / "v1"
            required = ("backend/app/main.py", "backend/requirements.txt", "scripts/backup.py",
                        "frontend/dist/index.html", "deploy/vm/install.sh", "deploy/vm/backup_daily.py")
            for relative in required:
                path = release / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("safe source", encoding="utf-8")
            (release / "frontend/dist/assets").mkdir()
            self.assertEqual(config.validate_release(release, base), release)
            private = release / "frontend/.env.production"
            private.write_text("PRIVATE=do-not-stage", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Private configuration"):
                config.validate_release(release, base)
            with self.assertRaises(ValueError):
                config.validate_release(base.parent, base)

    def test_existing_files_are_reused_exactly_and_never_replaced(self):
        with tempfile.TemporaryDirectory(prefix=".test-vm-", dir=HERE.parents[1]) as temporary:
            path = Path(temporary) / "config"
            config.write_once(path, "original")
            config.write_once(path, "original")
            with self.assertRaisesRegex(ValueError, "Existing configuration differs"):
                config.write_once(path, "replacement")
            self.assertEqual(path.read_text(encoding="utf-8"), "original")

    def test_production_template_has_only_exact_https_origins_and_persistent_sqlite(self):
        text = (HERE / "app.env.example").read_text(encoding="utf-8")
        text = text.replace("REQUIRED_PUBLIC_HOSTNAME", "desk.personal-domain.in")
        config.validate_env(text, "desk.personal-domain.in")
        for unsafe in (text.replace("COOKIE_SECURE=true", "COOKIE_SECURE=false"),
                       text.replace("https://desk.personal-domain.in", "*"),
                       text.replace("sqlite:////var/lib/personal-options-trader/trader.db", "postgresql://private")):
            with self.assertRaises(ValueError):
                config.validate_env(unsafe, "desk.personal-domain.in")

    def test_backup_limits_refuse_new_copies_without_deleting_archives(self):
        backup.check_capacity([20, 30], 10, 1000, 3, 100, 100)
        for args in (([20, 30], 10, 1000, 2, 100, 100),
                     ([20, 30], 51, 1000, 3, 100, 100),
                     ([20, 30], 10, 105, 3, 100, 100)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                backup.check_capacity(*args)

    def test_service_has_one_worker_loopback_proxy_trust_and_private_environment(self):
        unit = (HERE / "personal-options-trader.service").read_text(encoding="utf-8")
        self.assertIn("User=personal-options-trader", unit)
        self.assertIn("--host 127.0.0.1 --port 8000 --workers 1", unit)
        self.assertIn("--forwarded-allow-ips=127.0.0.1", unit)
        self.assertNotIn("--forwarded-allow-ips=*", unit)
        self.assertIn("Environment=PYTHON_DOTENV_DISABLED=1", unit)
        self.assertIn("EnvironmentFile=/etc/personal-options-trader/app.env", unit)

    def test_installer_keeps_restrictive_umask_and_activation_explicit(self):
        script = (HERE / "install.sh").read_text(encoding="utf-8")
        self.assertIn("umask 077", script)
        self.assertIn("activate=false", script)
        self.assertIn('if [[ "$activate" == true ]]', script)
        self.assertNotIn("rm -", script)
        for path in HERE.iterdir():
            if path.is_file() and path.suffix in {".sh", ".py", ".service", ".timer", ".template", ".example"}:
                self.assertNotIn(b"\r", path.read_bytes(), f"Linux scaffold must use LF: {path.name}")


if __name__ == "__main__":
    unittest.main()
