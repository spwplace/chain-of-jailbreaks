import importlib.util
import tempfile
import unittest
from pathlib import Path

from corg_eval.planner import corridor_from_plan, load_plan, save_plan, validate_plan
from corg_eval.smt import generate_smt_plan
from corg_eval.tactics import TACTICS


HAS_GENERATOR_DEPS = importlib.util.find_spec("z3") is not None and importlib.util.find_spec("jinja2") is not None


@unittest.skipUnless(HAS_GENERATOR_DEPS, "generator extras are not installed")
class SmtPlannerTests(unittest.TestCase):
    def test_smt_plan_renders_corridor(self):
        plan = generate_smt_plan(depth=5, seed=17, objective="mixed")
        validate_plan(plan)
        corridor = corridor_from_plan(plan)
        self.assertEqual(len(corridor.capsules), 5)
        self.assertEqual(corridor.metadata["lure_mode"], "planned")
        self.assertTrue(any(capsule.drift_markers for capsule in corridor.capsules))
        self.assertTrue(all(capsule.lure for capsule in corridor.capsules))
        pressured_fields = {
            capsule.target_field
            for capsule in plan.capsules
            if TACTICS[capsule.tactic].drift_capable
        }
        self.assertTrue(set(plan.metadata["required_drift_fields"]).issubset(pressured_fields))

    def test_plan_json_roundtrip(self):
        plan = generate_smt_plan(depth=3, seed=4, objective="authority")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            save_plan(plan, path)
            self.assertEqual(load_plan(path).to_dict(), plan.to_dict())


if __name__ == "__main__":
    unittest.main()
