from __future__ import annotations

import ast
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from v2.config import ROOT, Config
from v2.domain.catalog import Catalog
from v2.platform_api.client import PlatformClient


class StandaloneTests(unittest.TestCase):
    def test_runtime_and_utilities_have_no_legacy_imports(self):
        sources = [*(ROOT / "v2").rglob("*.py"), *(ROOT / "scripts").glob("*.py")]
        for path in sources:
            with self.subTest(path=str(path.relative_to(ROOT))):
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
                    if isinstance(node, ast.ImportFrom):
                        names.append(node.module or "")
                    self.assertFalse(any(name == "src" or name.startswith("src.") for name in names))

    def test_catalog_and_manifest_are_self_contained(self):
        self.assertTrue(Catalog.load().providers)
        manifest = Config().manifest()
        self.assertEqual(len(manifest["code_sha256"]), 64)
        self.assertFalse(manifest["voice_clone"])

    def test_external_catalog_is_fingerprinted_without_exposing_its_path(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "clinic.json"
            path.write_text('{"clinic": {}}', encoding="utf-8")
            manifest = replace(Config(), catalog_path=path).manifest()
            self.assertEqual(len(manifest["code_sha256"]), 64)
            self.assertNotIn(folder, str(manifest))

    def test_launchers_and_container_only_start_v2(self):
        for name in ("run.ps1", "run.sh"):
            content = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("-m v2", content)
            self.assertNotIn("src.main", content)
        self.assertNotIn("COPY src", (ROOT / "v2" / "Dockerfile").read_text(encoding="utf-8"))


class PlatformConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_platform_client_uses_injected_server_configuration(self):
        config = replace(Config(), api_key="test-clinic-key", api_base_url="https://clinic.example.test/api/v1")
        client = PlatformClient(config=config)
        try:
            self.assertEqual(client._client.headers["X-Api-Key"], "test-clinic-key")
            self.assertEqual(str(client._client.base_url), "https://clinic.example.test/api/v1/")
            self.assertNotIn("test-clinic-key", repr(config))
        finally:
            await client.aclose()


if __name__ == "__main__":
    unittest.main()
