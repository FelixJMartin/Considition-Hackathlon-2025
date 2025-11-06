# viz_map.py
import matplotlib.pyplot as plt
from client import get_map
from dotenv import load_dotenv
import os

load_dotenv()
MAP_NAME = os.getenv("MAP_NAME", "TRAINING_MAP_1")


def draw_map(m):
    nodes = m.get("nodes", [])
    edges = m.get("edges", [])
    stations = m.get("stations", [])
    vehicles = m.get("vehicles", [])

    # Build an index for node positions
    pos = {n["id"]: (n.get("x", 0), n.get("y", 0)) for n in nodes}

    # Draw edges
    for e in edges:
        u, v = e["u"], e["v"]
        (x1, y1) = pos.get(u, (None, None))
        (x2, y2) = pos.get(v, (None, None))
        if x1 is None or x2 is None:
            continue
        plt.plot([x1, x2], [y1, y2], linewidth=0.8)

    # Draw stations
    for s in stations:
        (xs, ys) = pos.get(s["nodeId"], (None, None))
        if xs is None:
            continue
        # Small marker; you can vary by type
        plt.scatter([xs], [ys], marker="s", s=20)

    # Draw vehicles
    for v in vehicles:
        (xv, yv) = pos.get(v.get("nodeId"), (None, None))
        if xv is None:
            continue
        plt.scatter([xv], [yv], marker="o", s=10)

    plt.title(m.get("name", MAP_NAME))
    plt.axis("equal")
    plt.show()


if __name__ == "__main__":
    m = get_map(MAP_NAME)
    if not m:
        print("Failed to fetch map")
        raise SystemExit(1)
    draw_map(m)
