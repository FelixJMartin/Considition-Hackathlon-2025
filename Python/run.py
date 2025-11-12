import sys
import time
import json
from client import ConsiditionClient

api_key = "e32ec928-ac93-466b-8cd5-ac151ef5f7fe"
base_url = "http://localhost:8080"
map_name = "Turbohill"


def is_charging_station(node):
    """
    Returnerar True om noden är en laddstation.
    """
    tgt = node.get("target") or {}
    t = str(tgt.get("Type", "")).replace(" ", "").lower()
    return t == "chargingstation"


def build_node_index(map_obj):
    """
    Bygger två saker:
      - node_index: dict { nodeId -> node-objekt }
      - station_ids: set med nodeId som är laddstationer
    """
    node_index = {}
    station_ids = set()
    for n in map_obj.get("nodes", []) or []:
        nid = n.get("id")
        if nid is None:
            continue
        node_index[nid] = n
        if is_charging_station(n):
            station_ids.add(nid)
    return node_index, station_ids


def pos_charging_stations(map_obj):
    """
    Skriver ut (och returnerar) laddstationers positioner och egenskaper.
    Returnerar: (station_ids, stations_list)
    """
    stations = []
    for node in map_obj.get("nodes", []) or []:
        if not is_charging_station(node):
            continue
        tgt = node.get("target") or {}
        stations.append({
            "nodeId": node.get("id"),
            "x": node.get("posX"),
            "y": node.get("posY"),
            "totalChargers": tgt.get("totalAmountOfChargers"),
            "availableChargers": tgt.get("amountOfAvailableChargers"),
            "speedKW": tgt.get("chargeSpeedPerCharger"),
            "zoneId": node.get("zoneId"),
        })

    print("\n=== Charging Stations (positions) ===")
    if not stations:
        print("(inga laddstationer hittades)")
    else:
        print("nodeId | (x,y)  | avail/total | speed[kW] | zone")
        print("-" * 60)
        for s in stations:
            xy = f"({s['x']},{s['y']})"
            cap = f"{s.get('availableChargers')}/{s.get('totalChargers')}"
            print(
                f"{s['nodeId']:>4}   | {xy:<7} | {cap:<11} | {s.get('speedKW')}      | {s.get('zoneId')}")

    station_ids = {s["nodeId"] for s in stations}
    return station_ids, stations


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
        edge_id = best.get("edge")  # e.g. "1.2-->1.3" while traveling

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


# ---------- Strict station logic & arriving-next-tick look-ahead ----------

def is_at_station_strict(c, station_ids):
    """
    True only if the customer is exactly AT a station node this tick (not already moving away).
    States that are OK to start charging: Waiting, Idle, Home, TransitioningToNode, Charging.
    We EXCLUDE TransitioningToEdge (leaving the node) and Traveling.
    """
    node = c.get("node")
    edge = (c.get("edge") or "").strip()
    state = (c.get("state") or "").strip()
    if node not in station_ids:
        return False
    if edge and edge != "N/A":
        return False
    return state in {"Charging", "Waiting", "Idle", "Home", "TransitioningToNode"}


def empty_tick(t):
    return {"tick": t, "customerRecommendations": []}


def arriving_next_tick_is_station(c, station_ids):
    """
    If edge is 'A-->B' and B ∈ station_ids, return B; else None.
    """
    edge = (c.get("edge") or "").strip()
    if "-->" not in edge:
        return None
    try:
        _, b = edge.split("-->")
        dest = b.strip()
        return dest if dest in station_ids else None
    except Exception:
        return None


def print_customers_at_or_heading(customers_list, station_ids):
    """
    Debug print: customers strictly AT a station or ARRIVING→ a station next tick.
    """
    hits = []
    for c in customers_list:
        if is_at_station_strict(c, station_ids):
            hits.append((c.get("id"), "AT", c.get("node"),
                        c.get("state"), c.get("chargeRemaining")))
        else:
            dest = arriving_next_tick_is_station(c, station_ids)
            if dest:
                hits.append((c.get("id"), "ARRIVING→", dest,
                            c.get("state"), c.get("chargeRemaining")))
    print("\n--- Customers at / heading to charging stations ---")
    if not hits:
        print("(inga kunder vid/på väg till stationer i denna tick)")
    else:
        for cid, status, sid, state, soc in hits:
            print(
                f"Customer {cid} {status} station {sid} | state={state} | SoC={soc}")


# ---------- Recommendation builder (now from current logs) ----------

def generate_customer_recommendations(customers_this_tick, station_ids, soc_full=0.99):
    """
    Build ONE actionable recommendation per customer for THIS/NEXT immediate station only:
      A) If strictly AT a station and not full -> charge here to 1.0
      B) Else if arriving next tick at a station and not full -> charge there to 1.0
    """
    recs = []
    for c in customers_this_tick:
        soc = float(c.get("chargeRemaining") or 0.0)

        # A) already AT a station
        if is_at_station_strict(c, station_ids) and soc < soc_full:
            recs.append({
                "customerId": str(c["id"]),
                "chargingRecommendations": [{
                    "nodeId": str(c["node"]),
                    "chargeTo": 1.0
                }]
            })
            continue

        # B) arriving next tick to a station
        dest_station = arriving_next_tick_is_station(c, station_ids)
        if dest_station and soc < soc_full:
            recs.append({
                "customerId": str(c["id"]),
                "chargingRecommendations": [{
                    "nodeId": str(dest_station),
                    "chargeTo": 1.0
                }]
            })

    return recs


def generate_tick(tick_no, recommendations):
    """
    Build the tick object using camelCase schema.
    """
    return {
        "tick": tick_no,
        "customerRecommendations": recommendations,
    }


def should_move_on_to_next_tick(_response):
    return True


def main():
    client = ConsiditionClient(base_url, api_key)

    # 1) Fetch map, build indices
    try:
        map_obj = client.get_map(map_name)
    except Exception as e:
        print(f"Failed to fetch map: {e}")
        sys.exit(1)
    if not map_obj:
        print("Failed to fetch map!")
        sys.exit(1)

    node_index, station_ids = build_node_index(map_obj)
    pos_charging_stations(map_obj)

    # 2) Start with an empty tick 0 (we will fill it AFTER the first simulate step)
    good_ticks = [empty_tick(0)]
    input_payload = {"mapName": map_name, "ticks": good_ticks[:]}

    total_ticks = int(map_obj.get("ticks", 0) or 0)
    max_ticks = 20
    final_score = 0.0

    for i in range(max_ticks):
        print(f"Playing tick: {i}")
        t0 = time.perf_counter()
        try:
            game_response = client.post_game(input_payload)
        except Exception as e:
            print(f"Error posting game data: {e}")
            sys.exit(1)
        print(f"Tick {i} took: {(time.perf_counter()-t0)*1000:.2f}ms")

        # Scores (snapshot)
        kwh = float(game_response.get("kwhRevenue", 0) or 0)
        ccs = float(game_response.get("customerCompletionScore", 0) or 0)
        scr = float(game_response.get("score", 0) or 0)
        final_score = scr
        print(
            f"Score snapshot → kWh:{kwh:.2f}  CCS:{ccs:.2f}  score:{scr:.2f}  total:{(kwh+ccs+scr):.2f}")

        # Logs at tick i
        customers_this_tick = customer_info_from_response(
            game_response, i, node_index)
        print_customers_at_or_heading(customers_this_tick, station_ids)

        # Rebuild indices from returned map (late/bonus drivers etc.)
        updated_map = game_response.get("map", map_obj) or map_obj
        node_index, station_ids = build_node_index(updated_map)

        # === KEY FIX: build recs FOR TICK i (the tick that will be used when simulating i→i+1) ===
        recs_for_tick_i = generate_customer_recommendations(
            customers_this_tick, station_ids)

        # Overwrite the LAST tick entry (which is tick i) with these recommendations
        # (good_ticks[-1]['tick'] should equal i here)
        good_ticks[-1] = {"tick": i,
                          "customerRecommendations": recs_for_tick_i}

        if recs_for_tick_i:
            import json as _json
            print("→ Applying recommendations for tick", i, ":\n",
                  _json.dumps(recs_for_tick_i, indent=2))

        # Prepare next call: simulate to i+1 and append an empty placeholder for tick i+1
        next_tick_no = i + 1
        good_ticks.append(empty_tick(next_tick_no))
        input_payload = {
            "mapName": map_name,
            "playToTick": next_tick_no,  # simulate i→i+1 using recs from tick i
            "ticks": good_ticks[:],
        }

    print(f"Final score: {final_score}")


if __name__ == "__main__":
    main()
