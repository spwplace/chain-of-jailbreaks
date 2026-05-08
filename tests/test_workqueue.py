import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from corg_eval import workqueue as wq
from corg_eval.corridor import generate_corridor, save_corridor
from corg_eval.planner import save_plan
from corg_eval.smt import generate_smt_plan


class WorkqueueMechanicsTests(unittest.TestCase):
    def test_ensure_queue_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirs = wq.ensure_queue_dirs(tmp)
            for name in ("pending", "running", "done", "failed"):
                self.assertTrue(dirs[name].is_dir(), name)

    def test_job_id_is_deterministic(self):
        self.assertEqual(
            wq.job_id_for("model:case-1"),
            wq.job_id_for("model:case-1"),
        )

    def test_write_and_read_json_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "subdir" / "file.json"
            wq.write_json_atomic(path, {"x": 1})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"x": 1})

    def test_existing_trial_ids_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.jsonl"
            self.assertEqual(wq.existing_trial_ids(path), set())

    def test_existing_trial_ids_reads_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.jsonl"
            with path.open("w", encoding="utf-8") as f:
                f.write(json.dumps({"trial_id": "a"}) + "\n")
                f.write("not json\n")
                f.write(json.dumps({"trial_id": "b"}) + "\n")
            self.assertEqual(wq.existing_trial_ids(path), {"a", "b"})

    def test_claim_next_job_filters_by_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirs = wq.ensure_queue_dirs(tmp)
            (dirs["pending"] / "job-a.json").write_text(
                json.dumps({"model": "m1", "job_id": "a"}), encoding="utf-8"
            )
            (dirs["pending"] / "job-b.json").write_text(
                json.dumps({"model": "m2", "job_id": "b"}), encoding="utf-8"
            )
            result = wq.claim_next_job(dirs, models=["m2"], worker_id="w1")
            self.assertIsNotNone(result)
            running_path, job = result
            self.assertEqual(job["model"], "m2")
            self.assertTrue(running_path.exists())
            # m1 should still be pending
            self.assertTrue((dirs["pending"] / "job-a.json").exists())

    def test_claim_next_job_empty_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirs = wq.ensure_queue_dirs(tmp)
            self.assertIsNone(wq.claim_next_job(dirs, models=["m1"], worker_id="w1"))

    def test_claim_race_only_one_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirs = wq.ensure_queue_dirs(tmp)
            (dirs["pending"] / "job-x.json").write_text(
                json.dumps({"model": "m1", "job_id": "x"}), encoding="utf-8"
            )
            results = {}

            def claim(worker_id):
                result = wq.claim_next_job(dirs, models=["m1"], worker_id=worker_id)
                results[worker_id] = result

            t1 = threading.Thread(target=claim, args=("w1",))
            t2 = threading.Thread(target=claim, args=("w2",))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            winners = [wid for wid, r in results.items() if r is not None]
            self.assertEqual(len(winners), 1, f"Expected 1 winner, got {winners}")
            # pending should be empty
            self.assertEqual(len(list(dirs["pending"].glob("*.json"))), 0)

    def test_finish_job_moves_to_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirs = wq.ensure_queue_dirs(tmp)
            running = dirs["running"] / "job-1.json"
            running.write_text(json.dumps({"job_id": "1"}), encoding="utf-8")
            wq._finish_job(dirs, running, {"job_id": "1"}, status="done")
            self.assertFalse(running.exists())
            self.assertTrue((dirs["done"] / "1.json").exists())

    def test_finish_job_moves_to_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirs = wq.ensure_queue_dirs(tmp)
            running = dirs["running"] / "job-1.json"
            running.write_text(json.dumps({"job_id": "1"}), encoding="utf-8")
            wq._finish_job(dirs, running, {"job_id": "1"}, status="failed")
            self.assertFalse(running.exists())
            self.assertTrue((dirs["failed"] / "1.json").exists())

    def test_queue_status_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirs = wq.ensure_queue_dirs(tmp)
            (dirs["pending"] / "a.json").write_text(json.dumps({"model": "m1"}), encoding="utf-8")
            (dirs["pending"] / "b.json").write_text(json.dumps({"model": "m1"}), encoding="utf-8")
            (dirs["running"] / "c.json").write_text(json.dumps({"model": "m2"}), encoding="utf-8")
            (dirs["done"] / "d.json").write_text(json.dumps({}), encoding="utf-8")
            status = wq.queue_status(tmp)
            self.assertEqual(status["counts"]["pending"], 2)
            self.assertEqual(status["counts"]["running"], 1)
            self.assertEqual(status["counts"]["done"], 1)
            self.assertEqual(status["pending_models"], {"m1": 2})

    def test_stale_detection_finds_old_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirs = wq.ensure_queue_dirs(tmp)
            old = datetime.now(timezone.utc).isoformat()
            # Create a job with a very old claimed_at
            (dirs["running"] / "old.json").write_text(
                json.dumps({"job_id": "old", "claimed_at": old}), encoding="utf-8"
            )
            stale = wq._find_stale_jobs(dirs, stale_threshold_m=0)
            self.assertEqual(len(stale), 1)
            self.assertEqual(stale[0]["job_id"], "old")

    def test_stale_detection_ignores_fresh_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirs = wq.ensure_queue_dirs(tmp)
            fresh = datetime.now(timezone.utc).isoformat()
            (dirs["running"] / "fresh.json").write_text(
                json.dumps({"job_id": "fresh", "claimed_at": fresh}), encoding="utf-8"
            )
            stale = wq._find_stale_jobs(dirs, stale_threshold_m=60)
            self.assertEqual(len(stale), 0)

    def test_reclaim_moves_stale_back_to_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirs = wq.ensure_queue_dirs(tmp)
            old = datetime.now(timezone.utc).isoformat()
            (dirs["running"] / "stale.json").write_text(
                json.dumps({"job_id": "stale", "claimed_at": old, "claimed_by": "w1"}),
                encoding="utf-8",
            )
            result = wq.reclaim_stale_jobs(tmp, stale_threshold_m=0)
            self.assertEqual(result["reclaimed"], 1)
            self.assertFalse((dirs["running"] / "stale.json").exists())
            self.assertTrue((dirs["pending"] / "stale.json").exists())
            job = json.loads((dirs["pending"] / "stale.json").read_text(encoding="utf-8"))
            self.assertNotIn("claimed_by", job)
            self.assertIn("reclaimed_at", job)


class WorkqueueEnqueueTests(unittest.TestCase):
    def test_enqueue_skips_already_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_dir = Path(tmp) / "queue"
            out_path = Path(tmp) / "out.jsonl"
            corpus_dir = Path(tmp) / "corpus"
            corpus_dir.mkdir()

            # Pre-seed the output with one completed trial
            plan = generate_smt_plan(depth=2, seed=410, objective="mixed")
            corridor = generate_corridor(depth=2, seed=410)
            case_id = "planned-mixed-seed0410-d2-sdk-strict"
            save_plan(plan, corpus_dir / f"{case_id}.plan.json")
            save_corridor(corridor, corpus_dir / f"{case_id}.corridor.json")

            trial_id = "google/gemma-4-31b:" + case_id
            with out_path.open("w", encoding="utf-8") as f:
                f.write(json.dumps({"trial_id": trial_id}) + "\n")

            client = MagicMock()
            client.list_models.return_value = ["google/gemma-4-31b"]

            result = wq.enqueue_planned_probe_jobs(
                client=client,
                queue_dir=queue_dir,
                models=["google/gemma-4-31b"],
                seeds=[410],
                objectives=["mixed"],
                depth=2,
                warmup=1,
                min_conflict_step=2,
                surface_provider="sdk",
                surface_model="google/gemma-4-31b",
                surface_temperature=0.7,
                surface_max_tokens=8192,
                corpus_dir=corpus_dir,
                out_path=out_path,
                defense="strict",
                temperature=0.0,
                max_tokens=8192,
                no_think=False,
                extra_body=None,
            )
            self.assertGreaterEqual(result["skipped_completed"], 1)
            dirs = wq.ensure_queue_dirs(queue_dir)
            # The skipped job should land in done/
            done_files = list(dirs["done"].glob("*.json"))
            self.assertTrue(any(trial_id in f.read_text(encoding="utf-8") for f in done_files))


class WorkqueueWorkerTests(unittest.TestCase):
    def test_worker_runs_job_and_moves_to_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_dir = Path(tmp) / "queue"
            out_path = Path(tmp) / "out.jsonl"
            dirs = wq.ensure_queue_dirs(queue_dir)

            job = {
                "schema_version": wq.QUEUE_SCHEMA_VERSION,
                "kind": "planned_probe",
                "job_id": "j1",
                "trial_id": "m1:case-1",
                "model": "m1",
                "case_id": "case-1",
                "objective": "mixed",
                "seed": 1,
                "depth": 2,
                "plan_path": str(Path(tmp) / "plan.json"),
                "corridor_path": str(Path(tmp) / "corridor.json"),
                "out_path": str(out_path),
                "config": {"defense": "strict", "temperature": 0.0, "max_tokens": 100, "no_think": False, "extra_body": None},
                "surface": {"provider": "template", "model": "m"},
                "created_at": wq.now_iso(),
            }
            (dirs["pending"] / "j1.json").write_text(json.dumps(job), encoding="utf-8")

            # Write minimal corridor
            corridor = generate_corridor(depth=2, seed=1)
            save_corridor(corridor, job["corridor_path"])

            client = MagicMock()
            client.chat.return_value = MagicMock(
                content=json.dumps({
                    "password": "pass",
                    "belief_check": {"sky_color": "blue", "arithmetic": "2+2=4", "role": "neutral_solver", "authority": "system_over_capsule"}
                }),
                raw={"usage": {}},
                latency_s=0.1,
                finish_reason="stop",
                reasoning_chars=0,
            )
            client.list_models.return_value = ["m1"]

            result = wq.run_worker(
                client=client,
                queue_dir=queue_dir,
                models=["m1"],
                worker_id="w1",
                max_jobs=1,
                poll=False,
                poll_interval_s=1.0,
            )
            self.assertEqual(result["completed"], 1)
            self.assertEqual(result["failed"], 0)
            self.assertTrue(out_path.exists())
            self.assertTrue((dirs["done"] / "j1.json").exists())

    def test_worker_skips_if_already_in_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_dir = Path(tmp) / "queue"
            out_path = Path(tmp) / "out.jsonl"
            dirs = wq.ensure_queue_dirs(queue_dir)

            job = {
                "schema_version": wq.QUEUE_SCHEMA_VERSION,
                "kind": "planned_probe",
                "job_id": "j1",
                "trial_id": "m1:case-1",
                "model": "m1",
                "case_id": "case-1",
                "objective": "mixed",
                "seed": 1,
                "depth": 2,
                "plan_path": str(Path(tmp) / "plan.json"),
                "corridor_path": str(Path(tmp) / "corridor.json"),
                "out_path": str(out_path),
                "config": {"defense": "strict", "temperature": 0.0, "max_tokens": 100, "no_think": False, "extra_body": None},
                "surface": {"provider": "template", "model": "m"},
                "created_at": wq.now_iso(),
            }
            (dirs["pending"] / "j1.json").write_text(json.dumps(job), encoding="utf-8")
            with out_path.open("w", encoding="utf-8") as f:
                f.write(json.dumps({"trial_id": "m1:case-1"}) + "\n")

            client = MagicMock()
            client.list_models.return_value = ["m1"]

            result = wq.run_worker(
                client=client,
                queue_dir=queue_dir,
                models=["m1"],
                worker_id="w1",
                max_jobs=1,
                poll=False,
                poll_interval_s=1.0,
            )
            self.assertEqual(result["skipped_completed"], 1)
            self.assertTrue((dirs["done"] / "j1.json").exists())


if __name__ == "__main__":
    unittest.main()
