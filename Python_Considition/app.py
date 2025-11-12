import sys
import time
import json
from client import ConsiditionClient

# --- CONFIG ---
api_key = "e32ec928-ac93-466b-8cd5-ac151ef5f7fe"
base_url = "http://localhost:8080"
# base_url = "https://api.considition.com"
# map_name = "Turbohill"
# map_name = "Clutchfield"
map_name = "Batterytown"

# === Small internal cache so the main() you provided works ===
# filled in should_move_on_to_next_tick(); consumed in generate_tick()
_PENDING_RECS_FOR_NEXT_TICK = []

# Minimal memory to avoid immediate re-topups
# customerId -> soc_threshold_to_allow_next_charge (e.g. 0.70)
_RECENT_CHARGE_UNTIL = {}


# --- BASIC MAP/STATION INDEXING ---

def is_charging_station(node):
    tgt = node.get("target") or {}
    t = str(tgt.get("Type", "")).replace(" ", "").lower()
    return t == "chargingstation"


def build_node_and_station_indexes(map_obj):
    """
    Returns:
      node_index:    { nodeId -> node }
      station_ids:   set(nodeId)
      station_meta:  { nodeId -> {"avail": int, "total": int, "speed": float} }
    """
    node_index = {}
    station_ids = set()
    station_meta = {}

    for n in map_obj.get("nodes", []) or []:
        nid = n.get("id")
        if nid is None:
            continue
        node_index[nid] = n
        if is_charging_station(n):
            station_ids.add(nid)
            tgt = n.get("target") or {}
            station_meta[nid] = {
                "avail": int(tgt.get("amountOfAvailableChargers") or 0),
                "total": int(tgt.get("totalAmountOfChargers") or 0),
                "speed": float(tgt.get("chargeSpeedPerCharger") or 0.0),
            }
    return node_index, station_ids, station_meta


def print_charging_stations(station_meta, node_index):
    print("\n=== Charging Stations (positions) ===")
    if not station_meta:
        print("(inga laddstationer hittades)")
        return
    print("nodeId | (x,y)  | avail/total | speed[kW]")
    print("-" * 52)
    for nid, meta in sorted(station_meta.items()):
        n = node_index.get(nid, {})
        xy = f"({n.get('posX')},{n.get('posY')})"
        cap = f"{meta['avail']}/{meta['total']}"
        print(f"{nid:>4}   | {xy:<7} | {cap:<11} | {int(meta['speed'])}")


# --- CUSTOMER LOG PARSING (current tick snapshot) ---

def customer_info_from_response(game_response, current_tick, node_index):
    """
    Produce per-customer snapshot at <= current_tick from customerLogs.
    """
    logs = game_response.get("customerLogs", []) or []
    customers = []

    for entry in logs:
        cid = entry.get("customerId") or entry.get("id")
        best = None
        for rec in entry.get("logs", []) or []:
            t = rec.get("tick")
            if t is None:
                continue
            if t <= current_tick and (best is None or t > best.get("tick", -1)):
                best = rec
        if best is None:
            continue

        px = best.get("posX")
        py = best.get("posY")
        node_id = best.get("node")
        edge_id = best.get("edge")  # e.g. "1.2-->1.3"

        if (px is None or py is None) and node_id:
            n = node_index.get(str(node_id))
            if n:
                px = n.get("posX", px)
                py = n.get("posY", py)

        customers.append({
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
        })

    # Pretty-print (useful while tuning)
    def fmt_float(v, p=3):
        try:
            return f"{float(v):.{p}f}"
        except Exception:
            return "N/A"

    def or_na(v): return "N/A" if v is None else v

    print(f"\n=== Customer Info @ Tick {current_tick} ===")
    if not customers:
        print("(no customer logs for this tick)")
    else:
        for c in customers:
            print(
                f"ID {or_na(c['id']):>5} | Mood: {or_na(c['mood']):<8} | "
                f"SoC: {fmt_float(c['chargeRemaining'])} | "
                f"State: {or_na(c['state']):<18} | Node: {or_na(c['node']):<4} | "
                f"Edge: {or_na(c['edge']):<12} | Pos: ({or_na(c['posX'])},{or_na(c['posY'])})"
            )
    return customers


# --- LIGHTWEIGHT DECISION HELPERS ---

CHARGEABLE_STATES = {"Charging", "Waiting",
                     "Idle", "Home", "TransitioningToNode"}


def is_at_station_chargeable(c, station_ids):
    node = c.get("node")
    edge = (c.get("edge") or "").strip()
    state = (c.get("state") or "").strip()
    if node not in station_ids:
        return False
    if edge and edge != "N/A":
        return False
    return state in CHARGEABLE_STATES


def arriving_next_tick_is_station(c, station_ids):
    edge = (c.get("edge") or "").strip()
    if "-->" not in edge:
        return None
    try:
        _, b = edge.split("-->")
        dest = b.strip()
        return dest if dest in station_ids else None
    except Exception:
        return None


def empty_tick(t):
    return {"tick": t, "customerRecommendations": []}


# --- RECOMMENDER WITH SMALL OPTIMIZATIONS ---

def generate_customer_recommendations(customers_this_tick, station_ids, station_meta):
    """
    Optimised but still simple policy:

      SAFETY:
        - If SoC < 0.12 → always charge to 1.0 at current/next station (if possible).

      AT a station (chargeable state) and SoC < 0.98:
        - If station has availability OR already Charging → recommend charging.
        - Target depends on stall speed & SoC:
            * fast (≥180 kW):   1.0 if SoC < 0.50 else 0.85
            * normal/slow:      0.80–0.85 (shorter sessions free capacity)
        - Suppress immediate re-topups using _RECENT_CHARGE_UNTIL.

      ARRIVING next tick to a station and SoC < 0.98:
        - Only recommend if destination has availability now (avoid queues).
        - Same adaptive target logic as above.

      Staggering:
        - Slight, deterministic nudge on chargeTo per customer to avoid synchronized end times.
    """
    recs = []
    CRITICAL_SOC = 0.12
    FULL_SOC = 0.98
    FAST_KW = 180.0

    for c in customers_this_tick:
        cid = str(c.get("id"))
        soc = float(c.get("chargeRemaining") or 0.0)
        state = (c.get("state") or "").strip()

        # Respect "cooldown": if we've recently filled this customer to X,
        # don't recommend again until SoC drops below that.
        cooldown = float(_RECENT_CHARGE_UNTIL.get(cid, 0.0) or 0.0)
        if soc >= cooldown and soc >= 0.55:
            # if they've got plenty since last top-up, skip unless safety/arrival logic says otherwise
            pass  # handled below by checks

        # Helper to compute a target considering speed & SoC
        def compute_target(speed_kw: float, soc_now: float) -> float:
            if soc_now < CRITICAL_SOC:
                return 1.0
            if speed_kw >= FAST_KW:
                # fast stalls: top up more aggressively to boost kWh
                return 1.0 if soc_now < 0.50 else 0.85
            # normal/slow: be modest to free capacity earlier
            return 0.85 if soc_now < 0.40 else 0.80

        # Small deterministic staggering (avoid all cars stopping at exactly same SoC)
        # Creates +/- ~0.02 variation by hashing the id
        def stagger(target: float) -> float:
            h = hash(cid) % 7  # 0..6
            delta = (h - 3) * 0.005  # -0.015 .. +0.015
            t = max(0.65, min(1.0, target + delta))
            return t

        # --- A) AT station path ---
        if is_at_station_chargeable(c, station_ids) and soc < FULL_SOC:
            sid = str(c["node"])
            meta = station_meta.get(sid, {"avail": 0, "speed": 0.0})
            speed = float(meta.get("speed") or 0.0)
            can_start = (meta.get("avail", 0) > 0) or state == "Charging"

            if can_start:
                target = stagger(compute_target(speed, soc))

                # Respect cooldown unless safety
                if soc >= CRITICAL_SOC and soc >= cooldown and cooldown > 0.0 and soc >= 0.55:
                    # skip trivial top-up
                    pass
                else:
                    recs.append({
                        "customerId": cid,
                        "chargingRecommendations": [{
                            "nodeId": sid,
                            "chargeTo": target
                        }]
                    })
                    # After issuing a charge, set a per-customer cooldown so we don't immediately re-topup
                    # When we ask them to go to 1.0 we set cooldown ~0.95; otherwise ~target-0.05
                    _RECENT_CHARGE_UNTIL[cid] = 0.95 if target >= 0.95 else max(
                        0.70, target - 0.05)
                continue  # don't also issue an arrival rec

        # --- B) ARRIVING next tick path ---
        dest_station = arriving_next_tick_is_station(c, station_ids)
        if dest_station and soc < FULL_SOC:
            meta = station_meta.get(dest_station, {"avail": 0, "speed": 0.0})
            if meta.get("avail", 0) > 0:
                speed = float(meta.get("speed") or 0.0)
                target = stagger(compute_target(speed, soc))

                # Cooldown check (unless safety)
                if soc < CRITICAL_SOC or soc < cooldown or cooldown == 0.0 or soc < 0.55:
                    recs.append({
                        "customerId": cid,
                        "chargingRecommendations": [{
                            "nodeId": dest_station,
                            "chargeTo": target
                        }]
                    })
                    _RECENT_CHARGE_UNTIL[cid] = 0.95 if target >= 0.95 else max(
                        0.70, target - 0.05)

    return recs


# === ADAPTERS TO MATCH YOUR MAIN() ===

def should_move_on_to_next_tick(game_response):
    """
    Compute recommendations based on the logs present in this game_response,
    and stash them so generate_tick(...) can attach them to the NEXT tick object.
    Return True to move on.
    """
    global _PENDING_RECS_FOR_NEXT_TICK

    # Latest map -> dynamic station availability
    updated_map = game_response.get("map", {}) or {}
    node_index, station_ids, station_meta = build_node_and_station_indexes(
        updated_map)

    # Determine the current tick seen in logs (max tick)
    logs = game_response.get("customerLogs", []) or []
    current_tick = -1
    for entry in logs:
        for rec in entry.get("logs", []) or []:
            t = rec.get("tick")
            if isinstance(t, (int, float)) and t > current_tick:
                current_tick = t
    if current_tick < 0:
        current_tick = 0  # fallback

    customers_this_tick = customer_info_from_response(
        game_response, current_tick, node_index)
    recs = generate_customer_recommendations(
        customers_this_tick, station_ids, station_meta)

    if recs:
        print("→ Applying recommendations:",
              json.dumps(recs, indent=2))
    _PENDING_RECS_FOR_NEXT_TICK = recs  # consumed by next generate_tick(...)
    return True


def generate_tick(map_obj, tick_no):
    """
    Build the tick object the way your main() expects.
    It attaches the recommendations computed in the LAST call to should_move_on_to_next_tick(...).
    """
    global _PENDING_RECS_FOR_NEXT_TICK
    recs = _PENDING_RECS_FOR_NEXT_TICK or []
    tick = {
        "tick": tick_no,
        "customerRecommendations": recs
    }
    # Clear after consuming so we don't resend same recs twice
    _PENDING_RECS_FOR_NEXT_TICK = []
    return tick


# --- (Optional) quick-run main used in your code ---
def main():
    api_key = "e32ec928-ac93-466b-8cd5-ac151ef5f7fe"
    base_url = "http://localhost:8080"
    # base_url = "https://api.considition.com"
    # map_name = "Turbohill"
    # map_name = "Clutchfield"
    map_name = "Batterytown"

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
