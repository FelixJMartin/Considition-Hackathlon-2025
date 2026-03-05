# Considition 2025 — EV Charging Optimization

**Team**: Alex & Felix | **Duration**: One week (Nov 3–13, 2025)  
**Organizers**: Consid AB × Vattenfall AB | **Participants**: 230+ teams

---

##  The Challenge

Develop an AI strategy to optimize electric vehicle charging across urban networks.

**Objectives**:
-  Maximize grid efficiency and renewable energy usage
-  Maintain customer satisfaction (minimize wait times, range anxiety)
-  Minimize costs (energy pricing, congestion)
-  Prioritize green charging (solar/wind availability)

**Constraints**: Dynamic demand, limited capacity, real-time weather, time-varying pricing

---

##  Our Solution

### Three-Layer Strategy

**1. State-of-Charge (SoC) Management**
- Emergency: <35% → immediate charging
- Preventive: <65% → opportunistic charging
- Persona-based targets: 87–97% (varies by customer type & charger speed)

**2. Smart Station Selection**
```python
green_factor = base + solar_bonus + wind_boost - cloud_penalty
if congestion > 90% and soc > emergency:
    skip_station()  # Avoid queues
```

**3. Customer Persona Modeling**
- **Eco-conscious**: +3% target for green stations
- **Cost-sensitive**: -2% target to save money
- **Stress-averse**: -2% to reduce range anxiety

---

##  Key Features

### Dynamic Green Energy Scoring
Weather + time-of-day → green factor (0.0–1.0)
- Solar peak (10am–3pm): +0.10
- Wind strength: +0.25×wind
- Cloud cover: -0.20×clouds

### Adaptive Charging Targets

| Scenario | Fast (≥150kW) | Slow (<150kW) |
|----------|---------------|---------------|
| First charge | 93% | 90% |
| Regular | 90% | 87% |
| Emergency | 96% | 93% |

### Memory & Learning
- Track charging history per customer
- Estimate consumption rate (exponential moving average)
- Cooldown period to prevent over-charging

---

## 📈 Example Results
```
Final State Distribution:
  Home: 487 | Charging: 156 | Moving: 98 | Waiting: 23

Average final SoC: 84.7%
Customers charged: 612/650 (94.2%)
```

---

##  Tech Stack

**Python** | RESTful API | Event-driven tick simulation

**Core logic**:
1. Parse game state → extract customer positions/SoC
2. Score stations → green factor × availability × speed
3. Generate recommendations → customer→station→target SoC
4. Submit → local validation → cloud competition

---

##  Real-World Impact

This addresses:
- Smart city EV infrastructure planning
- Grid stability with renewable energy
- Fleet logistics for electric vehicles

---

##  Competition

**Considition 2025**: Annual AI hackathon by Consid + Vattenfall  
**Focus**: Sustainable energy & smart cities  
**Format**: One-week practice + final night live leaderboard

---

**TL;DR**: Built a reactive AI system that routes EVs to optimal charging stations based on battery level, customer persona, green energy availability, and congestion. Achieved 94% customer coverage with balanced cost/sustainability trade-offs.
