import tempfile
import unittest
from pathlib import Path

from corg_eval.corridor import export_nested_zip, generate_corridor, load_corridor, save_corridor


class CorridorTests(unittest.TestCase):
    def test_generate_is_deterministic(self):
        a = generate_corridor(depth=4, seed=123)
        b = generate_corridor(depth=4, seed=123)
        self.assertEqual(a.to_dict(), b.to_dict())
        self.assertEqual(len(a.capsules), 4)

    def test_json_roundtrip(self):
        corridor = generate_corridor(depth=2, seed=5)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "corridor.json"
            save_corridor(corridor, path)
            self.assertEqual(load_corridor(path).to_dict(), corridor.to_dict())

    def test_zip_export_smoke(self):
        corridor = generate_corridor(depth=1, seed=9)
        with tempfile.TemporaryDirectory() as tmp:
            path = export_nested_zip(corridor, Path(tmp) / "corridor.zip")
            self.assertTrue(path.exists())
            self.assertGreater(path.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()

