import sys
import time
import json
from client import ConsiditionClient

# --- GLOBAL CONFIG (not used directly by main) --------------------------------------

api_key = "e32ec928-ac93-466b-8cd5-ac151ef5f7fe"
base_url = "http://localhost:8080"
# base_url = "https://api.considition.com"
# map_name = "Turbohill"
# map_name = "Clutchfield"
# map_name = "Batterytown"
map_name = "Thunderroad"

# === Internal cache so the main() you provided works ===
# Filled in should_move_on_to_next_tick(); consumed in generate_tick()
_PENDING_RECS_FOR_NEXT_TICK = []

# Minimal memory to avoid immediate re-topups
# customerId -> soc_threshold_to_allow_next_charge (e.g. 0.75)
_RECENT_CHARGE_UNTIL = {}

# Track if a customer has charged at least once (for completion score)
# customerId -> bool
_HAS_CHARGED = {}

# Optional: first tick we saw each customer (could be used for extra heuristics)
_FIRST_SEEN_TICK = {}


# --- BASIC MAP/STATION INDEXING -----------------------------------------------------


def is_charging_station(node: dict) -> bool:
    """
    A node is a charging station if its 'target.Type' is 'ChargingStation'
    (case/space insensitive).
    """
    tgt = node.get("target") or {}
    t = str(tgt.get("Type", "")).replace(" ", "").lower()
    return t == "chargingstation"


def build_node_and_station_indexes(map_obj):
    """
    Build quick lookup structures from the map object.

    Returns:
      node_index:   { nodeId -> node }
      station_ids:  set(nodeId)
      station_meta: { nodeId -> {
            "avail": int,
            "total": int,
            "speed": float,
            "is_green": bool,
            "zoneId": str | None
        } }
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
                # Static green flag on station
                "is_green": bool(
                    tgt.get("isGreen")
                    or tgt.get("IsGreen")
                    or tgt.get("green")
                    or False
                ),
                # Try to read zone id if present
                "zoneId": str(n.get("zoneId") or n.get("zone") or "")
                if (n.get("zoneId") or n.get("zone"))
                else None,
            }

    return node_index, station_ids, station_meta


# --- CUSTOMER LOG PARSING (current tick snapshot) -----------------------------------


def _update_memory_from_log_entry(cid: str, rec: dict):
    """
    Update global memories (_HAS_CHARGED etc.) based on a single log record.
    If ticksSpentCharging > 0 or state indicates charging, we mark the customer
    as 'has charged at least once'.
    """
    state = (rec.get("state") or "").strip()
    t_charge = rec.get("ticksSpentCharging")
    try:
        t_charge = float(t_charge) if t_charge is not None else 0.0
    except Exception:
        t_charge = 0.0

    if t_charge > 0 or state in {"Charging", "DoneCharging"}:
        _HAS_CHARGED[cid] = True


def customer_info_from_response(game_response, current_tick, node_index):
    """
    Produce per-customer snapshot at <= current_tick from customerLogs.

    Also updates:
      - _HAS_CHARGED (by inspecting charging states)
      - _FIRST_SEEN_TICK
    """
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

        # Track first-seen tick
        if cid not in _FIRST_SEEN_TICK:
            _FIRST_SEEN_TICK[cid] = current_tick

        # Update charging memory from the chosen record
        _update_memory_from_log_entry(cid, best)

        px = best.get("posX")
        py = best.get("posY")
        node_id = best.get("node")
        edge_id = best.get("edge")  # e.g. "1.2-->1.3"

        # Fill in coordinates from node if missing
        if (px is None or py is None) and node_id is not None:
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
            "node": str(node_id) if node_id is not None else None,
            "edge": edge_id,
            "persona": best.get("persona"),  # if available
        })

    # Debug / tuning printout (can be commented out once stable)
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
                f"State: {or_na(c['state']):<20} | Node: {or_na(c['node']):<6} | "
                f"Edge: {or_na(c['edge']):<16} | Persona: {or_na(c['persona'])}"
            )

    return customers


# --- LIGHTWEIGHT DECISION HELPERS ---------------------------------------------------

# States where we consider issuing a charge recommendation if the customer is at a station
CHARGEABLE_STATES = {
    "Charging",
    "Waiting",
    "Idle",
    "Home",
    "TransitioningToNode",
}


def is_at_station_chargeable(c: dict, station_ids) -> bool:
    """
    Customer is at a station and in a state where charging is possible.
    """
    node = c.get("node")
    edge = (c.get("edge") or "").strip()
    state = (c.get("state") or "").strip()
    if node not in station_ids:
        return False
    if edge and edge != "N/A":
        # If they’re on an edge, they’re travelling, not at the stall yet
        return False
    return state in CHARGEABLE_STATES


def arriving_next_tick_is_station(c: dict, station_ids):
    """
    If the current edge ends in a station node, return that nodeId, else None.
    """
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


# --- ENVIRONMENT / WEATHER / GREENNESS ---------------------------------------------


def augment_station_meta_with_environment(game_response, current_tick, station_meta):
    """
    Add dynamic environment-related keys to each station's meta:
      - greenFactor: 0..1, how "green" charging is *right now*
      - priceFactor: 0..1, higher = more expensive (fallback ~0.5)

    Uses:
      - Station's own is_green flag.
      - Time-of-day (solar window 06–18, extra around midday).
      - Optional global weather fields in game_response["weather"]:
            cloudCover (0..1), windStrength (0..1)
    """
    weather = game_response.get("weather") or {}
    # Try a few possible key spellings
    cloud = weather.get("cloudCover")
    if cloud is None:
        cloud = weather.get("CloudCover")
    wind = weather.get("windStrength")
    if wind is None:
        wind = weather.get("WindStrength")

    try:
        cloud = float(cloud)
    except Exception:
        cloud = 0.3  # mildly cloudy default
    try:
        wind = float(wind)
    except Exception:
        wind = 0.4  # mildly windy default

    # Time-of-day from ticks: 1 tick = 5 minutes
    minutes = current_tick * 5
    hour = (minutes // 60) % 24

    # Solar window & midday boost as per docs (6–18, extra 10–15)
    in_solar_window = 6 <= hour < 18
    in_midday_peak = 10 <= hour < 15

    for sid, meta in station_meta.items():
        is_green = bool(meta.get("is_green"))
        base = 0.35  # base greenness for generic station

        # Static green station bonus
        if is_green:
            base += 0.25  # dedicated green infrastructure

        # Time-of-day influence
        if in_solar_window:
            base += 0.15
        if in_midday_peak:
            base += 0.10

        # Weather influence: more wind, less clouds → greener
        base += 0.25 * wind
        base -= 0.20 * cloud

        # Clamp to [0, 1]
        green_factor = max(0.0, min(1.0, base))

        # For now, we don't have reliable per-zone price in the DTO,
        # so we use a neutral priceFactor and leave hooks to adjust later.
        price_factor = 0.5

        meta["greenFactor"] = green_factor
        meta["priceFactor"] = price_factor


# --- RECOMMENDER – GREEN-AWARE, QUEUE-AWARE, COMPLETION-FOCUSED --------------------

def generate_customer_recommendations(customers_this_tick, station_ids, station_meta):
    """
    Heuristic policy heavily biased toward CUSTOMER COMPLETION,
    while being aware of:
      - greener times/places to charge
      - queueing / congestion at stations
      - personas affecting charge aggressiveness

    MAIN SOC THRESHOLDS
    --------------------
      EMERGENCY_SOC            = 0.25  → must charge ASAP.
      LOW_SOC                  = 0.35  → prefer charging if at/arriving to station.
      PREVENTIVE_AT_STATION    = 0.45  → at station, we like to top up below this
                                          when conditions are green/low congestion.
      FIRST_CHARGE_MAX_SOC     = 0.85  → if never charged and SoC ≤ this at station,
                                          we strongly want first charge.

    Station meta extras:
      - station_meta[sid]["greenFactor"]  ∈ [0, 1]
      - station_meta[sid]["priceFactor"]  ∈ [0, 1]
    """
    recs = []

    # Thresholds
    EMERGENCY_SOC = 0.25
    LOW_SOC = 0.35
    PREVENTIVE_AT_STATION = 0.45
    FIRST_CHARGE_MAX_SOC = 0.85
    MAX_TARGET = 0.95

    # Charger speed split
    FAST_KW_THRESHOLD = 150.0

    # Base targets (before green/persona tweaks)
    FIRST_FAST_TARGET = 0.90
    FIRST_SLOW_TARGET = 0.86
    NORMAL_FAST_TARGET = 0.84
    NORMAL_SLOW_TARGET = 0.80

    # --- Queue estimation: how many are currently Charging/Waiting per station -----
    queue_by_station = {}
    for c in customers_this_tick:
        node = c.get("node")
        if node in station_ids:
            state = (c.get("state") or "").strip()
            if state in {"Charging", "Waiting"}:
                queue_by_station[node] = queue_by_station.get(node, 0) + 1

    def persona_adjustment(persona: str, green_factor: float,
                           price_factor: float, base_target: float) -> float:
        """
        Tiny persona-based and green-based tweaks around the base target.
        """
        if not persona:
            persona = ""
        p = persona.lower()

        t = base_target

        # Eco-conscious: charge more when it's green
        if "eco" in p:
            t += 0.04 * green_factor  # up to +0.04

        # Cost-sensitive: charge slightly less, especially if not very green
        if "cost" in p:
            t -= 0.03 * (1.0 - green_factor)  # more reduction when not green

        # Dislikes driving / stressed: keep charges slightly shorter
        if "stress" in p or "dislikesdriving" in p or "dislikes driving" in p:
            t -= 0.02

        # We could use price_factor here later if the API exposes it meaningfully.
        return t

    def choose_target_soc(speed_kw: float,
                          current_soc: float,
                          first_charge: bool,
                          persona: str,
                          green_factor: float,
                          price_factor: float) -> float:
        """
        Pick a target SoC depending on speed, first charge, persona, and greenness.
        """
        if first_charge:
            base = FIRST_FAST_TARGET if speed_kw >= FAST_KW_THRESHOLD else FIRST_SLOW_TARGET
        else:
            base = NORMAL_FAST_TARGET if speed_kw >= FAST_KW_THRESHOLD else NORMAL_SLOW_TARGET

        # Green boosts the base slightly (even for neutral personas)
        base += 0.06 * (green_factor - 0.5)  # from about -0.03 to +0.03

        base = persona_adjustment(persona, green_factor, price_factor, base)

        # Ensure we gain at least +12 percentage points if we bother to charge
        target = max(base, current_soc + 0.12)
        return max(0.60, min(MAX_TARGET, target))

    def stagger_target(cid: str, target: float) -> float:
        """
        Small deterministic stagger to avoid perfect synchronisation.
        """
        h = hash(cid) % 7  # 0..6
        delta = (h - 3) * 0.01  # -0.03 .. +0.03
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
        has_charged = _HAS_CHARGED.get(cid, False)
        cooldown_soc = float(_RECENT_CHARGE_UNTIL.get(cid, 0.0) or 0.0)

        emergency = soc <= EMERGENCY_SOC

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
            price_factor = float(meta.get("priceFactor") or 0.5)

            queue_len = queue_by_station.get(sid, 0)
            utilization = 0.0
            if total > 0:
                utilization = (total - avail) / total

            # High congestion if almost all stalls used AND some waiting
            high_congestion = (utilization > 0.8) or (
                queue_len >= max(1, total))

            can_start = (avail > 0) or (state == "Charging")
            if not can_start:
                continue

            target = None

            # (1) Emergency: charge regardless of congestion.
            if emergency:
                base = 0.93 if speed >= FAST_KW_THRESHOLD else 0.90
                base += 0.04 * (green_factor - 0.5)
                target = max(0.60, min(MAX_TARGET, base))

            # (2) First-ever charge: we really want this, even if somewhat congested.
            elif not has_charged and soc <= FIRST_CHARGE_MAX_SOC:
                target = choose_target_soc(
                    speed_kw=speed,
                    current_soc=soc,
                    first_charge=True,
                    persona=persona,
                    green_factor=green_factor,
                    price_factor=price_factor,
                )

            # (3) Preventive charging when at station:
            else:
                # If it's quite green and not too congested, allow top-ups up to ~0.45.
                if green_factor >= 0.4 and not high_congestion:
                    cond_ok = soc <= PREVENTIVE_AT_STATION
                else:
                    # At night / not green / congested → only charge when quite low.
                    cond_ok = soc <= LOW_SOC

                if cond_ok:
                    # Respect cooldown: if we recently charged them to a higher level, skip.
                    if not (cooldown_soc > 0.0 and soc >= cooldown_soc - 0.03):
                        target = choose_target_soc(
                            speed_kw=speed,
                            current_soc=soc,
                            first_charge=False,
                            persona=persona,
                            green_factor=green_factor,
                            price_factor=price_factor,
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
                # Cooldown: require SoC to drop ~10 points below last target
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
            price_factor = float(meta.get("priceFactor") or 0.5)

            queue_len = queue_by_station.get(sid, 0)
            utilization = 0.0
            if total > 0:
                utilization = (total - avail) / total
            high_congestion = (utilization > 0.8) or (
                queue_len >= max(1, total))

            if avail <= 0 and not emergency:
                # If we're not in emergency, don't route more cars to a full station.
                continue

            target = None

            # (1) Emergency: they are arriving very low → definitely charge.
            if emergency:
                base = 0.93 if speed >= FAST_KW_THRESHOLD else 0.90
                base += 0.04 * (green_factor - 0.5)
                target = max(0.60, min(MAX_TARGET, base))

            # (2) First-ever charge on arrival:
            elif not has_charged and soc <= FIRST_CHARGE_MAX_SOC:
                # If congestion is very high and SoC is still decent, we might skip,
                # but generally we want that first charge:
                if not (high_congestion and soc > LOW_SOC):
                    target = choose_target_soc(
                        speed_kw=speed,
                        current_soc=soc,
                        first_charge=True,
                        persona=persona,
                        green_factor=green_factor,
                        price_factor=price_factor,
                    )

            # (3) Preventive: they are a bit low and station is not too congested.
            elif soc <= LOW_SOC and not high_congestion:
                if not (cooldown_soc > 0.0 and soc >= cooldown_soc - 0.03):
                    target = choose_target_soc(
                        speed_kw=speed,
                        current_soc=soc,
                        first_charge=False,
                        persona=persona,
                        green_factor=green_factor,
                        price_factor=price_factor,
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


# === ADAPTERS TO MATCH YOUR MAIN() ==================================================


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

    # Inject dynamic environment info (time-of-day, weather) into station_meta
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


def main():
    api_key = "e32ec928-ac93-466b-8cd5-ac151ef5f7fe"
    base_url = "http://localhost:8080"
    # base_url = "https://api.considition.com/api/"
    # map_name = "Turbohill"
    # map_name = "Clutchfield"
    # map_name = "Batterytown"
    map_name = "Thunderroad"

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

    max_dev_ticks = 100

    for i in range(max_dev_ticks):
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
