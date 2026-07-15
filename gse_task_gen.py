"""Derive GSE tasks automatically from a flight schedule.

The user's only real input is the flight schedule. From each flight we know the
stand, the time, the aircraft type, the passenger count, the fuel uplift and
whether catering is needed - which is enough to decide *which* ground-service
vehicles are required and *when* they should start, relative to the flight.

The per-flight service durations / resupply / operator TAT are still computed by
``gse_task_timing`` at routing time; this module only decides the set of tasks
(class, start time, origin depot, destination stand) so the scheduler can size
the fleet. Keeping it separate means the studio, the CLI and the sample
generator all expand flights into tasks the same way.
"""

from __future__ import annotations

from gse_models import GSEClass
from gse_task_timing import FlightMeta, compute_service_minutes

# Short suffix used to build readable task IDs like "FL401-FUEL".
_SHORT = {
    GSEClass.BUS: "BUS",
    GSEClass.FUEL: "FUEL",
    GSEClass.STAIRS: "STR",
    GSEClass.LUGGAGE: "BAG",
    GSEClass.FOOD: "FOOD",
    GSEClass.SIGNALS: "SIG",
}

# Minutes BEFORE the scheduled departure that each service vehicle is dispatched.
# Ordered so the turnaround flows: catering/stairs first, fuel + bags, then
# boarding by bus, and finally the marshaller for pushback.
_DEP_OFFSET = {
    GSEClass.FOOD: 50,
    GSEClass.STAIRS: 45,
    GSEClass.FUEL: 40,
    GSEClass.LUGGAGE: 35,
    GSEClass.BUS: 25,
    GSEClass.SIGNALS: 8,
}

# Minutes AFTER the scheduled arrival that each service starts (the marshaller
# guides the aircraft in a touch before on-block, hence the negative value).
_ARR_OFFSET = {
    GSEClass.SIGNALS: -2,
    GSEClass.STAIRS: 2,
    GSEClass.LUGGAGE: 3,
    GSEClass.BUS: 5,
    GSEClass.FOOD: 6,
    GSEClass.FUEL: 5,
}

_TRUE_TOKENS = frozenset({"yes", "y", "true", "1"})


# ---------------------------------------------------------------------------
# Small normalisers (accept dicts from the studio or tuples from the generator)
# ---------------------------------------------------------------------------
def _parse_hhmm(value: object) -> int:
    """Return minutes-since-midnight for an 'HH:MM' string / time / datetime."""
    if value is None:
        return 0
    if hasattr(value, "hour") and hasattr(value, "minute"):  # time/datetime
        return int(value.hour) * 60 + int(value.minute)
    text = str(value).strip()
    if not text:
        return 0
    # Excel may hand back "07:05:00" or "07:05".
    parts = text.split(":")
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        return 0


def _fmt_hhmm(minutes: int) -> str:
    minutes = max(0, min(24 * 60 - 1, minutes))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _to_meta(flight: dict) -> FlightMeta:
    """Build a FlightMeta from a plain flight dict (studio/sheet/sample)."""
    def get(*keys):
        for k in keys:
            if k in flight and flight[k] not in (None, ""):
                return flight[k]
        return None

    ac = get("aircraft_type")
    pax = get("pax")
    fuel = get("fuel_litres")
    cat = get("requires_catering")
    return FlightMeta(
        flight=str(get("flight") or "").strip(),
        direction=str(get("direction") or "departure").strip().lower(),
        stand=str(get("stand") or "").strip(),
        aircraft_type="NB" if ac is None else str(ac).strip().upper(),
        pax=150 if pax is None else int(float(pax)),
        fuel_litres=0 if fuel is None else int(float(fuel)),
        requires_catering=True if cat is None else str(cat).strip().lower() in _TRUE_TOKENS,
    )


def _truthy(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in _TRUE_TOKENS


def _normalise_nodes(nodes) -> list[dict]:
    """Accept node dicts or (id, kind, ..., x, y, ...) tuples -> uniform dicts.

    A ``has_bridge`` flag (jet bridge) is read when present: dict key
    ``has_bridge`` or the 7th tuple element (id, kind, desc, x, y, cap, bridge).
    """
    out = []
    for n in nodes:
        if isinstance(n, dict):
            out.append({"id": str(n["id"]).strip(), "kind": str(n.get("kind", "")).strip().lower(),
                        "x": float(n.get("x", 0) or 0), "y": float(n.get("y", 0) or 0),
                        "has_bridge": _truthy(n.get("has_bridge"))})
        else:  # tuple from generate_sample_inputs: (id, kind, desc, x, y, cap[, bridge])
            out.append({"id": str(n[0]).strip(), "kind": str(n[1]).strip().lower(),
                        "x": float(n[3]), "y": float(n[4]),
                        "has_bridge": _truthy(n[6]) if len(n) > 6 else False})
    return out


def _nearest(candidates: list[dict], x: float, y: float) -> dict | None:
    best, best_d = None, float("inf")
    for c in candidates:
        d = (c["x"] - x) ** 2 + (c["y"] - y) ** 2
        if d < best_d:
            best, best_d = c, d
    return best


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def generate_tasks(flights, nodes) -> list[dict]:
    """Expand a flight schedule into GSE task rows (gse_tasks sheet schema).

    ``flights`` and ``nodes`` are lists of dicts (or tuples for nodes). Returns a
    list of dicts with keys: task_id, flight, gse_class, start_time, duration_min,
    origin, destination, transit, notes.
    """
    node_list = _normalise_nodes(nodes)
    by_id = {n["id"]: n for n in node_list}
    # Stands served by a jet bridge: passengers board through the bridge, so they
    # need neither passenger stairs nor an apron bus.
    bridge_stands = {n["id"] for n in node_list if n.get("has_bridge")}
    origins = [n for n in node_list if n["kind"] == "origin"]
    # A "crew" origin (terminal) is preferred for buses; the rest act as depots.
    crew = next((o for o in origins if "CREW" in o["id"].upper() or "TERMINAL" in o["id"].upper()), None)
    depots = [o for o in origins if o is not crew] or origins

    tasks: list[dict] = []
    for raw in flights:
        meta = _to_meta(raw)
        if not meta.flight or not meta.stand:
            continue
        base = _parse_hhmm(raw.get("scheduled_time") if isinstance(raw, dict) else None)
        stand_node = by_id.get(meta.stand)
        sx = stand_node["x"] if stand_node else 0.0
        sy = stand_node["y"] if stand_node else 0.0
        is_dep = meta.direction == "departure"
        offsets = _DEP_OFFSET if is_dep else _ARR_OFFSET

        has_bridge = meta.stand in bridge_stands
        for cls in GSEClass:
            # A jet bridge removes the need for passenger stairs and apron buses.
            if has_bridge and cls in (GSEClass.STAIRS, GSEClass.BUS):
                continue
            # compute_service_minutes returns 0 for classes that don't apply to
            # this flight (e.g. fuel on a zero-uplift arrival, catering when not
            # required), so this is where applicability is decided.
            service = compute_service_minutes(cls, meta)
            if service <= 0:
                continue

            offset = offsets.get(cls, 30 if is_dep else 3)
            start_min = base - offset if is_dep else base + offset

            if cls == GSEClass.BUS and crew is not None:
                origin = crew["id"]
            else:
                near = _nearest(depots, sx, sy)
                origin = near["id"] if near else (origins[0]["id"] if origins else meta.stand)

            tasks.append({
                "task_id": f"{meta.flight}-{_SHORT[cls]}",
                "flight": meta.flight,
                "gse_class": cls.value,
                "start_time": _fmt_hhmm(start_min),
                "duration_min": int(service),
                "origin": origin,
                "destination": meta.stand,
                "transit": "",
                "notes": "auto" if is_dep else "auto-arrival",
            })
    return tasks
