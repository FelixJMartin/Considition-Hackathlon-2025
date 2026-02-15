import sys
import time
import json
from client import ConsiditionClient

# ============================================================================
# Configuration
# ============================================================================
api_key = "---"
base_url = "https://api.considition.com/"
map_name = "Pistonia"

# ============================================================================
# Global State (Memory across ticks)
# ============================================================================
_PENDING_RECS_FOR_NEXT_TICK = []
_RECENT_CHARGE_UNTIL = {}  # Track cooldown period after charging
_HAS_CHARGED = {}  # Track if customer has charged at least once
_FIRST_SEEN_TICK = {}  # When we first saw each customer
_CAR_STATE = {}  # Estimated consumption rate per customer


# ============================================================================
# Map Parsing Utilities
# ============================================================================

def is_charging_station(node: dict) -> bool:
    """Check if a node is a charging station."""
    tgt = node.get("target") or {}
    t = str(tgt.get("Type", "")).replace(" ", "").lower()
    return t == "chargingstation"


def build_node_and_station_indexes(map_obj):
    """
    Parse map to extract:
    - node_index: all nodes by ID
    - station_ids: set of charging station IDs
    - station_meta: metadata about each station (capacity, speed, green status)
    """
    node_index = {}
    station_ids = set()
    station_meta = {}

    for n in map_obj.get("nodes", []) or []:
        nid = n.get("id")
        if nid is None:
            continue
        nid = str(nid)
        node_index[nid] = n

        if is_charging_station(n):
            station_ids.add(nid)
            tgt = n.get("target") or {}
            station_meta[nid] = {
                "avail": int(tgt.get("amountOfAvailableChargers") or 0),
                "total": int(tgt.get("totalAmountOfChargers") or 0),
                "speed": float(tgt.get("chargeSpeedPerCharger") or 0.0),
                "is_green": bool(
                    tgt.get("isGreen") or tgt.get("IsGreen") or tgt.get("green") or False
                ),
                "zoneId": str(n.get("zoneId") or n.get("zone") or "")
                if (n.get("zoneId") or n.get("zone"))
                else None,
            }
    
    return node_index, station_ids, station_meta


# ============================================================================
# Customer State Tracking
# ============================================================================

def _update_memory_from_log_entry(cid: str, rec: dict):
    """Mark customer as having charged if they spent time charging."""
    state = (rec.get("state") or "").strip()
    t_charge = rec.get("ticksSpentCharging")
    
    try:
        t_charge = float(t_charge) if t_charge is not None else 0.0
    except:
        t_charge = 0.0

    if t_charge > 0 or state in {"Charging", "DoneCharging"}:
        _HAS_CHARGED[cid] = True


def _update_car_state(cid: str, soc, tick, state):
    """
    Track consumption rate using exponential moving average.
    Only update when customer is moving (not charging/waiting).
    """
    if soc is None or tick is None:
        return
    
    try:
        soc = float(soc)
    except:
        return

    st = _CAR_STATE.get(cid)
    moving = state not in {"Charging", "Waiting", "Home"}

    # Calculate consumption rate if we have previous data and car is moving
    if st is not None and moving:
        last_soc = st.get("last_soc")
        last_tick = st.get("last_tick")
        est_cons = st.get("est_cons_per_tick")

        if last_soc is not None and last_tick is not None and tick > last_tick:
            dsoc = last_soc - soc
            dt = tick - last_tick
            
            if dt > 0 and dsoc > 1e-4:
                inst_rate = dsoc / dt
                
                # Exponential moving average: 70% old estimate + 30% new measurement
                if est_cons is None:
                    est_cons = inst_rate
                else:
                    est_cons = 0.7 * est_cons + 0.3 * inst_rate
                
                st["est_cons_per_tick"] = est_cons

    # Initialize or update state
    if st is None:
        st = {"last_soc": soc, "last_tick": tick, "est_cons_per_tick": None}
        _CAR_STATE[cid] = st
    else:
        st["last_soc"] = soc
        st["last_tick"] = tick


def customer_info_from_response(game_response, current_tick, node_index):
    """
    Extract customer information from game response.
    Returns list of customer dicts with current state, position, SoC, etc.
    """
    logs = game_response.get("customerLogs", []) or []
    customers = []

    for entry in logs:
        cid = entry.get("customerId") or entry.get("id")
        if cid is None:
            continue
        cid = str(cid)

        # Find most recent log entry for this customer
        best = None
        for rec in entry.get("logs", []) or []:
            t = rec.get("tick")
            if t is None:
                continue
            if t <= current_tick and (best is None or t > best.get("tick", -1)):
                best = rec
        
        if best is None:
            continue

        # Track when we first saw this customer
        if cid not in _FIRST_SEEN_TICK:
            _FIRST_SEEN_TICK[cid] = current_tick

        _update_memory_from_log_entry(cid, best)

        # Extract position and state
        px = best.get("posX")
        py = best.get("posY")
        node_id = best.get("node")
        edge_id = best.get("edge")
        state = (best.get("state") or "").strip()
        soc = best.get("chargeRemaining")

        # Fill in missing position from node data
        if (px is None or py is None) and node_id is not None:
            n = node_index.get(str(node_id))
            if n:
                px = n.get("posX", px)
                py = n.get("posY", py)

        _update_car_state(cid, soc, current_tick, state)

        customers.append({
            "tick": current_tick,
            "id": cid,
            "mood": best.get("mood"),
            "state": state,
            "chargeRemaining": soc,
            "ticksSpentCharging": best.get("ticksSpentCharging"),
            "ticksSpentWaiting": best.get("ticksSpentWaiting"),
            "posX": px,
            "posY": py,
            "node": str(node_id) if node_id is not None else None,
            "edge": edge_id,
            "persona": best.get("persona"),
        })

    return customers


# ============================================================================
# Customer State Detection
# ============================================================================

# States where customer can receive charging recommendation
CHARGEABLE_STATES = {
    "Charging",
    "Waiting",
    "WaitingForCharger",
    "Idle",
    "Home",
    "TransitioningToNode",
}


def is_at_station_chargeable(c: dict, station_ids) -> bool:
    """Check if customer is at a charging station and can charge."""
    node = c.get("node")
    edge = (c.get("edge") or "").strip()
    state = (c.get("state") or "").strip()
    
    if node not in station_ids:
        return False
    
    # Must not be on an edge (traveling)
    if edge and edge != "N/A":
        return False
    
    return state in CHARGEABLE_STATES


def arriving_next_tick_is_station(c: dict, station_ids):
    """
    Check if customer is traveling TO a charging station.
    Edge format: "nodeA-->nodeB"
    Returns destination station ID if applicable, None otherwise.
    """
    edge = (c.get("edge") or "").strip()
    
    if "-->" not in edge:
        return None
    
    try:
        _, dest = edge.split("-->")
        dest = dest.strip()
        return dest if dest in station_ids else None
    except:
        return None


# ============================================================================
# Environmental Factors (Weather, Time-of-Day)
# ============================================================================

def augment_station_meta_with_environment(game_response, current_tick, station_meta):
    """
    Calculate green energy factor based on:
    - Time of day (solar availability)
    - Weather (cloud cover, wind strength)
    - Station type (green vs. non-green)
    """
    weather = game_response.get("weather") or {}
    cloud = weather.get("cloudCover", weather.get("CloudCover"))
    wind = weather.get("windStrength", weather.get("WindStrength"))

    try:
        cloud = float(cloud)
    except:
        cloud = 0.3  # Default moderate cloud cover
    
    try:
        wind = float(wind)
    except:
        wind = 0.4  # Default moderate wind

    # Calculate time of day (each tick = 5 minutes)
    minutes = current_tick * 5
    hour = (minutes // 60) % 24

    in_solar = 6 <= hour < 18  # Daylight hours
    peak = 10 <= hour < 15  # Peak solar hours

    # Calculate green factor for each station
    for sid, meta in station_meta.items():
        is_green = bool(meta.get("is_green"))
        
        base = 0.35
        if is_green:
            base += 0.25  # Green stations have renewable infrastructure
        if in_solar:
            base += 0.15  # Solar available during day
        if peak:
            base += 0.10  # Extra boost during peak solar
        base += 0.25 * wind  # Wind contribution
        base -= 0.20 * cloud  # Clouds reduce solar
        
        meta["greenFactor"] = max(0.0, min(1.0, base))  # Clamp to [0, 1]
        meta["priceFactor"] = 0.5  # Placeholder for future pricing logic


# ============================================================================
# Charging Recommendation Logic
# ============================================================================

def generate_customer_recommendations(customers_this_tick, station_ids, station_meta):
    """
    Main decision logic: determine which customers should charge where and to what level.
    
    Strategy:
    1. Emergency charging (<35% SoC)
    2. First charge (haven't charged yet)
    3. Preventive charging (<65% SoC at station, <70% arriving)
    4. Persona-based adjustments (eco, cost-sensitive, stress-averse)
    5. Congestion avoidance (skip overcrowded stations)
    """
    recs = []

    # ========== Thresholds ==========
    EMERGENCY_SOC = 0.35
    LOW_SOC = 0.45
    PREVENTIVE_AT_STATION = 0.65
    ARRIVAL_PREVENTIVE_SOC = 0.70
    FIRST_CHARGE_MAX_SOC = 0.95
    MAX_TARGET = 0.97
    FAST_KW_THRESHOLD = 150.0

    # Target SoC levels
    FIRST_FAST_TARGET = 0.93
    FIRST_SLOW_TARGET = 0.90
    NORMAL_FAST_TARGET = 0.90
    NORMAL_SLOW_TARGET = 0.87

    # Count customers waiting/charging at each station (for congestion detection)
    queue_by_station = {}
    for c in customers_this_tick:
        node = c.get("node")
        if node in station_ids:
            state = (c.get("state") or "").strip()
            if state in {"Charging", "Waiting", "WaitingForCharger"}:
                queue_by_station[node] = queue_by_station.get(node, 0) + 1

    # ========== Helper Functions ==========
    
    def persona_adjustment(persona, green_factor, price_factor, base_target):
        """Adjust target SoC based on customer persona."""
        p = (persona or "").lower()
        t = base_target
        
        if "eco" in p:
            t += 0.03 * green_factor  # Eco-conscious: charge more when green
        if "cost" in p:
            t -= 0.02 * (1 - green_factor)  # Cost-sensitive: charge less
        if "stress" in p or "dislikesdriving" in p or "dislikes driving" in p:
            t -= 0.02  # Stress-averse: charge less to reduce wait time
        
        return t

    def choose_target_soc(speed_kw, current_soc, first_charge, persona, green_factor, price_factor):
        """
        Choose target SoC based on:
        - Charger speed (fast vs. slow)
        - Whether this is first charge
        - Customer persona
        - Green energy availability
        """
        if first_charge:
            base = FIRST_FAST_TARGET if speed_kw >= FAST_KW_THRESHOLD else FIRST_SLOW_TARGET
        else:
            base = NORMAL_FAST_TARGET if speed_kw >= FAST_KW_THRESHOLD else NORMAL_SLOW_TARGET

        # Adjust for green energy availability
        base += 0.04 * (green_factor - 0.5)
        
        # Apply persona adjustments
        base = persona_adjustment(persona, green_factor, price_factor, base)
        
        # Ensure we charge at least 18% more than current
        target = max(base, current_soc + 0.18)
        
        return max(0.65, min(MAX_TARGET, target))

    def stagger_target(cid, target):
        """
        Add small random offset to prevent all customers leaving simultaneously.
        Uses customer ID hash for deterministic but varied offsets.
        """
        h = hash(cid) % 7
        delta = (h - 3) * 0.01  # Range: -3% to +3%
        t = target + delta
        return max(0.65, min(MAX_TARGET, t))

    # ========== Main Recommendation Loop ==========
    
    for c in customers_this_tick:
        cid = str(c.get("id"))
        state = (c.get("state") or "").strip()
        soc_raw = c.get("chargeRemaining")
        
        try:
            soc = float(soc_raw if soc_raw is not None else 0.0)
        except:
            soc = 0.0

        persona = c.get("persona") or ""
        has_charged = _HAS_CHARGED.get(cid, False)
        cooldown_soc = float(_RECENT_CHARGE_UNTIL.get(cid, 0.0) or 0.0)

        emergency = soc <= EMERGENCY_SOC

        # ========== Case 1: Customer AT charging station ==========
        if is_at_station_chargeable(c, station_ids):
            sid = str(c["node"])
            meta = station_meta.get(sid, {
                "avail": 0, "total": 0, "speed": 0.0,
                "greenFactor": 0.5, "priceFactor": 0.5,
            })
            
            speed = float(meta.get("speed") or 0.0)
            avail = int(meta.get("avail") or 0)
            total = int(meta.get("total") or 0)
            green_factor = float(meta.get("greenFactor") or 0.5)
            price_factor = float(meta.get("priceFactor") or 0.5)

            # Check congestion
            queue_len = queue_by_station.get(sid, 0)
            utilization = (total - avail) / total if total > 0 else 0.0
            high_congestion = (utilization > 0.9) or (queue_len >= max(2, total))

            # Can only start charging if charger available (or already charging)
            can_start = (avail > 0) or (state == "Charging")
            if not can_start:
                continue

            target = None

            # Priority 1: Emergency (low battery)
            if emergency:
                base = 0.96 if speed >= FAST_KW_THRESHOLD else 0.93
                base += 0.03 * (green_factor - 0.5)
                target = max(0.70, min(MAX_TARGET, base))

            # Priority 2: First charge
            elif not has_charged and soc <= FIRST_CHARGE_MAX_SOC:
                target = choose_target_soc(
                    speed, soc, True, persona, green_factor, price_factor
                )

            # Priority 3: Preventive charging
            else:
                cond_ok = (soc <= PREVENTIVE_AT_STATION)
                
                # Skip if congested and battery not too low
                if high_congestion and soc > LOW_SOC + 0.10:
                    cond_ok = False

                if cond_ok:
                    # Respect cooldown period (don't charge if recently charged)
                    if not (cooldown_soc > 0.0 and soc >= cooldown_soc - 0.02):
                        target = choose_target_soc(
                            speed, soc, False, persona, green_factor, price_factor
                        )

            # Issue recommendation if target determined
            if target is not None:
                target = stagger_target(cid, target)
                recs.append({
                    "customerId": cid,
                    "chargingRecommendations": [{
                        "nodeId": sid,
                        "chargeTo": target
                    }]
                })
                
                # Update memory
                _HAS_CHARGED[cid] = True
                _RECENT_CHARGE_UNTIL[cid] = max(0.70, target - 0.15)
                continue

        # ========== Case 2: Customer ARRIVING at charging station ==========
        dest_station = arriving_next_tick_is_station(c, station_ids)
        
        if dest_station:
            sid = dest_station
            meta = station_meta.get(sid, {
                "avail": 0, "total": 0, "speed": 0.0,
                "greenFactor": 0.5, "priceFactor": 0.5,
            })
            
            avail = int(meta.get("avail") or 0)
            speed = float(meta.get("speed") or 0.0)
            total = int(meta.get("total") or 0)
            green_factor = float(meta.get("greenFactor") or 0.5)
            price_factor = float(meta.get("priceFactor") or 0.5)

            # Check congestion
            queue_len = queue_by_station.get(sid, 0)
            utilization = (total - avail) / total if total > 0 else 0.0
            high_congestion = (utilization > 0.9) or (queue_len >= max(2, total))

            # Skip if no availability (unless emergency)
            if avail <= 0 and not emergency:
                continue

            target = None

            # Priority 1: Emergency
            if emergency:
                base = 0.96 if speed >= FAST_KW_THRESHOLD else 0.93
                base += 0.03 * (green_factor - 0.5)
                target = max(0.70, min(MAX_TARGET, base))

            # Priority 2: First charge (skip if congested and battery OK)
            elif not has_charged and soc <= FIRST_CHARGE_MAX_SOC:
                if not (high_congestion and soc > ARRIVAL_PREVENTIVE_SOC):
                    target = choose_target_soc(
                        speed, soc, True, persona, green_factor, price_factor
                    )

            # Priority 3: Preventive (skip if congested)
            elif soc <= ARRIVAL_PREVENTIVE_SOC and not high_congestion:
                # Respect cooldown
                if not (cooldown_soc > 0.0 and soc >= cooldown_soc - 0.02):
                    target = choose_target_soc(
                        speed, soc, False, persona, green_factor, price_factor
                    )

            # Issue recommendation if target determined
            if target is not None:
                target = stagger_target(cid, target)
                recs.append({
                    "customerId": cid,
                    "chargingRecommendations": [{
                        "nodeId": dest_station,
                        "chargeTo": target
                    }]
                })
                
                # Update memory
                _HAS_CHARGED[cid] = True
                _RECENT_CHARGE_UNTIL[cid] = max(0.70, target - 0.15)

    return recs


# ============================================================================
# Game Loop
# ============================================================================

def should_move_on_to_next_tick(game_response):
    """
    Process game response and generate recommendations for next tick.
    Returns True when ready to proceed.
    """
    global _PENDING_RECS_FOR_NEXT_TICK

    # Parse updated map
    updated_map = game_response.get("map", {}) or {}
    node_index, station_ids, station_meta = build_node_and_station_indexes(updated_map)

    # Determine current tick from logs
    logs = game_response.get("customerLogs", []) or []
    current_tick = -1
    for entry in logs:
        for rec in entry.get("logs", []) or []:
            t = rec.get("tick")
            if isinstance(t, (int, float)) and t > current_tick:
                current_tick = t
    if current_tick < 0:
        current_tick = 0

    # Update station metadata with environmental factors
    augment_station_meta_with_environment(game_response, current_tick, station_meta)

    # Extract customer information
    customers_this_tick = customer_info_from_response(
        game_response, current_tick, node_index
    )
    
    # Generate recommendations
    recs = generate_customer_recommendations(
        customers_this_tick, station_ids, station_meta
    )

    if recs:
        print("→ Applying recommendations:", json.dumps(recs, indent=2))

    _PENDING_RECS_FOR_NEXT_TICK = recs
    return True


def generate_tick(map_obj, tick_no):
    """Create tick payload with pending recommendations."""
    global _PENDING_RECS_FOR_NEXT_TICK
    
    recs = _PENDING_RECS_FOR_NEXT_TICK or []
    tick = {
        "tick": tick_no,
        "customerRecommendations": recs
    }
    
    _PENDING_RECS_FOR_NEXT_TICK = []
    return tick


# ============================================================================
# Local Testing & Cloud Submission
# ============================================================================

def run_local_and_collect_ticks(map_name: str):
    """
    Run simulation locally to test strategy and collect all ticks.
    Returns (all_ticks, final_score) for cloud submission.
    """
    local_base_url = "http://localhost:8081"
    client = ConsiditionClient(local_base_url, api_key)

    # Fetch map
    try:
        map_obj = client.get_map(map_name)
    except Exception as e:
        print(f"Failed to fetch map from local engine: {e}")
        sys.exit(1)

    if not map_obj:
        print("Failed to fetch local map!")
        sys.exit(1)

    total_ticks = int(map_obj.get("ticks", 0))
    print(f"Local map has {total_ticks} ticks")

    final_score = 0
    good_ticks = []  # Ticks that successfully advanced simulation
    all_ticks = []  # All generated ticks (including retries)

    # Initialize first tick
    current_tick = generate_tick(map_obj, 0)
    all_ticks.append(current_tick)

    input_payload = {
        "mapName": map_name,
        "ticks": [current_tick],
        "playToTick": 0,
    }

    # Main simulation loop
    for i in range(total_ticks):
        while True:
            print(f"[LOCAL] Playing tick: {i}")
            start = time.perf_counter()
            
            try:
                game_response = client.post_game(input_payload)
            except Exception as e:
                print(f"Error posting local game data: {e}")
                sys.exit(1)
            
            elapsed_ms = (time.perf_counter() - start) * 1000
            print(f"[LOCAL] Tick {i} took: {elapsed_ms:.2f}ms")

            if not game_response:
                print("Got no local game response")
                sys.exit(1)

            final_score = game_response.get("score", 0)

            # Check if we can advance to next tick
            if should_move_on_to_next_tick(game_response):
                good_ticks.append(current_tick)
                updated_map = game_response.get("map", map_obj) or map_obj
                current_tick = generate_tick(updated_map, i + 1)
                all_ticks.append(current_tick)

                input_payload = {
                    "mapName": map_name,
                    "ticks": [*good_ticks, current_tick],
                    "playToTick": i + 1,
                }
                break

            # Retry current tick with updated recommendations
            updated_map = game_response.get("map", map_obj) or map_obj
            current_tick = generate_tick(updated_map, i)
            all_ticks.append(current_tick)

            input_payload = {
                "mapName": map_name,
                "ticks": [*good_ticks, current_tick],
                "playToTick": i,
            }

    # ========== Final Statistics ==========
    
    final_logs = game_response.get("customerLogs", []) or []
    done_states = {}
    soc_values = []

    for entry in final_logs:
        cid = entry.get("customerId") or entry.get("id")
        if not cid:
            continue
        
        # Find final state
        best = None
        for rec in entry.get("logs", []) or []:
            t = rec.get("tick")
            if t is None:
                continue
            if best is None or t > best.get("tick", -1):
                best = rec
        
        if best is None:
            continue

        state = (best.get("state") or "").strip()
        soc = best.get("chargeRemaining")
        
        try:
            soc = float(soc)
        except:
            soc = 0.0

        done_states[state] = done_states.get(state, 0) + 1
        soc_values.append(soc)

    # Print statistics
    print("\n=== FINAL STATE DISTRIBUTION ===")
    for s, cnt in sorted(done_states.items(), key=lambda x: -x[1]):
        print(f"{s}: {cnt}")
    
    if soc_values:
        avg_soc = sum(soc_values) / len(soc_values)
        print(f"\nAvg final SoC: {avg_soc:.3f}")
        print(f"Min final SoC: {min(soc_values):.3f}")
        print(f"Max final SoC: {max(soc_values):.3f}")

    total_customers = len(_FIRST_SEEN_TICK)
    charged_customers = sum(1 for v in _HAS_CHARGED.values() if v)
    
    print("\n=== CHARGE COVERAGE ===")
    print(f"Total customers: {total_customers}")
    print(f"Charged at least once: {charged_customers}")
    if total_customers > 0:
        coverage = charged_customers / total_customers
        print(f"Coverage rate: {coverage:.1%}")

    print(f"\n[LOCAL] Final score: {final_score}")
    return all_ticks, final_score


def submit_ticks_to_cloud(map_name: str, all_ticks):
    """Submit locally-tested strategy to cloud competition API."""
    cloud_base_url = "https://api.considition.com/"
    client = ConsiditionClient(cloud_base_url, api_key)

    payload = {
        "mapName": map_name,
        "ticks": all_ticks,
    }

    print(f"\n[CLOUD] Submitting {len(all_ticks)} ticks...")
    start = time.perf_counter()
    
    try:
        game_response = client.post_game(payload)
    except Exception as e:
        print(f"Error posting to cloud API: {e}")
        sys.exit(1)
    
    elapsed_ms = (time.perf_counter() - start) * 1000
    print(f"[CLOUD] Submission took: {elapsed_ms:.2f}ms")

    if not game_response:
        print("[CLOUD] No response from cloud")
        sys.exit(1)

    cloud_score = game_response.get("score", 0)
    print(f"[CLOUD] Final score: {cloud_score}\n")
    
    return cloud_score


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    """Run local simulation then submit to cloud."""
    all_ticks, local_score = run_local_and_collect_ticks(map_name)
    print(f"✓ Local dev score: {local_score}")
    
    submit_ticks_to_cloud(map_name, all_ticks)


if __name__ == "__main__":
    main()
