"""Synthetic inputs shared by v2 tests."""
def dataset(durations, demands=None, capacity=1):
    return {"time_unit": "day", "resource_capacity": {"crane": capacity},
            "tasks": [{"id": str(i), "duration": d, "name": str(i), "cost": 1,
                       "resources": {"crane": (demands or [0] * len(durations))[i]}}
                      for i, d in enumerate(durations)]}
