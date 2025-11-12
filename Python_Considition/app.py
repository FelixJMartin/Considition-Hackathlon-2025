# import sys
# import time
# from client import ConsiditionClient

# from collections import deque


# api_key = "e32ec928-ac93-466b-8cd5-ac151ef5f7fe"
# base_url = "http://localhost:8080/api"
# map_name = "Turbohill"


# def list_charging_stations_full(map_obj, game_response=None):
#     zone_logs = (game_response or {}).get("zoneLogs", [])
#     latest_zlog = max(zone_logs, key=lambda e: e.get(
#         "tick", -1)) if zone_logs else None
#     zones = {z["zoneId"]: z for z in latest_zlog["zones"]
#              } if latest_zlog else {}

#     stations = []
#     for node in map_obj.get("nodes", []):
#         tgt = node.get("target") or {}
#         if str(tgt.get("Type", "")).replace(" ", "").lower() == "chargingstation":
#             zone_id = node.get("zoneId") or tgt.get("zoneId")
#             zone_data = zones.get(zone_id, {})
#             src_info = zone_data.get("sourceinfo", {})
#             green_total = sum(v.get("production", 0)
#                               for v in src_info.values() if v.get("isGreen"))
#             total_prod = sum(v.get("production", 0) for v in src_info.values())
#             green_share = round(green_total / total_prod,
#                                 3) if total_prod > 0 else None

#             stations.append({
#                 "nodeId": node.get("id"),
#                 "x": node.get("posX"),
#                 "y": node.get("posY"),
#                 "zoneId": zone_id,
#                 "isGreenStation": tgt.get("isGreen", False),
#                 "totalChargers": tgt.get("totalAmountOfChargers", 0),
#                 "availableChargers": tgt.get("amountOfAvailableChargers", 0),
#                 "brokenChargers": tgt.get("totalAmountOfBrokenChargers", 0),
#                 "chargeSpeedKW": tgt.get("chargeSpeedPerCharger", 0),
#                 "chargePrice": tgt.get("chargePrice", None),
#                 "zoneGreenShare": green_share,
#                 "zonePricePerMWh": zone_data.get("pricePerMWh"),
#                 "zoneSupplyRatio": zone_data.get("suppliedDemandRatio"),
#                 "weather": zone_data.get("weather"),
#             })
#     return stations


# def print_charging_stations(map_obj, sort_by="nodeId", limit=None):
#     "<---- lets just make a prettier more readable version to visuaise the chargers --->"
#     data = get_charging_stations(map_obj)
#     if not data:
#         print("No charging stations found.")
#         return

#     if sort_by in data[0]:
#         data.sort(key=lambda d: (d.get(sort_by) is None, d.get(sort_by)))

#     if isinstance(limit, int) and limit > 0:
#         data = data[:limit]

#     header = [
#         "nodeId", "(x,y)", "avail/total", "broken", "speed[kW]", "green", "zone"
#     ]
#     print(" | ".join(header))
#     print("-" * 80)

#     for s in data:
#         xy = f"({s.get('x')},{s.get('y')})"
#         cap = f"{s.get('availableChargers')}/{s.get('totalChargers')}"
#         row = [
#             str(s.get("nodeId")),
#             xy,
#             cap,
#             str(s.get("brokenChargers")),
#             str(s.get("chargeSpeedKW")),
#             str(bool(s.get("isGreen"))),
#             str(s.get("zoneId")),
#         ]
#         print(" | ".join(row))


# # client = ConsiditionClient(base_url, api_key)
# # map_obj = client.get_map(map_name)

# # # stations = get_charging_stations(map_obj)

# # # # print(stations)
# # # print_charging_stations(map_obj)


# # stations = list_charging_stations_full(map_obj)
# # for s in stations:
# #     print(f"Station {s['nodeId']} @ ({s['x']},{s['y']}) | "
# #           f"Avail: {s['availableChargers']}/{s['totalChargers']} | "
# #           f"Speed: {s['chargeSpeedKW']}kW | GreenStation: {s['isGreenStation']} | "
# #           f"Zone {s['zoneId']} → GreenShare: {s['zoneGreenShare']} | "
# #           f"Price: {s['zonePricePerMWh']} | Supply: {s['zoneSupplyRatio']}")


# "<------------------------>"


# def list_customers(map_obj):
#     out = []
#     for n in map_obj.get("nodes", []):
#         for c in n.get("customers", []) or []:
#             cc = dict(c)
#             cc["_nodeId"] = n.get("id")
#             out.append(cc)
#     return out


# def is_charging_station(node):
#     """Return True if the node is a charging station (covers minor naming variants)."""
#     tgt = node.get("target") or {}
#     t = str(tgt.get("Type", "")).replace(" ", "").lower()
#     return t == "chargingstation"


# def build_node_index(map_obj):
#     """Return {nodeId: node}, and a set of station nodeIds."""
#     idx = {}
#     stations = set()
#     for n in map_obj.get("nodes", []) or []:
#         nid = n.get("id")
#         if nid is not None:
#             idx[nid] = n
#             if is_charging_station(n):
#                 stations.add(nid)
#     return idx, stations


# def customers_needing_charge(customers, soc_low=0.25):
#     """
#     Return customers that look risky (low SoC) or standing at a charger with sub-80% SoC.
#     This is intentionally simple to earn quick 'charged at least once' points.
#     """
#     need = []
#     for c in customers:
#         soc = c.get("chargeRemaining")
#         try:
#             soc = float(soc)
#         except Exception:
#             continue
#         if soc is None:
#             continue
#         # Low battery OR “at a charger and not yet pretty full”
#         if soc < soc_low or soc < 0.80:
#             need.append(c)
#     return need


# def neighbors_of(node_id, map_obj):
#     """
#     Return neighbor nodeIds of node_id from the map's edges.
#     Tries common key names; falls back gracefully if unknown.
#     """
#     nbrs = set()
#     for e in map_obj.get("edges", []) or []:
#         # Try common shapes:
#         a = e.get("from") or e.get(
#             "fromNodeId") or e.get("a") or e.get("start")
#         b = e.get("to") or e.get("toNodeId") or e.get("b") or e.get("end")
#         if a is None or b is None:
#             continue
#         if a == node_id:
#             nbrs.add(b)
#         elif b == node_id:
#             nbrs.add(a)
#     return list(nbrs)


# def pick_station_for_customer(c, node_index, station_ids, map_obj):
#     """
#     Return (where_to_go_nodeId, where_to_charge_nodeId) for customer c.
#     - If standing on a station: (current_node, current_node)
#     - Else if an adjacent node is a station: (that_station, that_station)
#     - Else: (None, None) → skip for now (keeps it simple and safe)
#     """
#     here = c.get("_nodeId")
#     if here in station_ids:
#         return here, here

#     for nb in neighbors_of(here, map_obj):
#         if nb in station_ids:
#             return nb, nb

#     return None, None


# "<------------------------>"


# def neighbors_of(node_id, map_obj):
#     nbrs = set()
#     for e in map_obj.get("edges", []) or []:
#         a = e.get("from") or e.get(
#             "fromNodeId") or e.get("a") or e.get("start")
#         b = e.get("to") or e.get("toNodeId") or e.get("b") or e.get("end")
#         if a is None or b is None:
#             continue
#         if a == node_id:
#             nbrs.add(b)
#         elif b == node_id:
#             nbrs.add(a)
#     return list(nbrs)


# def shortest_path_within_hops(start_id, goal_set, map_obj, max_hops=2):
#     """Return the path (list of nodeIds) from start to the nearest station within max_hops, else []"""
#     if start_id in goal_set:
#         return [start_id]
#     q = deque([(start_id, [start_id])])
#     seen = {start_id}
#     while q:
#         node, path = q.popleft()
#         if len(path) - 1 >= max_hops:
#             continue
#         for nb in neighbors_of(node, map_obj):
#             if nb in seen:
#                 continue
#             new_path = path + [nb]
#             if nb in goal_set:
#                 return new_path
#             seen.add(nb)
#             q.append((nb, new_path))
#     return []


# "<------------------------>"
# "<---------- Recommendations that we can give  ------------->"


# def make_charge_rec(customer_id, node_id, minutes=10):
#     return {
#         "customerId": customer_id,
#         "action": "Charge",        # try this
#         "nodeId": node_id,         # if engine expects 'atNodeId', add it too
#         "atNodeId": node_id,       # hedge
#         "minutes": minutes,        # small top-up to “charged at least once”
#     }


# def make_move_rec(customer_id, to_node_id):
#     return {
#         "customerId": customer_id,
#         "action": "Move",          # sometimes engines want 'MoveToNode'
#         "toNodeId": to_node_id,
#         "toNode": to_node_id,      # hedge
#     }


# "<------------------------>"


# def should_move_on_to_next_tick(response):
#     return True


# def generate_customer_recommendations(map_obj, current_tick):
#     customers = list_customers(map_obj)
#     node_index, station_ids = build_node_index(map_obj)

#     recs = []
#     for c in customers:
#         cid = c.get("id")
#         if cid is None:
#             continue
#         here = c.get("_nodeId")
#         soc = c.get("chargeRemaining")
#         try:
#             soc = float(soc)
#         except Exception:
#             soc = None

#         # 1) If on a station and not ~full → charge briefly
#         if here in station_ids and (soc is None or soc < 0.90):
#             recs.append(make_charge_rec(cid, here, minutes=10))
#             continue

#         # 2) If lowish SoC, try to get to a station within 2 hops
#         if soc is not None and soc < 0.45:
#             path = shortest_path_within_hops(
#                 here, station_ids, map_obj, max_hops=2)
#             if path and len(path) >= 2:
#                 next_hop = path[1]
#                 recs.append(make_move_rec(cid, next_hop))
#                 # don't queue charge yet; we'll issue it when they stand on the station in a later tick
#                 continue

#         # 3) Light-touch: if medium SoC and an adjacent station exists, hop there and charge
#         if soc is not None and 0.45 <= soc < 0.75:
#             nbs = neighbors_of(here, map_obj)
#             adj_station = next((nb for nb in nbs if nb in station_ids), None)
#             if adj_station is not None:
#                 recs.append(make_move_rec(cid, adj_station))
#                 # also request a short charge now (some engines accept same-tick plan)
#                 recs.append(make_charge_rec(cid, adj_station, minutes=10))
#                 continue

#     return recs


# def generate_tick(map_obj, current_tick):
#     return {
#         "tick": current_tick,
#         "customerRecommendations": generate_customer_recommendations(map_obj, current_tick),
#     }


# def main():
#     api_key = "e32ec928-ac93-466b-8cd5-ac151ef5f7fe"
#     base_url = "http://localhost:8080/api"
#     map_name = "Turbohill"

#     client = ConsiditionClient(base_url, api_key)

#     try:
#         map_obj = client.get_map(map_name)
#     except Exception as e:
#         print(f"Failed to fetch map: {e}")
#         sys.exit(1)

#     if not map_obj:
#         print("Failed to fetch map!")
#         sys.exit(1)

#     final_score = 0
#     good_ticks = []

#     current_tick = generate_tick(map_obj, 0)
#     input_payload = {
#         "mapName": map_name,
#         "ticks": [current_tick],
#     }

#     total_ticks = int(map_obj.get("ticks", 0))

#     for i in range(total_ticks):
#         while True:
#             print(f"Playing tick: {i}")
#             start = time.perf_counter()
#             try:
#                 game_response = client.post_game(input_payload)
#             except Exception as e:
#                 print(f"Error posting game data: {e}")
#                 sys.exit(1)
#             elapsed_ms = (time.perf_counter() - start) * 1000
#             print(f"Tick {i} took: {elapsed_ms:.2f}ms")

#             if not game_response:
#                 print("Got no game response")
#                 sys.exit(1)

#             # right after you receive game_response
#             if isinstance(game_response, dict):
#                 if game_response.get("errors"):
#                     print("ERRORS:", game_response["errors"])
#                 if game_response.get("validationMessage"):
#                     print("VALIDATION:", game_response["validationMessage"])
#                 if game_response.get("customerLogs"):
#                     print("customerLogs (first 8):")
#                     for line in game_response["customerLogs"][:8]:
#                         print("-", line)

#             # Sum the scores directly (assuming they are numbers)
#             # final_score = game_response.get("score", 0)
#             # Peek scores
#             kwh = float(game_response.get(
#                 "kwhRevenue", game_response.get("kwchRevenue", 0)) or 0)
#             ccs = float(game_response.get("customerCompletionScore", 0) or 0)
#             scr = float(game_response.get("score", 0) or 0)
#             final_score = kwh + ccs + scr
#             print(
#                 f"Score snapshot → kWh:{kwh:.2f}  CCS:{ccs:.2f}  score:{scr:.2f}  total:{final_score:.2f}")

#             if should_move_on_to_next_tick(game_response):
#                 good_ticks.append(current_tick)
#                 updated_map = game_response.get("map", map_obj) or map_obj
#                 current_tick = generate_tick(updated_map, i + 1)
#                 input_payload = {
#                     "mapName": map_name,
#                     "playToTick": i + 1,
#                     "ticks": [*good_ticks, current_tick],
#                 }
#                 break

#             updated_map = game_response.get("map", map_obj) or map_obj
#             current_tick = generate_tick(updated_map, i)
#             input_payload = {
#                 "mapName": map_name,
#                 "playToTick": i,
#                 "ticks": [*good_ticks, current_tick],
#             }

#     print(f"Final score: {final_score}")


# if __name__ == "__main__":
#     main()


import sys
import time
from client import ConsiditionClient


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

    try:
        map_obj = client.get_map(map_name)
    except Exception as e:
        print(f"Failed to fetch map: {e}")
        sys.exit(1)

    if not map_obj:
        print("Failed to fetch map!")
        sys.exit(1)

    final_score = 0
    good_ticks = []

    current_tick = generate_tick(map_obj, 0)
    input_payload = {
        "mapName": map_name,
        "ticks": [current_tick],
    }

    total_ticks = int(map_obj.get("ticks", 0))

    for i in range(total_ticks):
        while True:
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

            # Sum the scores directly (assuming they are numbers)
            final_score = game_response.get("score", 0)

            if should_move_on_to_next_tick(game_response):
                good_ticks.append(current_tick)
                updated_map = game_response.get("map", map_obj) or map_obj
                current_tick = generate_tick(updated_map, i + 1)
                input_payload = {
                    "mapName": map_name,
                    "playToTick": i + 1,
                    "ticks": [*good_ticks, current_tick],
                }
                break

            updated_map = game_response.get("map", map_obj) or map_obj
            current_tick = generate_tick(updated_map, i)
            input_payload = {
                "mapName": map_name,
                "playToTick": i,
                "ticks": [*good_ticks, current_tick],
            }

    print(f"Final score: {final_score}")


if __name__ == "__main__":
    main()
