#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build and visualize an AOE-style project network from a generic JSON file.

Input conventions:
- tasks: [{id, name, duration, cost?, resources?}, ...]
- precon: {"TASK_ID": ["PREDECESSOR_ID", ...], ...}
  The key task depends on every task listed in its value.
- resource_capacity: {"crane": 1, ...}

Scheduling policy:
- non-preemptive activities;
- precedence constraints must be satisfied;
- resource usage must not exceed resource_capacity;
- resource-constrained scheduling uses a deterministic heuristic: at each time point,
  start all feasible ready tasks, prioritizing the longest remaining precedence path.
  This is a feasible schedule, not a proof of globally minimum makespan (RCPSP is NP-hard).

Outputs: AOE graph PNG, Gantt PNG, schedule JSON, and topological-order TXT.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
from matplotlib.font_manager import FontProperties, fontManager

EPS = 1e-9


def fmt_num(value: Any) -> str:
    """Format numbers without unnecessary trailing .0."""
    if isinstance(value, bool):
        return str(value)
    try:
        x = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isclose(x, round(x), abs_tol=EPS):
        return str(int(round(x)))
    return f"{x:g}"


def as_number(value: Any, label: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric, not boolean")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric; got {value!r}") from exc
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"{label} must be finite and >= {minimum}; got {value!r}")
    return number


def normalize_ids(raw: Any) -> str:
    if raw is None or str(raw).strip() == "":
        raise ValueError("Every task must have a non-empty id")
    return str(raw)


def load_and_validate(path: Path):
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError("The JSON root must be an object")
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("JSON must contain a non-empty 'tasks' array")

    tasks: dict[str, dict[str, Any]] = {}
    input_position: dict[str, int] = {}
    for i, raw in enumerate(raw_tasks):
        if not isinstance(raw, dict):
            raise ValueError(f"tasks[{i}] must be an object")
        tid = normalize_ids(raw.get("id"))
        if tid in tasks:
            raise ValueError(f"Duplicate task id: {tid}")
        name = str(raw.get("name", tid))
        duration = as_number(raw.get("duration"), f"task {tid}.duration")
        cost = raw.get("cost", None)
        if cost is not None:
            # Cost is only displayed; it never influences scheduling priority.
            try:
                cost_num = float(cost)
                if not math.isfinite(cost_num):
                    raise ValueError
                cost = cost_num
            except (TypeError, ValueError):
                # Preserve non-numeric costs if the caller uses a textual cost.
                cost = str(cost)
        resources_raw = raw.get("resources", {}) or {}
        if not isinstance(resources_raw, dict):
            raise ValueError(f"task {tid}.resources must be an object")
        resources = {
            str(resource): as_number(amount, f"task {tid}.resources[{resource!r}]")
            for resource, amount in resources_raw.items()
        }
        tasks[tid] = {
            "id": tid,
            "name": name,
            "duration": duration,
            "cost": cost,
            "resources": resources,
            "raw_precon": raw.get("precon", []),
        }
        input_position[tid] = i

    # A precedence edge pred -> task means pred must finish before task can start.
    dependencies: dict[str, set[str]] = {tid: set() for tid in tasks}
    top_precon = data.get("precon", {}) or {}
    if not isinstance(top_precon, dict):
        raise ValueError("'precon' must be an object mapping task ids to predecessor ids")

    def add_predecessors(task_id_raw: Any, preds_raw: Any, source: str):
        task_id = normalize_ids(task_id_raw)
        if task_id not in tasks:
            raise ValueError(f"{source}: precon refers to unknown task {task_id!r}")
        if preds_raw is None:
            return
        if isinstance(preds_raw, (str, int, float)) and not isinstance(preds_raw, bool):
            preds = [preds_raw]
        elif isinstance(preds_raw, list) or isinstance(preds_raw, tuple) or isinstance(preds_raw, set):
            preds = list(preds_raw)
        else:
            raise ValueError(f"{source}: predecessors for {task_id!r} must be an id or list of ids")
        for pred_raw in preds:
            pred = normalize_ids(pred_raw)
            if pred not in tasks:
                raise ValueError(f"{source}: task {task_id!r} has unknown predecessor {pred!r}")
            if pred == task_id:
                raise ValueError(f"Task {task_id!r} cannot depend on itself")
            dependencies[task_id].add(pred)

    for task_id, preds in top_precon.items():
        add_predecessors(task_id, preds, "top-level precon")
    # Also accept task-local precon fields; merge them with top-level precon.
    for tid, task in tasks.items():
        if task["raw_precon"]:
            add_predecessors(tid, task["raw_precon"], f"tasks[{tid}].precon")
        task.pop("raw_precon", None)

    # Build the precedence DAG. IDs are identifiers only, not intended execution order.
    dep_graph = nx.DiGraph()
    dep_graph.add_nodes_from(tasks)
    for task_id, preds in dependencies.items():
        for pred in preds:
            dep_graph.add_edge(pred, task_id)
    if not nx.is_directed_acyclic_graph(dep_graph):
        cycle = nx.find_cycle(dep_graph)
        cycle_text = " -> ".join(str(edge[0]) for edge in cycle + [cycle[0]])
        raise ValueError(f"Precedence constraints contain a cycle: {cycle_text}")

    # Normalize resource capacities. If a used resource has no declared capacity,
    # assume enough capacity for all its demand and emit a warning (no artificial constraint).
    capacity_raw = data.get("resource_capacity", {}) or {}
    if not isinstance(capacity_raw, dict):
        raise ValueError("'resource_capacity' must be an object")
    capacities = {
        str(resource): as_number(capacity, f"resource_capacity[{resource!r}]")
        for resource, capacity in capacity_raw.items()
    }
    warnings = []
    for resource in sorted({r for task in tasks.values() for r in task["resources"]}):
        total_demand = sum(task["resources"].get(resource, 0.0) for task in tasks.values())
        if resource not in capacities:
            capacities[resource] = total_demand
            warnings.append(
                f"Resource {resource!r} is used but has no resource_capacity entry; "
                f"capacity defaults to total demand {fmt_num(total_demand)} (effectively unconstrained)."
            )
        for tid, task in tasks.items():
            demand = task["resources"].get(resource, 0.0)
            if demand > capacities[resource] + EPS:
                raise ValueError(
                    f"Task {tid!r} requires {fmt_num(demand)} of resource {resource!r}, "
                    f"but capacity is only {fmt_num(capacities[resource])}"
                )

    return data, tasks, dependencies, dep_graph, capacities, input_position, warnings


def compute_priorities(tasks: dict[str, dict[str, Any]], dep_graph: nx.DiGraph):
    """Critical-path-length priority, based only on precedence and duration (not cost)."""
    topo = list(nx.topological_sort(dep_graph))
    remaining = {}
    for tid in reversed(topo):
        successors = list(dep_graph.successors(tid))
        tail = max((remaining[s] for s in successors), default=0.0)
        remaining[tid] = tasks[tid]["duration"] + tail
    return remaining


def make_topological_order(tasks, dep_graph, priorities):
    # Ready nodes are prioritized by remaining path length; task names only break ties.
    # Never sort by task ID, since IDs have no sequence meaning.
    return list(nx.lexicographical_topological_sort(
        dep_graph,
        key=lambda tid: (-priorities[tid], tasks[tid]["name"].casefold())
    ))


def schedule_tasks(tasks, dependencies, dep_graph, capacities):
    """Non-preemptive parallel list scheduling with renewable resource capacities."""
    priorities = compute_priorities(tasks, dep_graph)
    topo_order = make_topological_order(tasks, dep_graph, priorities)
    completed: set[str] = set()
    started: set[str] = set()
    running: list[dict[str, Any]] = []
    schedule: dict[str, dict[str, Any]] = {}
    now = 0.0

    def active_usage():
        usage = defaultdict(float)
        for item in running:
            for resource, amount in tasks[item["id"]]["resources"].items():
                usage[resource] += amount
        return usage

    def can_start(tid: str, usage: dict[str, float]) -> bool:
        for resource, amount in tasks[tid]["resources"].items():
            if usage.get(resource, 0.0) + amount > capacities.get(resource, 0.0) + EPS:
                return False
        return True

    while len(completed) < len(tasks):
        # Complete every task that has reached its finish time.
        finished_now = [item for item in running if item["finish"] <= now + EPS]
        for item in finished_now:
            completed.add(item["id"])
        if finished_now:
            running = [item for item in running if item["finish"] > now + EPS]

        usage = active_usage()
        ready = [
            tid for tid in tasks
            if tid not in started and dependencies[tid].issubset(completed)
        ]
        ready.sort(key=lambda tid: (-priorities[tid], tasks[tid]["name"].casefold()))

        did_start_any = False
        for tid in ready:
            if not can_start(tid, usage):
                continue
            task = tasks[tid]
            start = now
            finish = start + task["duration"]
            schedule[tid] = {
                "id": tid,
                "name": task["name"],
                "duration": task["duration"],
                "cost": task["cost"],
                "start": start,
                "finish": finish,
                "resources": dict(task["resources"]),
                "predecessors": sorted(dependencies[tid], key=lambda x: tasks[x]["name"].casefold()),
                "priority_remaining_path": priorities[tid],
            }
            started.add(tid)
            did_start_any = True
            if task["duration"] <= EPS:
                # Zero-duration tasks finish immediately and do not consume resources.
                completed.add(tid)
            else:
                running.append({"id": tid, "finish": finish})
                for resource, amount in task["resources"].items():
                    usage[resource] = usage.get(resource, 0.0) + amount

        if len(completed) == len(tasks):
            break
        if did_start_any:
            # Stay at this time point to start other tasks that became eligible,
            # including successors of zero-duration tasks.
            continue

        if not running:
            # A DAG with valid capacities should never deadlock here.
            blocked = [tid for tid in tasks if tid not in started]
            raise RuntimeError(f"Scheduling deadlock; tasks not started: {blocked}")
        next_time = min(item["finish"] for item in running)
        if next_time < now - EPS:
            raise RuntimeError("Internal scheduling error: time moved backwards")
        now = next_time

    makespan = max((item["finish"] for item in schedule.values()), default=0.0)

    # Verify precedence and resource capacity after scheduling.
    for tid, preds in dependencies.items():
        for pred in preds:
            if schedule[pred]["finish"] > schedule[tid]["start"] + EPS:
                raise RuntimeError(f"Internal error: precedence violated: {pred} -> {tid}")
    for resource, capacity in capacities.items():
        # Check usage at every start/finish boundary. End times are exclusive, so
        # completed activities release resources exactly when another may start.
        times = sorted({0.0} | {s["start"] for s in schedule.values()} | {s["finish"] for s in schedule.values()})
        for t in times:
            used = sum(
                s["resources"].get(resource, 0.0)
                for s in schedule.values()
                if s["start"] <= t + EPS and s["finish"] > t + EPS
            )
            if used > capacity + EPS:
                raise RuntimeError(
                    f"Internal error: resource {resource!r} capacity exceeded at t={t}: "
                    f"{used} > {capacity}"
                )

    return schedule, topo_order, priorities, makespan


def choose_chinese_font():
    available = {font.name for font in fontManager.ttflist}
    for name in ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Noto Sans CJK JP", "Noto Serif CJK SC", "Noto Serif CJK JP", "Noto Sans Mono CJK SC", "WenQuanYi Zen Hei", "Arial Unicode MS"]:
        if name in available:
            return name
    return "DejaVu Sans"


def make_aoe_graph(tasks, dependencies, schedule, capacities, makespan, out_path: Path, title: str):
    """Draw activity edges and dashed precedence/resource connectors between time events.

    Start/end vertices are kept distinct even when their displayed timestamps match. This
    avoids visually merging unrelated events merely because their timestamps are equal.
    """
    graph = nx.DiGraph()
    source, sink = "__PROJECT_START__", "__PROJECT_END__"
    graph.add_node(source, time=0.0, role="source", task_name="项目开始",
                   label="E0\nt=0\n项目开始")
    graph.add_node(sink, time=makespan, role="sink", task_name="项目结束",
                   label=f"项目结束\nt={fmt_num(makespan)}")

    # Use graph-safe internal vertex names. External task IDs remain labels only;
    # punctuation inside an ID must not be parsed by Graphviz as a port separator.
    node_tokens = {tid: f"T{index:03d}" for index, tid in enumerate(tasks, start=1)}

    def start_node(tid):
        return f"S_{node_tokens[tid]}"

    def finish_node(tid):
        return f"F_{node_tokens[tid]}"

    for tid, s in schedule.items():
        graph.add_node(start_node(tid), time=s["start"], role="start", task_id=tid,
                       task_name=s["name"], label=f"t={fmt_num(s['start'])}\n{s['name']}开始\n[{tid}]")
        graph.add_node(finish_node(tid), time=s["finish"], role="finish", task_id=tid,
                       task_name=s["name"], label=f"t={fmt_num(s['finish'])}\n{s['name']}完成\n[{tid}]")
        cost_text = "未提供" if s["cost"] is None else fmt_num(s["cost"])
        resource_text = ""
        if s["resources"]:
            resource_text = "\n资源:" + ",".join(f"{k}={fmt_num(v)}" for k, v in s["resources"].items())
        edge_label = (
            f"{s['name']} [{tid}]\n"
            f"duration={fmt_num(s['duration'])}, cost={cost_text}{resource_text}"
        )
        graph.add_edge(start_node(tid), finish_node(tid), kind="activity", task_id=tid,
                       duration=s["duration"], cost=s["cost"], label=edge_label)

    # Combine multiple reasons between the same pair of event vertices into one connector.
    def add_connector(u, v, reason: str, gap: float):
        gap = max(0.0, float(gap))
        if graph.has_edge(u, v):
            edge = graph[u][v]
            if edge.get("kind") == "activity":
                # This should not occur for the generated event-node scheme.
                raise RuntimeError(f"Unexpected connector overlaps activity edge {u} -> {v}")
            if reason not in edge["reasons"]:
                edge["reasons"].append(reason)
            edge["gap"] = max(edge.get("gap", 0.0), gap)
            edge["label"] = "/".join(edge["reasons"]) + f"\nΔt={fmt_num(edge['gap'])}"
        else:
            graph.add_edge(u, v, kind="connector", reasons=[reason], gap=gap,
                           label=f"{reason}\nΔt={fmt_num(gap)}")

    # Connect initial event to every source activity.
    for tid in tasks:
        if not dependencies[tid]:
            add_connector(source, start_node(tid), "项目起点/等待", schedule[tid]["start"])

    # Precedence connectors; key task depends on each item in its precon list.
    for tid, preds in dependencies.items():
        for pred in preds:
            gap = schedule[tid]["start"] - schedule[pred]["finish"]
            add_connector(finish_node(pred), start_node(tid), f"前置:{pred}→{tid}", gap)

    # For capacity-1 resources, explicitly show the chosen serial order in the AOE graph.
    # (Typical example: one crane, each crane operation requires one crane.)
    for resource, capacity in capacities.items():
        if not math.isclose(capacity, 1.0, abs_tol=EPS):
            continue
        using = [
            tid for tid, task in tasks.items()
            if task["resources"].get(resource, 0.0) > EPS
        ]
        # A task whose demand is below capacity could overlap another such task, so only
        # generate exclusive-order connectors for tasks that individually consume the full unit.
        using = [tid for tid in using if math.isclose(tasks[tid]["resources"].get(resource, 0.0), capacity, abs_tol=EPS)]
        using.sort(key=lambda tid: (schedule[tid]["start"], schedule[tid]["finish"], tasks[tid]["name"].casefold()))
        for prev, nxt in zip(using, using[1:]):
            if schedule[prev]["finish"] <= schedule[nxt]["start"] + EPS:
                add_connector(finish_node(prev), start_node(nxt), f"资源序列:{resource}",
                              schedule[nxt]["start"] - schedule[prev]["finish"])

    # Connect all terminal activities to the project end; edge gap is post-activity waiting.
    successors = {tid: set() for tid in tasks}
    for tid, preds in dependencies.items():
        for pred in preds:
            successors[pred].add(tid)
    for tid in tasks:
        if not successors[tid]:
            add_connector(finish_node(tid), sink, "项目收尾/等待", makespan - schedule[tid]["finish"])

    # Prefer Graphviz's DOT layout when both pydot and the Graphviz `dot` executable are
    # available. It takes edge labels into account and generally avoids crossings better
    # than a hand-built layout. The matplotlib layout below is a portable fallback.
    try:
        font_name = choose_chinese_font()
        graph.graph["graph"] = {
            "rankdir": "TB",
            "ranksep": "0.65",
            "nodesep": "0.75",
            "splines": "spline",
            "overlap": "false",
            "pad": "0.25",
            "outputorder": "edgesfirst",
            "label": f"AOE 活动网络图：{title}\\n实线=工序(duration/cost)；虚线=前置依赖、资源序列或等待",
            "labelloc": "t",
            "fontsize": "16",
            "fontname": font_name,
            "dpi": "180",
        }
        graph.graph["node"] = {
            "shape": "ellipse",
            "style": "filled",
            "fontname": font_name,
            "fontsize": "10",
            "color": "#555555",
            "penwidth": "1.0",
            "margin": "0.12,0.08",
        }
        graph.graph["edge"] = {"fontname": font_name, "fontsize": "8", "arrowsize": "0.7"}
        for node, attrs in graph.nodes(data=True):
            role = attrs.get("role")
            attrs["shape"] = "ellipse"
            attrs["style"] = "filled"
            attrs["fontname"] = font_name
            attrs["fontsize"] = "9"
            attrs["color"] = "#555555"
            attrs["fillcolor"] = "#f3c778" if role in ("source", "sink") else ("#c9e3f5" if role == "start" else "#d9ead3")
        for u, v, attrs in graph.edges(data=True):
            attrs["fontname"] = font_name
            if attrs.get("kind") == "activity":
                attrs.update({"color": "#2369a8", "style": "solid", "penwidth": "1.8", "fontsize": "8"})
            else:
                attrs.update({"color": "#777777", "style": "dashed", "penwidth": "1.0", "fontsize": "7"})
        pydot_graph = nx.nx_pydot.to_pydot(graph)
        pydot_graph.set_rankdir("TB")
        pydot_graph.set_ranksep("0.65")
        pydot_graph.set_nodesep("0.75")
        pydot_graph.set_splines("spline")
        pydot_graph.set_overlap("false")
        pydot_graph.set_pad("0.25")
        pydot_graph.set_fontname(font_name)
        pydot_graph.set_dpi("180")
        pydot_graph.write_png(str(out_path), prog="dot")
        pydot_graph.write_svg(str(out_path.with_suffix(".svg")), prog="dot")
        pydot_graph.write_raw(str(out_path.with_suffix(".dot")))
        return graph
    except Exception as exc:
        print(f"提示：Graphviz 排版不可用，将使用 matplotlib 后备布局。原因：{exc}", file=sys.stderr)

    # Fallback layout: x-coordinate is scheduled time; distinct events at the same time
    # occupy separate vertical positions, so equal times do not imply a dependency.
    layer: dict[float, list[str]] = defaultdict(list)
    for node, attrs in graph.nodes(data=True):
        layer[round(float(attrs["time"]), 8)].append(node)

    def node_sort_key(node):
        a = graph.nodes[node]
        role_rank = {"source": 0, "start": 1, "finish": 2, "sink": 3}.get(a.get("role"), 4)
        return (role_rank, str(a.get("task_name", "")).casefold(), str(a.get("task_id", "")))

    pos = {}
    for timestamp in sorted(layer):
        group = sorted(layer[timestamp], key=node_sort_key)
        if len(group) == 1:
            ys = [0.0]
        else:
            ys = np.linspace((len(group) - 1) / 2.0, -(len(group) - 1) / 2.0, len(group))
        for node, y in zip(group, ys):
            pos[node] = (float(graph.nodes[node]["time"]), float(y))

    plt.rcParams["font.sans-serif"] = [choose_chinese_font(), "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    max_layer = max((len(nodes) for nodes in layer.values()), default=1)
    fig_w = max(17, min(30, 15 + makespan * 0.42))
    fig_h = max(10, min(22, max_layer * 0.72 + 4))
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))

    activity_edges = [(u, v) for u, v, d in graph.edges(data=True) if d["kind"] == "activity"]
    connector_edges = [(u, v) for u, v, d in graph.edges(data=True) if d["kind"] == "connector"]
    nx.draw_networkx_edges(
        graph, pos, edgelist=connector_edges, ax=ax,
        edge_color="#777777", style="dashed", width=1.25,
        arrows=True, arrowstyle="-|>", arrowsize=12,
        min_source_margin=10, min_target_margin=10,
        connectionstyle="arc3,rad=0.04"
    )
    nx.draw_networkx_edges(
        graph, pos, edgelist=activity_edges, ax=ax,
        edge_color="#2369a8", width=2.0,
        arrows=True, arrowstyle="-|>", arrowsize=15,
        min_source_margin=10, min_target_margin=10,
        connectionstyle="arc3,rad=0.0"
    )

    node_colors = []
    for node in graph.nodes:
        role = graph.nodes[node].get("role")
        if role in ("source", "sink"):
            node_colors.append("#e7a94b")
        elif role == "start":
            node_colors.append("#c9e3f5")
        else:
            node_colors.append("#d9ead3")
    nx.draw_networkx_nodes(graph, pos, node_size=1050, node_color=node_colors,
                           edgecolors="#444444", linewidths=0.9, ax=ax)
    labels = {node: attrs["label"] for node, attrs in graph.nodes(data=True)}
    nx.draw_networkx_labels(graph, pos, labels=labels, font_size=7.1, ax=ax)

    edge_labels = {(u, v): d["label"] for u, v, d in graph.edges(data=True)}
    nx.draw_networkx_edge_labels(
        graph, pos, edge_labels=edge_labels, font_size=6.7, label_pos=0.52,
        rotate=False, bbox={"alpha": 0.86, "color": "white", "pad": 0.8, "edgecolor": "none"}, ax=ax
    )

    # Grid marks the numeric time axis. The node timestamps are the authoritative values.
    ax.set_xlabel("工期时间点（事件节点内标注实际时刻）")
    ax.set_title(f"AOE 活动网络图：{title}\n实线=实际工序(duration/cost)；虚线=前置依赖、资源序列或等待", fontsize=13, pad=16)
    ax.set_xticks(sorted(set(float(g) for g in layer)))
    ax.tick_params(axis="x", labelsize=8)
    ax.grid(axis="x", linestyle=":", alpha=0.32)
    ax.set_ylim(-max_layer / 2 - 1.5, max_layer / 2 + 1.5)
    ax.margins(x=0.035, y=0.03)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.set_yticks([])
    fig.tight_layout()
    fig.savefig(out_path, dpi=220, bbox_inches="tight")
    plt.close(fig)
    return graph


def make_gantt(tasks, schedule, capacities, out_path: Path, title: str, time_unit: str):
    plt.rcParams["font.sans-serif"] = [choose_chinese_font(), "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    ordered = sorted(schedule, key=lambda tid: (
        schedule[tid]["start"], -schedule[tid]["priority_remaining_path"],
        schedule[tid]["name"].casefold()
    ))
    fig_h = max(7, 0.48 * len(ordered) + 2.5)
    fig, ax = plt.subplots(figsize=(15, fig_h))
    y_positions = list(range(len(ordered)))
    labels = [f"{schedule[tid]['name']} [{tid}]" for tid in ordered]
    for y, tid in zip(y_positions, ordered):
        s = schedule[tid]
        resource_txt = ", ".join(f"{k}={fmt_num(v)}" for k, v in s["resources"].items())
        is_resource_task = bool(s["resources"])
        ax.barh(y, s["duration"], left=s["start"], height=0.62,
                edgecolor="#333333", linewidth=0.65,
                color="#e3a34b" if is_resource_task else "#6daedb")
        if resource_txt:
            ax.text(s["finish"] + 0.08, y, resource_txt, va="center", fontsize=8)
    ax.set_yticks(y_positions)
    ax.set_yticklabels(labels, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel(f"时间 ({time_unit})")
    ax.set_title(f"工序调度甘特图：{title}\n橙色工序表示使用了输入中声明的资源", fontsize=12)
    ax.grid(axis="x", linestyle=":", alpha=0.4)
    ax.set_xlim(left=0, right=max((s["finish"] for s in schedule.values()), default=0) * 1.12 + 1)
    fig.tight_layout()
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig)


def json_safe(value):
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    if isinstance(value, float) and math.isclose(value, round(value), abs_tol=EPS):
        return int(round(value))
    return value


def main():
    parser = argparse.ArgumentParser(
        description="从 JSON 工序依赖和资源约束构建 AOE 风格图，输出调度表及可视化图片。"
    )
    parser.add_argument("json_file", help="输入 JSON 文件路径")
    parser.add_argument("--outdir", default="aoe_output", help="输出目录，默认 aoe_output")
    args = parser.parse_args()

    input_path = Path(args.json_file).expanduser().resolve()
    if not input_path.is_file():
        print(f"错误：找不到 JSON 文件：{input_path}", file=sys.stderr)
        return 2

    try:
        data, tasks, dependencies, dep_graph, capacities, input_position, warnings = load_and_validate(input_path)
        schedule, topo_order, priorities, makespan = schedule_tasks(tasks, dependencies, dep_graph, capacities)
    except (ValueError, json.JSONDecodeError, nx.NetworkXException) as exc:
        print(f"输入或依赖关系错误：{exc}", file=sys.stderr)
        return 2

    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    title = str(data.get("name", input_path.stem))
    time_unit = str(data.get("time_unit", "未指定"))
    cost_unit = str(data.get("cost_unit", "未指定"))

    aoe_path = outdir / "aoe_graph.png"
    gantt_path = outdir / "schedule_gantt.png"
    schedule_path = outdir / "schedule.json"
    topo_path = outdir / "topological_order.txt"

    graph_title = f"{title}（计划工期：{fmt_num(makespan)} {time_unit}）"
    make_aoe_graph(tasks, dependencies, schedule, capacities, makespan, aoe_path, graph_title)
    make_gantt(tasks, schedule, capacities, gantt_path, title, time_unit)

    task_output = []
    for tid in sorted(schedule, key=lambda x: (schedule[x]["start"], -priorities[x], tasks[x]["name"].casefold())):
        s = schedule[tid]
        task_output.append({
            "id": tid,
            "name": s["name"],
            "duration": s["duration"],
            "cost": s["cost"],
            "start": s["start"],
            "finish": s["finish"],
            "resources": s["resources"],
            "predecessors": s["predecessors"],
            "priority_remaining_path": s["priority_remaining_path"],
        })
    result = {
        "project_name": title,
        "time_unit": time_unit,
        "cost_unit": cost_unit,
        "makespan": makespan,
        "resource_capacity": capacities,
        "scheduling_method": (
            "non-preemptive resource-constrained list scheduling; ready tasks are started in parallel "
            "when feasible; priority is descending remaining precedence-path duration, then task name. "
            "Cost is excluded from scheduling priority. This is a heuristic feasible schedule, not a proof of global optimality."
        ),
        "topological_order": topo_order,
        "tasks": task_output,
        "warnings": warnings,
    }
    schedule_path.write_text(json.dumps(json_safe(result), ensure_ascii=False, indent=2), encoding="utf-8")
    topo_path.write_text("\n".join(f"{i + 1}. {tid} - {tasks[tid]['name']}" for i, tid in enumerate(topo_order)) + "\n", encoding="utf-8")

    print(f"项目：{title}")
    print(f"工序数量：{len(tasks)}")
    print(f"资源容量：{ {k: fmt_num(v) for k, v in capacities.items()} }")
    print(f"拓扑排序（按剩余路径优先级打破并列，不按 ID 排序）：{' -> '.join(topo_order)}")
    print(f"计划工期：{fmt_num(makespan)} {time_unit}")
    print("\n调度结果（按实际开始时间排序）：")
    print(f"{'开始':>8} {'结束':>8} {'工序ID':>10}  {'工序名称':<18} {'duration':>8} {'cost':>8}  资源")
    ordered = sorted(schedule, key=lambda tid: (schedule[tid]["start"], -priorities[tid], tasks[tid]["name"].casefold()))
    for tid in ordered:
        s = schedule[tid]
        cost = "-" if s["cost"] is None else fmt_num(s["cost"])
        resources = ",".join(f"{k}={fmt_num(v)}" for k, v in s["resources"].items()) or "-"
        print(f"{fmt_num(s['start']):>8} {fmt_num(s['finish']):>8} {tid:>10}  {s['name']:<18.18} {fmt_num(s['duration']):>8} {cost:>8}  {resources}")
    for warning in warnings:
        print(f"警告：{warning}")
    print("\n已生成：")
    print(f"  AOE 可视化图：{aoe_path}")
    print(f"  AOE 矢量图：  {aoe_path.with_suffix('.svg')}（可放大查看）")
    print(f"  调度甘特图：  {gantt_path}")
    print(f"  调度 JSON：   {schedule_path}")
    print(f"  拓扑排序文本：{topo_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
