"""New DAG construction and solver-free CPM tests; no network calls."""
import unittest
import itertools
from copy import deepcopy
from pathlib import Path

from scripts.maintenance.pipeline import aggregate, make_prompt, read_json, schedule, validate_response
from scripts.maintenance.resource_dag import optimize_dag, resource_pairs, reachability
from scripts.maintenance.test_fixtures import dataset


def response(data, physical=None, resource=None):
    physical, resource = physical or {}, resource or {}
    return {"timeline": {"relation_schema": 2, "task_conditions": [
                         {"task": t["id"], "preconditions": ["Synthetic start condition"],
                          "postconditions": ["Synthetic finish state"], "uncertainties": [],
                          "basis": "hypothesis"} for t in data["tasks"]], "summary": "Synthetic test timeline",
                         "stages": [{"id": "S1", "tasks": [t["id"] for t in data["tasks"]],
                                     "description": "Candidate branches; no stage barrier"}]},
            "pairs": [{"source": a, "target": b,
                       "physical_relation": physical.get((a, b), "INDEPENDENT"),
                       "basis": "unknown" if physical.get((a, b)) == "UNKNOWN" else "hypothesis", "evidence": "Synthetic test only",
                       "resource_conflicts": conflicts,
                       "resource_order": resource.get((a, b), "UNDECIDED") if conflicts else "NONE"}
                      for (a, b), conflicts in resource_pairs(data).items()]}


class ResourceDAGTests(unittest.TestCase):
    def test_unknown_is_abstention_not_a_vote_for_no_edge(self):
        data = dataset([1, 1])
        before = response(data, physical={("0", "1"): "BEFORE"})
        unknown = response(data, physical={("0", "1"): "UNKNOWN"})
        graph = aggregate(data, [before] + [unknown] * 4, "scipy", 30)
        self.assertEqual(graph["physical_edges"], [["0", "1"]])
        self.assertEqual(graph["construction_solver"]["stages"][0]["objective"], 0)

    def test_unresolved_resource_order_stays_an_assumption(self):
        data = dataset([2, 3], [1, 1])
        unknown = response(data, physical={("0", "1"): "UNKNOWN"})
        graph = aggregate(data, [unknown] * 5, "scipy", 30)
        result = schedule(data, graph)
        self.assertEqual(graph["status"], "conditional_candidate_dag")
        self.assertEqual(graph["unresolved_pairs"], [["0", "1"]])
        self.assertEqual(len(graph["resource_order_assumptions"]), 1)
        self.assertEqual(result["status"], "conditional_candidate_schedule")
        self.assertEqual(result["unresolved_overlaps"], [])
        self.assertFalse(result["physical_validity_verified"])

    def test_unknown_overlap_differs_from_independence(self):
        data = dataset([2, 3])
        unknown = response(data, physical={("0", "1"): "UNKNOWN"})
        graph = aggregate(data, [unknown] * 5, "scipy", 30)
        self.assertEqual(schedule(data, graph)["unresolved_overlaps"], [["0", "1"]])
        independent = aggregate(data, [response(data)] * 5, "scipy", 30)
        self.assertEqual(independent["unresolved_pairs"], [])
        self.assertEqual(independent["pairs"][0]["physical_relation"], "INDEPENDENT")

    def test_physics_does_not_change_with_durations_or_resource_votes(self):
        data = dataset([1, 4, 3], [1, 1, 0])
        raw = response(data, physical={("1", "2"): "BEFORE"}, resource={("0", "1"): "BEFORE"})
        first = aggregate(data, [raw] * 5, "scipy", 30)
        changed = deepcopy(data)
        changed["tasks"][0]["duration"] = 30
        raw2 = response(changed, physical={("1", "2"): "BEFORE"}, resource={("0", "1"): "AFTER"})
        second = aggregate(changed, [raw2] * 5, "scipy", 30)
        self.assertEqual(first["physical_edges"], second["physical_edges"])
        self.assertTrue(second["construction_solver"]["physical_frozen_before_resources"])

    def test_objective_matches_exhaustive_partial_orders(self):
        data = dataset([1, 2, 3])
        pairs = list(resource_pairs(data))
        # Inconsistent observations force a global decision, independently
        # checked by enumerating all 3^3 candidate partial orders.
        samples = [response(data, physical=dict(zip(pairs, labels))) for labels in [
            ("BEFORE", "INDEPENDENT", "BEFORE"),
            ("BEFORE", "UNKNOWN", "BEFORE"),
            ("INDEPENDENT", "AFTER", "UNKNOWN"),
            ("BEFORE", "AFTER", "BEFORE"),
            ("UNKNOWN", "INDEPENDENT", "AFTER")]]
        scores = []
        for choices in itertools.product(("BEFORE", "AFTER", "INDEPENDENT"), repeat=3):
            edges = {(a, b) if r == "BEFORE" else (b, a)
                     for (a, b), r in zip(pairs, choices) if r != "INDEPENDENT"}
            if any((a, b) in edges and (b, c) in edges and (a, c) not in edges
                   for a, b, c in itertools.permutations(("0", "1", "2"))):
                continue
            cost = sum(item["physical_relation"] != chosen
                       for sample in samples for item, chosen in zip(sample["pairs"], choices)
                       if item["physical_relation"] != "UNKNOWN")
            scores.append((cost, len(edges)))
        graph = aggregate(data, samples, "scipy", 30)
        stages = graph["construction_solver"]["stages"]
        self.assertEqual((stages[0]["objective"], stages[1]["objective"]), min(scores))

    def test_schema_conditions_and_legacy_labels_rejected(self):
        data = dataset([1, 1])
        raw = response(data)
        raw["timeline"].pop("relation_schema")
        with self.assertRaisesRegex(ValueError, "new run"):
            validate_response(data, raw)
        raw = response(data)
        raw["timeline"]["task_conditions"].pop()
        with self.assertRaisesRegex(ValueError, "task_conditions"):
            validate_response(data, raw)
        raw = response(data)
        raw["pairs"][0]["physical_relation"] = "VAGUE"
        with self.assertRaisesRegex(ValueError, "physical_relation"):
            validate_response(data, raw)

    def test_full_17_task_five_response_integration(self):
        root = Path(__file__).resolve().parents[2]
        data = read_json(root / "examples/maintenance/tasks.json")
        candidate = read_json(root / "examples/maintenance/candidate_graph.json")
        reach = reachability([t["id"] for t in data["tasks"]], candidate["edges"])
        relations = {(a, b): "BEFORE" if b in reach[a] else "AFTER" if a in reach[b] else "INDEPENDENT"
                     for a, b in resource_pairs(data)}
        raw = response(data, physical=relations)
        self.assertEqual(len(raw["pairs"]), 136)
        graph = aggregate(data, [raw] * 5, "scipy", 60)
        self.assertEqual(len(graph["resource_edges"]), 10)
        self.assertEqual(schedule(data, graph)["makespan"], 26)

    def test_cpm_rejects_unordered_crane_tasks(self):
        with self.assertRaisesRegex(ValueError, "resource order"):
            schedule(dataset([2, 3], [1, 1]), {"edges": []})

    def test_resource_directions_are_part_of_graph(self):
        data = dataset([2, 3], [1, 1])
        graph = aggregate(data, [response(data)] * 5, "scipy", 30)
        self.assertEqual(len(graph["resource_edges"]), 1)
        self.assertEqual(schedule(data, graph)["makespan"], 5)
        self.assertEqual(graph["physical_edges"], [])

    def test_makespan_overrules_bad_resource_preference_not_physics(self):
        # Starting 1 first unlocks a ten-day off-crane task; model votes prefer 0 first.
        data = dataset([3, 1, 10], [1, 1, 0])
        data["known_precedence"] = [["1", "2"]]
        raw = response(data, resource={("0", "1"): "BEFORE"})
        graph = aggregate(data, [raw] * 5, "scipy", 30)
        self.assertIn(["1", "0"], graph["resource_edges"])
        self.assertEqual(schedule(data, graph)["makespan"], 11)
        self.assertIn(["1", "2"], graph["physical_edges"])

    def test_resource_vote_breaks_equal_makespan_tie(self):
        data = dataset([2, 3], [1, 1])
        raw = response(data, resource={("0", "1"): "AFTER"})
        graph = aggregate(data, [raw] * 5, "scipy", 30)
        self.assertIn(["1", "0"], graph["resource_edges"])

    def test_physical_and_resource_combination_cannot_cycle(self):
        data = dataset([1, 1, 1], [1, 1, 1])
        data["known_precedence"] = [["0", "1"], ["1", "2"]]
        raw = response(data, resource={("0", "2"): "AFTER"})
        graph = aggregate(data, [raw] * 5, "scipy", 30)
        self.assertEqual(schedule(data, graph)["makespan"], 3)
        reach = reachability(["0", "1", "2"], graph["edges"])
        self.assertIn("2", reach["0"])

    def test_probabilities_and_unknown_preserved(self):
        data = dataset([1, 1])
        a = response(data, physical={("0", "1"): "BEFORE"})
        b = response(data, physical={("0", "1"): "UNKNOWN"})
        graph = aggregate(data, [a, a, a, b, b], "scipy", 30)
        self.assertEqual(graph["pairs"][0]["physical_probabilities"]["BEFORE"], .6)
        unknown = aggregate(data, [b] * 5, "scipy", 30)
        self.assertEqual(unknown["edges"], [])
        self.assertEqual(unknown["pairs"][0]["physical_relation"], "UNKNOWN")

    def test_cpm_forward_backward_and_float(self):
        data = dataset([2, 4, 1, 2])
        graph = {"edges": [["0", "1"], ["0", "2"], ["1", "3"], ["2", "3"]]}
        result = schedule(data, graph)
        self.assertEqual(result["makespan"], 8)
        self.assertEqual(result["tasks"][2]["total_float"], 3)
        self.assertEqual(result["critical_path"], ["0", "1", "3"])
        self.assertEqual(result["solver"]["method"], "CPM")

    def test_missing_timeline_or_fake_resource_conflict_rejected(self):
        data = dataset([1, 1], [1, 1])
        raw = response(data)
        raw["timeline"]["stages"][0]["tasks"].pop()
        with self.assertRaisesRegex(ValueError, "Timeline"):
            validate_response(data, raw)
        raw = response(data)
        raw["pairs"][0]["resource_conflicts"] = []
        with self.assertRaisesRegex(ValueError, "Resource conflicts"):
            validate_response(data, raw)

    def test_reduction_keeps_reachability_and_all_nodes(self):
        data = dataset([1, 1, 1, 1])
        graph = optimize_dag(data, [], fixed_graph={"edges": [["0", "1"], ["1", "2"], ["0", "2"]]})
        self.assertNotIn(["0", "2"], graph["edges"])
        self.assertEqual(len(graph["nodes"]), 4)
        self.assertEqual(len(schedule(data, graph)["tasks"]), 4)

    def test_pit_and_multi_capacity_rejected_explicitly(self):
        data = dataset([1, 1], [1, 1], capacity=2)
        with self.assertRaisesRegex(ValueError, "capacity=1"):
            make_prompt(data)
        data = dataset([1])
        data["resource_capacity"]["unit_pit"] = 1
        with self.assertRaisesRegex(ValueError, "unit_pit"):
            make_prompt(data)

    def test_one_task_and_fixed_cycle(self):
        data = dataset([2])
        graph = optimize_dag(data, [], fixed_graph={"edges": []})
        self.assertEqual(schedule(data, graph)["makespan"], 2)
        with self.assertRaisesRegex(ValueError, "cycle"):
            optimize_dag(dataset([1, 1]), [], fixed_graph={"edges": [["0", "1"], ["1", "0"]]})


if __name__ == "__main__":
    unittest.main()
