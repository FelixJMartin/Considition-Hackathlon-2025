import sys
import time
import json
from client import ConsiditionClient

# ============================================================
# GLOBAL CONFIG
# ============================================================

api_key = "e32ec928-ac93-466b-8cd5-ac151ef5f7fe"
# base_url = "http://localhost:8080"
base_url = "https://api.considition.com/"
# map_name = "Turbohill"
# map_name = "Clutchfield"
map_name = "Batterytown"
# map_name = "Thunderroad"

# --- Internal caches (used across ticks) --------------------

# Filled in should_move_on_to_next_tick(); consumed in generate_tick()
_PENDING_RECS_FOR_NEXT_TICK = []

# customerId -> soc_threshold_to_allow_next_charge (e.g. 0.75)
_RECENT_CHARGE_UNTIL = {}

# customerId -> bool (has ever charged)
_HAS_CHARGED = {}

# customerId -> first tick ever seen
_FIRST_SEEN_TICK = {}

# Per-car rolling state: consumption etc.
# cid -> {
#   "last_soc": float,
#   "last_tick": int,
#   "est_cons_per_tick": float,
# }
_CAR_STATE = {}

# ============================================================
# BASIC MAP / STATION INDEXING
# ============================================================


def is_charging_station(node: dict) -> bool:
    tgt = node.get("target") or {}
    t = str(tgt.get("Type", "")).replace(" ", "").lower()
    return t == "chargingstation"


def build_node_and_station_indexes(map_obj):
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
                    tgt.get("isGreen")
                    or tgt.get("IsGreen")
                    or tgt.get("green")
                    or False
                ),
                "zoneId": str(n.get("zoneId") or n.get("zone") or "")
                if (n.get("zoneId") or n.get("zone"))
                else None,
            }

    return node_index, station_ids, station_meta

# ============================================================
# CUSTOMER LOG PARSING & PER-CAR STATE
# ============================================================


def _update_memory_from_log_entry(cid: str, rec: dict):
    state = (rec.get("state") or "").strip()
    t_charge = rec.get("ticksSpentCharging")
    try:
        t_charge = float(t_charge) if t_charge is not None else 0.0
    except Exception:
        t_charge = 0.0

    if t_charge > 0 or state in {"Charging", "DoneCharging"}:
        _HAS_CHARGED[cid] = True


def _update_car_state(cid: str, soc, tick, state):
    """
    Maintain a per-car rolling estimate of SoC consumption per tick while driving.
    This makes the "emergency / low" thresholds adaptive to how fast they burn energy.
    """
    if soc is None or tick is None:
        return

    try:
        soc = float(soc)
    except Exception:
        return

    st = _CAR_STATE.get(cid)
    # Only update consumption when moving (not Charging / Waiting)
    moving = state not in {"Charging", "Waiting", "Home"}

    if st is not None and moving:
        last_soc = st.get("last_soc")
        last_tick = st.get("last_tick")
        est_cons = st.get("est_cons_per_tick")

        if last_soc is not None and last_tick is not None and tick > last_tick:
            dsoc = last_soc - soc
            dt = tick - last_tick
            if dt > 0 and dsoc > 1e-4:
                inst_rate = dsoc / dt
                if est_cons is None:
                    est_cons = inst_rate
                else:
                    # Exponential moving average
                    est_cons = 0.7 * est_cons + 0.3 * inst_rate
                st["est_cons_per_tick"] = est_cons

    if st is None:
        st = {"last_soc": soc, "last_tick": tick, "est_cons_per_tick": None}
        _CAR_STATE[cid] = st
    else:
        st["last_soc"] = soc
        st["last_tick"] = tick


def customer_info_from_response(game_response, current_tick, node_index):
    logs = game_response.get("customerLogs", []) or []
    customers = []

    for entry in logs:
        cid = entry.get("customerId") or entry.get("id")
        if cid is None:
            continue
        cid = str(cid)

        best = None
        for rec in entry.get("logs", []) or []:
            t = rec.get("tick")
            if t is None:
                continue
            if t <= current_tick and (best is None or t > best.get("tick", -1)):
                best = rec

        if best is None:
            continue

        if cid not in _FIRST_SEEN_TICK:
            _FIRST_SEEN_TICK[cid] = current_tick

        _update_memory_from_log_entry(cid, best)

        px = best.get("posX")
        py = best.get("posY")
        node_id = best.get("node")
        edge_id = best.get("edge")
        state = (best.get("state") or "").strip()
        soc = best.get("chargeRemaining")

        # Fill in coordinates from node if missing
        if (px is None or py is None) and node_id is not None:
            n = node_index.get(str(node_id))
            if n:
                px = n.get("posX", px)
                py = n.get("posY", py)

        # Update car state (consumption)
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

    # Debug: comment out when stable
    def fmt_float(v, p=3):
        try:
            return f"{float(v):.{p}f}"
        except Exception:
            return "N/A"

    def or_na(v):
        return "N/A" if v is None else v

    print(f"\n=== Customer Info @ Tick {current_tick} ===")
    if not customers:
        print("(no customer logs for this tick)")
    else:
        for c in customers:
            print(
                f"ID {or_na(c['id']):>5} | Mood: {or_na(c['mood']):<10} | "
                f"SoC: {fmt_float(c['chargeRemaining'])} | "
                f"State: {or_na(c['state']):<18} | Node: {or_na(c['node']):<6} | "
                f"Edge: {or_na(c['edge']):<16} | Persona: {or_na(c['persona'])}"
            )

    return customers

# ============================================================
# LIGHTWEIGHT HELPERS
# ============================================================


CHARGEABLE_STATES = {
    "Charging",
    "Waiting",
    "Idle",
    "Home",
    "TransitioningToNode",
}


def is_at_station_chargeable(c: dict, station_ids) -> bool:
    node = c.get("node")
    edge = (c.get("edge") or "").strip()
    state = (c.get("state") or "").strip()
    if node not in station_ids:
        return False
    if edge and edge != "N/A":
        return False
    return state in CHARGEABLE_STATES


def arriving_next_tick_is_station(c: dict, station_ids):
    edge = (c.get("edge") or "").strip()
    if "-->" not in edge:
        return None
    try:
        _, dest = edge.split("-->")
        dest = dest.strip()
        return dest if dest in station_ids else None
    except Exception:
        return None


def empty_tick(t: int) -> dict:
    return {"tick": t, "customerRecommendations": []}

# ============================================================
# ENVIRONMENT / GREENNESS
# ============================================================


def augment_station_meta_with_environment(game_response, current_tick, station_meta):
    weather = game_response.get("weather") or {}
    cloud = weather.get("cloudCover", weather.get("CloudCover"))
    wind = weather.get("windStrength", weather.get("WindStrength"))

    try:
        cloud = float(cloud)
    except Exception:
        cloud = 0.3
    try:
        wind = float(wind)
    except Exception:
        wind = 0.4

    # 1 tick = 5 min
    minutes = current_tick * 5
    hour = (minutes // 60) % 24

    in_solar_window = 6 <= hour < 18
    in_midday_peak = 10 <= hour < 15

    for sid, meta in station_meta.items():
        is_green = bool(meta.get("is_green"))
        base = 0.35

        if is_green:
            base += 0.25

        if in_solar_window:
            base += 0.15
        if in_midday_peak:
            base += 0.10

        base += 0.25 * wind
        base -= 0.20 * cloud

        green_factor = max(0.0, min(1.0, base))
        price_factor = 0.5  # placeholder hook

        meta["greenFactor"] = green_factor
        meta["priceFactor"] = price_factor

# ============================================================
# RECOMMENDER – MAP-AGNOSTIC, COMPLETION-FOCUSED
# ============================================================


def _persona_biases(persona: str):
    """
    Return small multiplicative/additive tweaks for thresholds/targets.
    """
    p = (persona or "").lower()
    bias = {
        "target_boost": 0.0,   # + to charge higher
        "target_cut": 0.0,     # - to cut target a bit
        "risk_aversion": 1.0,  # >1 means charge earlier
        "green_weight": 1.0,   # >1 means more green-seeking
    }

    if "eco" in p:
        bias["target_boost"] += 0.02
        bias["green_weight"] += 0.6

    if "cost" in p:
        bias["target_cut"] += 0.03
        bias["green_weight"] -= 0.3

    if "stress" in p or "dislikesdriving" in p or "dislikes driving" in p:
        bias["risk_aversion"] *= 1.3
        bias["target_boost"] += 0.01

    return bias


def generate_customer_recommendations(customers_this_tick, station_ids, station_meta):
    """
    Map-agnostic strategy:

    - Use per-car consumption estimate to set dynamic emergency/low thresholds.
    - Guarantee at least one strong first charge for each car.
    - Avoid re-topups above a per-car cooldown threshold.
    - Avoid sending non-urgent cars into heavy congestion.
    - Bias targets by persona and greenFactor.
    """
    recs = []

    # Base thresholds
    BASE_EMERGENCY_SOC = 0.18
    BASE_LOW_SOC = 0.30
    BASE_PREVENTIVE_SOC = 0.48
    MAX_TARGET = 0.94

    FAST_KW_THRESHOLD = 150.0

    FIRST_FAST_TARGET = 0.90
    FIRST_SLOW_TARGET = 0.86
    NORMAL_FAST_TARGET = 0.84
    NORMAL_SLOW_TARGET = 0.80

    # --- Queue estimation ----------------------------------------------------
    queue_by_station = {}
    for c in customers_this_tick:
        node = c.get("node")
        if node in station_ids:
            state = (c.get("state") or "").strip()
            if state in {"Charging", "Waiting"}:
                queue_by_station[node] = queue_by_station.get(node, 0) + 1

    def choose_target_soc(speed_kw, soc, first_charge, persona_bias, green_factor):
        if first_charge:
            base = FIRST_FAST_TARGET if speed_kw >= FAST_KW_THRESHOLD else FIRST_SLOW_TARGET
        else:
            base = NORMAL_FAST_TARGET if speed_kw >= FAST_KW_THRESHOLD else NORMAL_SLOW_TARGET

        # Prefer higher targets when green
        base += 0.05 * (green_factor - 0.5)

        # Persona tweaks
        base += persona_bias["target_boost"]
        base -= persona_bias["target_cut"]

        # Always gain at least 12 percentage points when we decide to charge
        target = max(base, soc + 0.12)
        return max(0.60, min(MAX_TARGET, target))

    def stagger_target(cid, target):
        h = hash(cid) % 7  # 0..6
        delta = (h - 3) * 0.01
        t = target + delta
        return max(0.60, min(MAX_TARGET, t))

    for c in customers_this_tick:
        cid = str(c.get("id"))
        state = (c.get("state") or "").strip()
        soc_raw = c.get("chargeRemaining")

        try:
            soc = float(soc_raw if soc_raw is not None else 0.0)
        except Exception:
            soc = 0.0

        persona = c.get("persona") or ""
        bias = _persona_biases(persona)

        has_charged = _HAS_CHARGED.get(cid, False)
        cooldown_soc = float(_RECENT_CHARGE_UNTIL.get(cid, 0.0) or 0.0)

        car_state = _CAR_STATE.get(cid, {})
        est_cons = car_state.get("est_cons_per_tick") or 0.0

        # Use consumption estimate to adapt thresholds:
        # if they burn quickly, behave more risk-averse.
        # Heuristic: "ticks to empty" ~ soc / cons
        if est_cons > 1e-5:
            ticks_to_empty = soc / est_cons
        else:
            ticks_to_empty = 999.0

        risk_aversion = bias["risk_aversion"]

        # Dynamic emergency: if they'd be empty in < ~6 ticks, treat as emergency.
        emergency = (soc <= BASE_EMERGENCY_SOC *
                     risk_aversion) or (ticks_to_empty <= 6.0 * risk_aversion)

        # Dynamic low thresholds
        low_soc = min(0.45, BASE_LOW_SOC * risk_aversion)
        preventive_soc = min(0.55, BASE_PREVENTIVE_SOC * risk_aversion)

        # ---------- A) At-station logic ----------
        if is_at_station_chargeable(c, station_ids):
            sid = str(c["node"])
            meta = station_meta.get(sid, {
                "avail": 0,
                "total": 0,
                "speed": 0.0,
                "greenFactor": 0.5,
                "priceFactor": 0.5,
            })
            speed = float(meta.get("speed") or 0.0)
            avail = int(meta.get("avail") or 0)
            total = int(meta.get("total") or 0)
            green_factor = float(meta.get("greenFactor") or 0.5)

            queue_len = queue_by_station.get(sid, 0)
            utilization = 0.0
            if total > 0:
                utilization = (total - avail) / total

            high_congestion = (utilization > 0.82) or (
                queue_len >= max(2, total))

            can_start = (avail > 0) or (state == "Charging")
            if not can_start:
                # No available stall and not already charging
                continue

            target = None

            if emergency:
                base = 0.93 if speed >= FAST_KW_THRESHOLD else 0.90
                base += 0.05 * (green_factor - 0.5)
                base += bias["target_boost"]
                base -= bias["target_cut"]
                target = max(0.60, min(MAX_TARGET, base))

            elif not has_charged:
                # First charge: very important for completion.
                # Even under moderate congestion we still go for it.
                target = choose_target_soc(
                    speed_kw=speed,
                    soc=soc,
                    first_charge=True,
                    persona_bias=bias,
                    green_factor=green_factor,
                )

            else:
                # Not first charge, not emergency.
                # If it's green and not too congested, allow preventive top-ups.
                if green_factor >= 0.45 and not high_congestion:
                    cond_ok = soc <= preventive_soc
                else:
                    cond_ok = soc <= low_soc

                if cond_ok:
                    # Respect cooldown: if we recently charged them to a higher level, skip.
                    if not (cooldown_soc > 0.0 and soc >= cooldown_soc - 0.03):
                        target = choose_target_soc(
                            speed_kw=speed,
                            soc=soc,
                            first_charge=False,
                            persona_bias=bias,
                            green_factor=green_factor,
                        )

            if target is not None:
                target = stagger_target(cid, target)
                recs.append({
                    "customerId": cid,
                    "chargingRecommendations": [{
                        "nodeId": sid,
                        "chargeTo": target
                    }]
                })
                _HAS_CHARGED[cid] = True
                # Require them to drop ~10 pts before we charge again
                _RECENT_CHARGE_UNTIL[cid] = max(0.65, target - 0.10)
                continue

        # ---------- B) Arriving-next-tick-to-station logic ----------
        dest_station = arriving_next_tick_is_station(c, station_ids)
        if dest_station:
            sid = dest_station
            meta = station_meta.get(sid, {
                "avail": 0,
                "total": 0,
                "speed": 0.0,
                "greenFactor": 0.5,
                "priceFactor": 0.5,
            })
            avail = int(meta.get("avail") or 0)
            speed = float(meta.get("speed") or 0.0)
            total = int(meta.get("total") or 0)
            green_factor = float(meta.get("greenFactor") or 0.5)

            queue_len = queue_by_station.get(sid, 0)
            utilization = 0.0
            if total > 0:
                utilization = (total - avail) / total
            high_congestion = (utilization > 0.82) or (
                queue_len >= max(2, total))

            # For non-emergency cars, don't route more to a totally full/highly congested station.
            if avail <= 0 and not emergency and high_congestion:
                continue

            target = None

            if emergency:
                base = 0.93 if speed >= FAST_KW_THRESHOLD else 0.90
                base += 0.05 * (green_factor - 0.5)
                base += bias["target_boost"]
                base -= bias["target_cut"]
                target = max(0.60, min(MAX_TARGET, base))

            elif not has_charged:
                # First charge at arrival, unless congestion is extreme and SoC is still decent
                if not (high_congestion and soc > low_soc):
                    target = choose_target_soc(
                        speed_kw=speed,
                        soc=soc,
                        first_charge=True,
                        persona_bias=bias,
                        green_factor=green_factor,
                    )

            elif soc <= low_soc and not high_congestion:
                # Preventive top-up on arrival if not heavily congested
                if not (cooldown_soc > 0.0 and soc >= cooldown_soc - 0.03):
                    target = choose_target_soc(
                        speed_kw=speed,
                        soc=soc,
                        first_charge=False,
                        persona_bias=bias,
                        green_factor=green_factor,
                    )

            if target is not None:
                target = stagger_target(cid, target)
                recs.append({
                    "customerId": cid,
                    "chargingRecommendations": [{
                        "nodeId": dest_station,
                        "chargeTo": target
                    }]
                })
                _HAS_CHARGED[cid] = True
                _RECENT_CHARGE_UNTIL[cid] = max(0.65, target - 0.10)

    return recs

# ============================================================
# ADAPTERS FOR YOUR MAIN()
# ============================================================


def should_move_on_to_next_tick(game_response):
    """
    Compute recommendations based on the logs present in this game_response,
    stash them so generate_tick(...) can attach them to the NEXT tick object.
    """
    global _PENDING_RECS_FOR_NEXT_TICK

    updated_map = game_response.get("map", {}) or {}
    node_index, station_ids, station_meta = build_node_and_station_indexes(
        updated_map)

    # Determine current tick (max tick in logs)
    logs = game_response.get("customerLogs", []) or []
    current_tick = -1
    for entry in logs:
        for rec in entry.get("logs", []) or []:
            t = rec.get("tick")
            if isinstance(t, (int, float)) and t > current_tick:
                current_tick = t
    if current_tick < 0:
        current_tick = 0

    augment_station_meta_with_environment(
        game_response, current_tick, station_meta)

    customers_this_tick = customer_info_from_response(
        game_response, current_tick, node_index
    )
    recs = generate_customer_recommendations(
        customers_this_tick, station_ids, station_meta
    )

    if recs:
        print("→ Applying recommendations:",
              json.dumps(recs, indent=2))

    _PENDING_RECS_FOR_NEXT_TICK = recs
    return True


def generate_tick(map_obj, tick_no):
    global _PENDING_RECS_FOR_NEXT_TICK
    recs = _PENDING_RECS_FOR_NEXT_TICK or []
    tick = {
        "tick": tick_no,
        "customerRecommendations": recs
    }
    _PENDING_RECS_FOR_NEXT_TICK = []
    return tick


"<------------------------------------------------------------------------------------>"


IS_LOCAL = base_url.startswith("http://localhost")


def run_local_and_collect_ticks(map_name: str):
    """
    Run the whole game on the LOCAL docker engine using playToTick,
    while collecting every tick object (with recommendations) in a list.

    Returns:
        all_ticks:   [tick0, tick1, ..., tickN]
        final_score: the score from the last local response
    """
    local_base_url = "http://localhost:8080"
    client = ConsiditionClient(local_base_url, api_key)

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
    good_ticks = []
    all_ticks = []

    # First tick: no logs yet, just an empty recommendation set
    current_tick = generate_tick(map_obj, 0)
    all_ticks.append(current_tick)

    input_payload = {
        "mapName": map_name,
        "ticks": [current_tick],
        "playToTick": 0,
    }

    max_dev_ticks = 80
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

            # Local score (for debugging only)
            final_score = game_response.get("score", 0)

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

            # If we ever decide NOT to move on (you always return True today),
            # we would update with the same tick index i.
            updated_map = game_response.get("map", map_obj) or map_obj
            current_tick = generate_tick(updated_map, i)
            all_ticks.append(current_tick)

            input_payload = {
                "mapName": map_name,
                "ticks": [*good_ticks, current_tick],
                "playToTick": i,
            }

    print(f"[LOCAL] Final local dev score: {final_score}")
    return all_ticks, final_score


def submit_ticks_to_cloud(map_name: str, all_ticks):
    """
    Submit the full tick sequence in ONE batch to the cloud API.
    No playToTick here, just the deterministic replay.
    """
    cloud_base_url = "https://api.considition.com/"
    client = ConsiditionClient(cloud_base_url, api_key)

    payload = {
        "mapName": map_name,
        "ticks": all_ticks,
    }

    print(f"[CLOUD] Submitting {len(all_ticks)} ticks in one batch...")
    start = time.perf_counter()
    try:
        game_response = client.post_game(payload)
    except Exception as e:
        print(f"Error posting to cloud API: {e}")
        sys.exit(1)
    elapsed_ms = (time.perf_counter() - start) * 1000
    print(f"[CLOUD] Submission took: {elapsed_ms:.2f}ms")

    if not game_response:
        print("[CLOUD] Got no response from cloud")
        sys.exit(1)

    print("[CLOUD] Response keys:", list(game_response.keys()))
    cloud_score = game_response.get("score", 0)
    print(f"[CLOUD] Final cloud score: {cloud_score}")
    return cloud_score


def main():

    # 1) Run locally on docker, using playToTick, and collect all ticks.
    all_ticks, local_score = run_local_and_collect_ticks(map_name)
    print(f"Local dev score (for your eyes only): {local_score}")

    # 2) Submit the SAME tick sequence in one batch to the cloud.
    submit_ticks_to_cloud(map_name, all_ticks)


if __name__ == "__main__":
    main()
