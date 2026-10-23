"""Resource-aware global DAG generation followed by plain CPM.

The original TRE labels concern observed events. Here BEFORE explicitly means
finish-to-start precedence. EQUAL is excluded: potential parallelism is not
simultaneity. Schema 2 distinguishes INDEPENDENT from UNKNOWN abstentions.
All generated schedules are provisional until domain constraints are verified.
"""
import argparse
import itertools
import json
import math
import hashlib
import os
import time
from pathlib import Path


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_data(data):
    tasks = data["tasks"]
    ids = [t["id"] for t in tasks]
    if not ids or any(not isinstance(i, str) or not i or i == "__completion__" for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("Task IDs must be nonempty and unique")
    for resource, capacity in data["resource_capacity"].items():
        if not math.isfinite(capacity) or capacity < 0:
            raise ValueError(f"Invalid capacity: {resource}")
    for task in tasks:
        if type(task["duration"]) is not int or task["duration"] <= 0:
            raise ValueError("Durations must be positive integer time slots")
        if not math.isfinite(task["cost"]) or task["cost"] < 0:
            raise ValueError("Costs must be finite and nonnegative")
        for resource, demand in task["resources"].items():
            if resource not in data["resource_capacity"]:
                raise ValueError(f"Missing capacity: {resource}")
            if not math.isfinite(demand) or demand < 0 or demand > data["resource_capacity"][resource]:
                raise ValueError(f"Invalid or infeasible demand: {task['id']}/{resource}")
    topological_order(ids, data.get("known_precedence", []))
    return ids


def topological_order(ids, edges):
    successors = {i: set() for i in ids}
    indegree = dict.fromkeys(ids, 0)
    for a, b in edges:
        if a not in successors or b not in successors or a == b:
            raise ValueError(f"Invalid precedence edge: {a}, {b}")
        if b not in successors[a]:
            successors[a].add(b)
            indegree[b] += 1
    ready = sorted(i for i in ids if indegree[i] == 0)
    order = []
    while ready:
        a = ready.pop(0)
        order.append(a)
        for b in sorted(successors[a]):
            indegree[b] -= 1
            if indegree[b] == 0:
                ready.append(b)
                ready.sort()
    if len(order) != len(ids):
        raise ValueError("Precedence cycle: resolve conflicting requirements before scheduling")
    return order


class BinaryMILP:
    """Shared sparse binary model; Gurobi matches the repository's solver choice."""

    def __init__(self, costs, upper=None, integer=None):
        self.costs = costs
        self.upper = upper if upper is not None else [1] * len(costs)
        self.integer = integer if integer is not None else [1] * len(costs)
        self.rows = []

    def add(self, coefficients, lower=-math.inf, upper=math.inf):
        self.rows.append((coefficients, lower, upper))

    def solve(self, backend, time_limit=60):
        if not math.isfinite(time_limit) or time_limit < 0:
            raise ValueError("time_limit must be nonnegative and finite; use 0 for unlimited")
        if backend == "gurobi":
            import gurobipy as gp
            model = gp.Model("MaintenanceOptimization")
            model.Params.OutputFlag = 0
            if time_limit > 0:
                model.Params.TimeLimit = time_limit
            model.Params.MIPGap = 0
            variables = {i: model.addVar(lb=0, ub=self.upper[i],
                         vtype=gp.GRB.INTEGER if self.integer[i] else gp.GRB.CONTINUOUS)
                         for i in range(len(self.costs))}
            model.setObjective(gp.quicksum(c * variables[i] for i, c in enumerate(self.costs)))
            for coefficients, lower, upper in self.rows:
                expr = gp.quicksum(c * variables[i] for i, c in coefficients.items())
                if math.isfinite(lower):
                    model.addConstr(expr >= lower)
                if math.isfinite(upper):
                    model.addConstr(expr <= upper)
            model.optimize()
            if model.SolCount == 0:
                raise ValueError(f"No feasible solution; Gurobi status={model.Status}")
            return [variables[i].X for i in range(len(self.costs))], {
                "backend": "gurobi", "optimal": model.Status == gp.GRB.OPTIMAL,
                "gap": model.MIPGap if model.IsMIP else 0.0,
                "objective_bound": model.ObjBound if model.IsMIP else model.ObjVal,
            }
        import numpy as np
        from scipy.optimize import Bounds, LinearConstraint, milp
        from scipy.sparse import coo_matrix
        row_ids, col_ids, values, lower, upper = [], [], [], [], []
        for row, (coefficients, lo, hi) in enumerate(self.rows):
            lower.append(lo)
            upper.append(hi)
            for col, value in coefficients.items():
                row_ids.append(row)
                col_ids.append(col)
                values.append(value)
        matrix = coo_matrix((values, (row_ids, col_ids)), shape=(len(self.rows), len(self.costs))).tocsc()
        options = {"mip_rel_gap": 0}
        if time_limit > 0:
            options["time_limit"] = time_limit
        result = milp(np.array(self.costs, dtype=float), integrality=np.array(self.integer),
                      bounds=Bounds(0, self.upper), constraints=LinearConstraint(matrix, lower, upper),
                      options=options)
        if result.x is None:
            raise ValueError(f"No feasible solution: {result.message}")
        return result.x, {"backend": "scipy-highs", "optimal": result.status == 0,
                          "gap": float(result.mip_gap) if result.mip_gap is not None else (0.0 if result.status == 0 else None),
                          "objective_bound": float(result.mip_dual_bound) if result.mip_dual_bound is not None else float(result.fun)}










def make_prompt(data):
    from scripts.maintenance.resource_dag import make_prompt as implementation
    return implementation(data)


def validate_response(data, response):
    from scripts.maintenance.resource_dag import validate_response as implementation
    return implementation(data, response)


def aggregate(data, responses, backend, time_limit):
    from scripts.maintenance.resource_dag import optimize_dag
    if not responses:
        raise ValueError("At least one complete response required")
    return optimize_dag(data, responses, backend, time_limit)


def schedule(data, graph, backend="scipy", time_limit=60):
    from scripts.maintenance.resource_dag import cpm
    return cpm(data, graph)


def collect_responses(data, api, model_name, repeat, output, resume=False, factory=None):
    """Persist completed calls; retries are explicit to avoid hidden repeated billing."""
    from scripts.maintenance.resource_dag import parse_response
    def parse_saved(raw_path):
        raw = raw_path.read_text(encoding="utf-8")
        recovery = {}
        result = parse_response(data, raw, recovery_info=recovery)
        if recovery:
            recovery["raw_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
            write_json(raw_path.with_suffix(".recovery.json"), recovery)
        return result
    prompt = make_prompt(data)
    raw_dir = Path(output).with_suffix(".runs")
    manifest = {"version": 1, "api": api, "model": model_name,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "base_url": os.environ.get("OPENAI_BASE_URL") if api == "gpt" else None}
    if raw_dir.exists() and not resume:
        raise ValueError(f"{raw_dir} exists; use --resume with the same input/model or choose a new output")
    raw_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = raw_dir / "manifest.json"
    if manifest_path.exists():
        if read_json(manifest_path) != manifest:
            raise ValueError("Resume input, prompt, endpoint or model changed; choose a new output directory")
    elif any(raw_dir.iterdir()):
        raise ValueError("Old run has no manifest; provenance cannot be verified. Keep it and choose a new output directory")
    else:
        write_json(manifest_path, manifest)
    llm = None
    responses = []
    for n in range(1, repeat + 1):
        raw_path = raw_dir / f"run_{n}.txt"
        parsed_path = raw_dir / f"run_{n}.json"
        if parsed_path.exists():
            response = read_json(parsed_path)
            validate_response(data, response)
            print(f"[{n}/{repeat}] Reusing validated response", flush=True)
        elif raw_path.exists():
            response = parse_saved(raw_path)
            write_json(parsed_path, response)
            print(f"[{n}/{repeat}] Validated existing raw response", flush=True)
        else:
            if llm is None:
                if factory is not None:
                    llm = factory()
                else:
                    from scripts.utils.llms_definitions import GPTModel, GeminiModel, TogetherModel
                    llm = (GPTModel(model_name, stream=True, max_retries=0) if api == "gpt" else
                           {"gemini": GeminiModel, "together": TogetherModel}[api](model_name))
            print(f"[{n}/{repeat}] Request sent; waiting for response (no automatic retries)...", flush=True)
            started = time.monotonic()
            last_progress, characters = started, 0
            attempt = len(list(raw_dir.glob(f"run_{n}.attempt_*.partial.txt"))) + 1
            partial_path = raw_dir / f"run_{n}.attempt_{attempt}.partial.txt"
            try:
                with partial_path.open("x", encoding="utf-8") as partial:
                    def on_chunk(content):
                        nonlocal last_progress, characters
                        first = characters == 0
                        partial.write(content)
                        partial.flush()
                        characters += len(content)
                        now = time.monotonic()
                        if first or now - last_progress >= 15:
                            print(f"[{n}/{repeat}] Receiving: {characters} characters, {now - started:.0f}s elapsed", flush=True)
                            last_progress = now
                    raw = llm.run_model(prompt, on_chunk=on_chunk) if api == "gpt" else llm.run_model(prompt)
            except Exception as error:
                from openai import APIConnectionError
                if getattr(error, "status_code", None) == 524:
                    raise RuntimeError("Relay gateway timeout 524. Wait at least 120 seconds, then retry with --resume. "
                                       "If streaming also fails, contact the relay operator; --time-limit cannot fix this.") from None
                if isinstance(error, APIConnectionError):
                    raise RuntimeError(f"API connection failed or stream was interrupted. Partial output: {partial_path}. "
                                       "Use --resume to reuse completed rounds and restart this failed round; "
                                       "it cannot continue a broken stream. Persistent failures require relay investigation.") from error
                raise
            raw_path.write_text(raw, encoding="utf-8")
            response = parse_saved(raw_path)
            write_json(parsed_path, response)
            print(f"[{n}/{repeat}] Complete and validated ({time.monotonic() - started:.0f}s)", flush=True)
        responses.append(response)
    return responses


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "infer", "aggregate", "complete", "schedule"])
    parser.add_argument("--data", default="examples/maintenance/tasks_live.json")
    parser.add_argument("--output", required=True)
    parser.add_argument("--graph", default="examples/maintenance/candidate_graph.json")
    parser.add_argument("--responses", nargs="+")
    parser.add_argument("--backend", choices=["gurobi", "scipy"], default="scipy")
    parser.add_argument("--time-limit", type=float, default=60,
                        help="DAG optimization budget in seconds; 0 disables the limit (default: 60)")
    parser.add_argument("--api", choices=["gpt", "gemini", "together"], default="gpt")
    parser.add_argument("--model")
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--resume", action="store_true", help="Reuse completed infer calls with matching input/model")
    args = parser.parse_args()
    if not math.isfinite(args.time_limit) or args.time_limit < 0:
        parser.error("--time-limit must be nonnegative and finite; use 0 for unlimited")
    data = read_json(args.data)
    validate_data(data)
    if args.action == "prepare":
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(make_prompt(data), encoding="utf-8")
    elif args.action == "complete":
        from scripts.maintenance.resource_dag import optimize_dag
        write_json(args.output, optimize_dag(data, [], args.backend, args.time_limit,
                                            fixed_graph=read_json(args.graph)))
    elif args.action == "schedule":
        result = schedule(data, read_json(args.graph), args.backend, args.time_limit)
        write_json(args.output, result)
        print(json.dumps({k: result[k] for k in ["status", "makespan", "serial_duration", "fixed_cost", "solver", "unresolved_overlaps"]}, ensure_ascii=False))
    else:
        if args.action == "infer":
            if not args.model or args.repeat < 1:
                parser.error("infer requires --model and --repeat >= 1")
            responses = collect_responses(data, args.api, args.model, args.repeat, args.output, args.resume)
        else:
            if not args.responses:
                parser.error("aggregate requires --responses")
            responses = [read_json(path) for path in args.responses]
        print("All responses validated; optimizing the global DAG...", flush=True)
        graph = aggregate(data, responses, args.backend, args.time_limit)
        write_json(args.output, graph)
        print(f"DAG saved to {args.output}", flush=True)
        print(f"Status: {graph['status']}; unresolved physical pairs: {len(graph['unresolved_pairs'])}; "
              f"conditional resource directions: {len(graph['resource_order_assumptions'])}", flush=True)


if __name__ == "__main__":
    main()
