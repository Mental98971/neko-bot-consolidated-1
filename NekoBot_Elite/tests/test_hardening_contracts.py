"""Regression contracts that do not require telegram runtime."""
from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TestCatalogIntegrity(unittest.TestCase):
    def test_skins_and_presets_parse_and_align(self):
        skins_dir = PROJECT_ROOT / "data" / "personality" / "skins"
        presets_dir = PROJECT_ROOT / "data" / "personality" / "presets"
        skins = {p.stem for p in skins_dir.glob("*.json")}
        presets = {p.stem for p in presets_dir.glob("*.json")}
        self.assertGreaterEqual(len(skins), 100)
        self.assertGreaterEqual(len(presets), 100)
        self.assertTrue({"neko", "nanora"} <= skins)
        self.assertTrue({"neko", "nanora"} <= presets)
        for path in list(skins_dir.glob("*.json")) + list(presets_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(data, dict)

    def test_fewshot_bank_shape(self):
        path = PROJECT_ROOT / "data" / "personality" / "dataset" / "fewshot_bank.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("examples", data)
        self.assertGreaterEqual(len(data["examples"]), 300)


class TestErrorHygieneSource(unittest.TestCase):
    def test_errors_module_defines_leak_markers(self):
        src = (PROJECT_ROOT / "bot" / "core" / "errors.py").read_text(encoding="utf-8")
        for marker in ("gemini", "groq", "openrouter", "api key", "429"):
            self.assertIn(marker, src.lower())
        self.assertIn("def user_facing_error", src)
        self.assertIn("def _looks_like_internal_leak", src)


class TestMenuContractSource(unittest.TestCase):
    def test_menu_labels_have_panel_keys(self):
        src = (PROJECT_ROOT / "bot" / "plugins" / "start.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        panels = None
        pages = {}
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "MENU_PANELS":
                panels = ast.literal_eval(node.value)
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id in {"MAIN_MENU", "PAGE_2", "PAGE_3", "MENU_PANELS"}:
                        try:
                            pages[t.id] = ast.literal_eval(node.value)
                        except Exception:
                            pass
        self.assertIsInstance(pages.get("MENU_PANELS") or panels, dict)
        panel_keys = set((pages.get("MENU_PANELS") or panels).keys())
        missing = []
        for page_name in ("MAIN_MENU", "PAGE_2", "PAGE_3"):
            for row in pages[page_name]:
                for label in row:
                    key = label.split()[-1].lower()
                    if key not in panel_keys:
                        missing.append((label, key))
        self.assertEqual(missing, [], msg=f"menu labels without panels: {missing}")


class TestPersonaAdminWired(unittest.TestCase):
    def test_superowner_commands_registered(self):
        src = (PROJECT_ROOT / "bot" / "plugins" / "personality.py").read_text(encoding="utf-8")
        for cmd in ("plist", "pshow", "padd", "pdel", "pmerge", "pset", "pdata", "pmeta", "pjson", "preload"):
            self.assertIn(f'"{cmd}"', src, msg=f"missing handler {cmd}")
        self.assertIn("owner_only", src)


class TestMusicJoinNotUsingStreamEnded(unittest.TestCase):
    def test_audio_engine_does_not_pass_stream_audio_ended(self):
        src = (PROJECT_ROOT / "bot" / "services" / "audio_engine.py").read_text(encoding="utf-8")
        self.assertNotIn("stream_type=StreamAudioEnded()", src)
        self.assertIn("def _fresh_audio_url", src)
