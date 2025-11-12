

import sys
import time
from client import ConsiditionClient

api_key = "e32ec928-ac93-466b-8cd5-ac151ef5f7fe"
base_url = "http://localhost:8080"
map_name = "Turbohill"


def build_node_index(map_obj):
    """Return: node_by_id dict mapping 'r.c' -> {'posX': x, 'posY': y, ...}"""
    idx = {}
    for n in map_obj.get("nodes", []) or []:
        nid = n.get("id")
        if nid is not None:
            idx[str(nid)] = n
    return idx


def customer_info_from_response(game_response, current_tick, node_index):
    """
    Print dynamic customer info for a given tick using customerLogs from the game response.
    Falls back to node coordinates if posX/posY are missing.
    """

    def fmt_float(v, places=3):
        if v is None:
            return "N/A"
        try:
            return f"{float(v):.{places}f}"
        except Exception:
            return str(v)

    def or_na(v):
        return "N/A" if v is None else v

    logs = game_response.get("customerLogs", []) or []
    customers = []

    for entry in logs:
        cid = entry.get("customerId") or entry.get("id")
        # pick the latest snapshot ≤ current_tick
        best = None
        for rec in entry.get("logs", []) or []:
            t = rec.get("tick")
            if t is None:
                continue
            if t <= current_tick and (best is None or t > best.get("tick", -1)):
                best = rec
        if best is None:
            continue

        # Determine coordinates
        px = best.get("posX")
        py = best.get("posY")
        node_id = best.get("node")
        edge_id = best.get("edge")  # like "1.2-->1.3" while traveling

        if (px is None or py is None) and node_id:
            n = node_index.get(str(node_id))
            if n:
                px = n.get("posX", px)
                py = n.get("posY", py)

        info = {
            "tick": current_tick,
            "id": cid,
            "mood": best.get("mood"),
            "state": best.get("state"),
            "chargeRemaining": best.get("chargeRemaining"),
            "ticksSpentCharging": best.get("ticksSpentCharging"),
            "ticksSpentWaiting": best.get("ticksSpentWaiting"),
            "posX": px,
            "posY": py,
            "node": node_id,
            "edge": edge_id,
        }
        customers.append(info)

    print(f"\n=== Customer Info @ Tick {current_tick} ===")
    if not customers:
        print("(no customer logs for this tick)")
        return customers

    for c in customers:
        print(
            f"ID {or_na(c['id']):>5} | Mood: {or_na(c['mood']):<8} | "
            f"SoC: {fmt_float(c['chargeRemaining'])} | "
            f"State: {or_na(c['state']):<18} | Node: {or_na(c['node']):<4} | "
            f"Edge: {or_na(c['edge']):<12} | Pos: ({or_na(c['posX'])},{or_na(c['posY'])})"
        )
    return customers


def should_move_on_to_next_tick(response):
    return True


def generate_customer_recommendations(map_obj, current_tick):
    return []


def generate_tick(map_obj, current_tick):
    return {
        "tick": current_tick,
        "customerRecommendations": generate_customer_recommendations(map_obj, current_tick),
    }


def main():
    api_key = "e32ec928-ac93-466b-8cd5-ac151ef5f7fe"
    base_url = "http://localhost:8080"
    map_name = "Turbohill"

    client = ConsiditionClient(base_url, api_key)

    # 1) Fetch static map (layout) and build node index
    try:
        map_obj = client.get_map(map_name)
    except Exception as e:
        print(f"Failed to fetch map: {e}")
        sys.exit(1)

    if not map_obj:
        print("Failed to fetch map!")
        sys.exit(1)

    node_index = build_node_index(map_obj)  # <-- after map_obj exists

    # 2) Init game state
    final_score = 0
    good_ticks = []

    current_tick = generate_tick(map_obj, 0)
    input_payload = {
        "mapName": map_name,
        "ticks": [current_tick],
    }

    total_ticks = int(map_obj.get("ticks", 0))
    max_ticks = 10

    # 3) Play and inspect ticks
    for i in range(min(total_ticks, max_ticks)):
        print(f"Playing tick: {i}")
        start = time.perf_counter()
        try:
            game_response = client.post_game(input_payload)
        except Exception as e:
            print(f"Error posting game data: {e}")
            sys.exit(1)
        elapsed_ms = (time.perf_counter() - start) * 1000
        print(f"Tick {i} took: {elapsed_ms:.2f}ms")

        if not game_response:
            print("Got no game response")
            sys.exit(1)

        # Score snapshot
        final_score = game_response.get("score", 0) or 0

        # 4) Print dynamic per-tick customer info from customerLogs
        customer_info_from_response(game_response, i, node_index)

        # 5) Advance to next tick
        if should_move_on_to_next_tick(game_response):
            good_ticks.append(current_tick)  # keep the tick we just played
            updated_map = game_response.get("map", map_obj) or map_obj  # usually unchanged, but safe
            current_tick = generate_tick(updated_map, i + 1)
            input_payload = {
                "mapName": map_name,
                "playToTick": i + 1,
                "ticks": [*good_ticks, current_tick],
            }
        else:
            # If you ever implement validation/feedback, handle it here.
            # For now, just stop to avoid looping on the same tick.
            break

    print(f"Final score: {final_score}")



if __name__ == "__main__":
    main()
