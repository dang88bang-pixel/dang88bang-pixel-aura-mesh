"""Scenario engine: evacuation, tactical, architecture, event, research.

The evacuation model is an agent-based social-force / flow-field hybrid:

* A **flow field** (BFS over the navigation grid from every exit) gives each
  agent a globally sensible direction, so nobody gets stuck in a dead end.
* **Local repulsion** between agents and from walls produces realistic
  congestion at doorways (the interesting part for planners).
* A **smoke field** diffuses from the fire source, slows agents down and
  raises their toxicity dose; agents exceeding the dose become casualties.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np

from .world import WORLD, World

ScenarioType = Literal["evacuation", "tactical", "architecture", "event", "research"]
SCENARIO_TYPES: tuple[str, ...] = ("evacuation", "tactical", "architecture", "event", "research")


@dataclass
class ScenarioParams:
    """Everything the UI can tune before pressing Start."""

    scenario: str = "evacuation"
    people: int = 24
    smoke_density: float = 0.35            # 0..1
    fire_source: tuple[float, float] | None = None
    speed: float = 1.0                     # simulation speed multiplier
    duration: float = 180.0                # simulated seconds
    panic: float = 0.3                     # 0..1, raises desired speed + jostling
    exits: list[str] = field(default_factory=list)
    seed: int = 1234
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "ScenarioParams":
        fire = data.get("fire_source")
        if isinstance(fire, (list, tuple)) and len(fire) >= 2:
            fire_source: tuple[float, float] | None = (float(fire[0]), float(fire[1]))
        else:
            fire_source = None
        scenario = str(data.get("scenario", data.get("type", "evacuation"))).lower()
        if scenario not in SCENARIO_TYPES:
            scenario = "evacuation"
        return cls(
            scenario=scenario,
            people=int(np.clip(int(data.get("people", 24)), 0, 400)),
            smoke_density=float(np.clip(float(data.get("smoke_density", 0.35)), 0.0, 1.0)),
            fire_source=fire_source,
            speed=float(np.clip(float(data.get("speed", 1.0)), 0.1, 20.0)),
            duration=float(np.clip(float(data.get("duration", 180.0)), 5.0, 3600.0)),
            panic=float(np.clip(float(data.get("panic", 0.3)), 0.0, 1.0)),
            exits=list(data.get("exits", []) or []),
            seed=int(data.get("seed", 1234)),
            notes=str(data.get("notes", "")),
        )

    def as_dict(self) -> dict:
        return {
            "scenario": self.scenario,
            "people": self.people,
            "smoke_density": self.smoke_density,
            "fire_source": list(self.fire_source) if self.fire_source else None,
            "speed": self.speed,
            "duration": self.duration,
            "panic": self.panic,
            "exits": self.exits,
            "seed": self.seed,
            "notes": self.notes,
        }


@dataclass
class Agent:
    """One simulated person."""

    agent_id: int
    position: np.ndarray
    velocity: np.ndarray
    desired_speed: float
    group: str = "civilian"
    escaped: bool = False
    escaped_at: float = 0.0
    dose: float = 0.0
    casualty: bool = False
    path_length: float = 0.0

    def as_dict(self) -> dict:
        return {
            "id": self.agent_id,
            "x": round(float(self.position[0]), 3),
            "y": round(float(self.position[1]), 3),
            "vx": round(float(self.velocity[0]), 3),
            "vy": round(float(self.velocity[1]), 3),
            "group": self.group,
            "escaped": self.escaped,
            "casualty": self.casualty,
            "dose": round(self.dose, 3),
            "speed": round(float(np.linalg.norm(self.velocity)), 3),
        }


class FlowField:
    """BFS distance-to-exit field over a coarse traversability grid."""

    def __init__(self, world: World, resolution: float = 0.35, clearance: float = 0.32) -> None:
        self.world = world
        self.resolution = resolution
        x0, y0, x1, y1 = world.bounds()
        self.origin = (x0 - 1.0, y0 - 1.0)
        self.width = max(4, int((x1 - x0 + 2.0) / resolution))
        self.height = max(4, int((y1 - y0 + 2.0) / resolution))
        self.free = np.zeros((self.height, self.width), dtype=bool)
        for j in range(self.height):
            for i in range(self.width):
                wx, wy = self.cell_to_world(i, j)
                self.free[j, i] = world.is_free(wx, wy, clearance)
        self.distance = np.full((self.height, self.width), np.inf)

    def cell_to_world(self, i: int, j: int) -> tuple[float, float]:
        return (self.origin[0] + (i + 0.5) * self.resolution, self.origin[1] + (j + 0.5) * self.resolution)

    def world_to_cell(self, x: float, y: float) -> tuple[int, int]:
        return (int((x - self.origin[0]) / self.resolution), int((y - self.origin[1]) / self.resolution))

    def compute(self, targets: list[tuple[float, float]]) -> None:
        """Multi-source BFS (8-connected, diagonal cost sqrt(2))."""
        self.distance = np.full((self.height, self.width), np.inf)
        frontier: list[tuple[int, int]] = []
        for tx, ty in targets:
            i, j = self.world_to_cell(tx, ty)
            i = int(np.clip(i, 0, self.width - 1))
            j = int(np.clip(j, 0, self.height - 1))
            # snap onto the nearest free cell
            if not self.free[j, i]:
                best = None
                for dj in range(-4, 5):
                    for di in range(-4, 5):
                        nj, ni = j + dj, i + di
                        if 0 <= ni < self.width and 0 <= nj < self.height and self.free[nj, ni]:
                            d = di * di + dj * dj
                            if best is None or d < best[0]:
                                best = (d, ni, nj)
                if best:
                    i, j = best[1], best[2]
                else:
                    continue
            self.distance[j, i] = 0.0
            frontier.append((i, j))

        neighbours = [(-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
                      (-1, -1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (1, 1, 1.414)]
        head = 0
        while head < len(frontier):
            i, j = frontier[head]
            head += 1
            base = self.distance[j, i]
            for di, dj, cost in neighbours:
                ni, nj = i + di, j + dj
                if not (0 <= ni < self.width and 0 <= nj < self.height):
                    continue
                if not self.free[nj, ni]:
                    continue
                nd = base + cost * self.resolution
                if nd < self.distance[nj, ni] - 1e-9:
                    self.distance[nj, ni] = nd
                    frontier.append((ni, nj))

    def direction(self, x: float, y: float) -> np.ndarray:
        """Steepest descent of the distance field at a world point."""
        i, j = self.world_to_cell(x, y)
        if not (0 <= i < self.width and 0 <= j < self.height):
            return np.zeros(2)
        best_dir = np.zeros(2)
        best_val = self.distance[j, i]
        if not np.isfinite(best_val):
            best_val = np.inf
        for dj in (-1, 0, 1):
            for di in (-1, 0, 1):
                if di == 0 and dj == 0:
                    continue
                ni, nj = i + di, j + dj
                if not (0 <= ni < self.width and 0 <= nj < self.height):
                    continue
                val = self.distance[nj, ni]
                if val < best_val:
                    best_val = val
                    best_dir = np.array([float(di), float(dj)])
        norm = float(np.linalg.norm(best_dir))
        return best_dir / norm if norm > 1e-9 else np.zeros(2)

    def distance_at(self, x: float, y: float) -> float:
        i, j = self.world_to_cell(x, y)
        if not (0 <= i < self.width and 0 <= j < self.height):
            return float("inf")
        return float(self.distance[j, i])


class SmokeField:
    """Coarse diffusion-advection grid for smoke concentration (0..1)."""

    def __init__(self, world: World, resolution: float = 0.5) -> None:
        x0, y0, x1, y1 = world.bounds()
        self.resolution = resolution
        self.origin = (x0, y0)
        self.width = max(4, int((x1 - x0) / resolution))
        self.height = max(4, int((y1 - y0) / resolution))
        self.field = np.zeros((self.height, self.width), dtype=float)
        self.blocked = np.zeros((self.height, self.width), dtype=bool)
        for j in range(self.height):
            for i in range(self.width):
                wx = self.origin[0] + (i + 0.5) * resolution
                wy = self.origin[1] + (j + 0.5) * resolution
                self.blocked[j, i] = not world.is_free(wx, wy, 0.05)
        self.source: tuple[int, int] | None = None
        self.rate = 0.0

    def set_source(self, x: float, y: float, rate: float) -> None:
        i = int(np.clip((x - self.origin[0]) / self.resolution, 0, self.width - 1))
        j = int(np.clip((y - self.origin[1]) / self.resolution, 0, self.height - 1))
        self.source = (i, j)
        self.rate = float(rate)

    def step(self, dt: float, diffusion: float = 0.55) -> None:
        if self.source is not None and self.rate > 0:
            i, j = self.source
            self.field[j, i] = min(1.0, self.field[j, i] + self.rate * dt)
        f = self.field
        lap = (
            np.roll(f, 1, axis=0) + np.roll(f, -1, axis=0) +
            np.roll(f, 1, axis=1) + np.roll(f, -1, axis=1) - 4.0 * f
        )
        f = f + diffusion * dt * lap
        f += 0.06 * dt * np.roll(f, -1, axis=0)   # buoyant drift towards +y
        f[self.blocked] = 0.0
        self.field = np.clip(f * (1.0 - 0.008 * dt), 0.0, 1.0)

    def at(self, x: float, y: float) -> float:
        i = int((x - self.origin[0]) / self.resolution)
        j = int((y - self.origin[1]) / self.resolution)
        if not (0 <= i < self.width and 0 <= j < self.height):
            return 0.0
        return float(self.field[j, i])

    def as_dict(self, threshold: float = 0.04, max_cells: int = 1500) -> dict:
        ys, xs = np.where(self.field > threshold)
        cells = []
        if len(xs):
            if len(xs) > max_cells:
                idx = np.linspace(0, len(xs) - 1, max_cells).astype(int)
                xs, ys = xs[idx], ys[idx]
            for i, j in zip(xs, ys):
                cells.append([
                    round(self.origin[0] + (int(i) + 0.5) * self.resolution, 2),
                    round(self.origin[1] + (int(j) + 0.5) * self.resolution, 2),
                    round(float(self.field[j, i]), 3),
                ])
        return {
            "resolution": self.resolution,
            "cells": cells,
            "max_concentration": round(float(self.field.max()), 3),
            "mean_concentration": round(float(self.field.mean()), 4),
        }


class ScenarioRun:
    """A running (or finished) scenario instance."""

    def __init__(self, params: ScenarioParams, world: World | None = None, run_id: str | None = None) -> None:
        self.params = params
        self.world = world or WORLD
        self.run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        self.started_at = time.time()
        self.sim_time = 0.0
        self.finished = False
        self.paused = False
        self.reaped = False
        self.status = "running"
        self.rng = np.random.default_rng(params.seed)
        self.timeline: list[dict] = []
        self.metrics: dict[str, Any] = {}
        self.events: list[dict] = []

        self.flow = FlowField(self.world)
        exits = [(e.x, e.y) for e in self.world.exits
                 if not params.exits or e.name in params.exits]
        if not exits:
            exits = [(e.x, e.y) for e in self.world.exits]
        self.exit_points = exits
        self.flow.compute(exits)

        self.smoke = SmokeField(self.world)
        if params.scenario == "evacuation" and params.smoke_density > 0:
            fire = params.fire_source or self._pick_fire_source()
            self.fire_source = fire
            self.smoke.set_source(fire[0], fire[1], 0.5 * params.smoke_density + 0.08)
        else:
            self.fire_source = params.fire_source

        self.agents: list[Agent] = []
        self._spawn_agents()
        self.total_agents = len(self.agents)
        self.escape_times: list[float] = []
        self.congestion_history: list[dict] = []

    # ------------------------------------------------------------------
    def _pick_fire_source(self) -> tuple[float, float]:
        # pick a free point far from the exits so evacuation is non-trivial
        best = None
        for _ in range(64):
            p = self.world.random_free_point(self.rng, 0.5)
            d = self.flow.distance_at(p[0], p[1])
            if not np.isfinite(d):
                continue
            if best is None or d > best[0]:
                best = (d, p)
        return best[1] if best else (10.0, 7.0)

    def _spawn_agents(self) -> None:
        groups = ["civilian"] * self.params.people
        if self.params.scenario == "tactical":
            n_team = max(2, self.params.people // 6)
            groups = ["operator"] * n_team + ["civilian"] * (self.params.people - n_team)
        for i, group in enumerate(groups):
            pos = np.array(self.world.random_free_point(self.rng, 0.4), dtype=float)
            base_speed = 1.32 if group == "civilian" else 1.55
            speed = base_speed * float(self.rng.normal(1.0, 0.12)) * (1.0 + 0.35 * self.params.panic)
            self.agents.append(
                Agent(
                    agent_id=i,
                    position=pos,
                    velocity=np.zeros(2),
                    desired_speed=float(np.clip(speed, 0.5, 2.6)),
                    group=group,
                )
            )

    # ------------------------------------------------------------------
    def step(self, dt: float) -> dict:
        """Advance the simulation by ``dt`` simulated seconds."""
        if self.finished or self.paused:
            return self.snapshot()
        dt = float(np.clip(dt, 0.005, 0.5))
        self.sim_time += dt
        self.smoke.step(dt)

        positions = np.array([a.position for a in self.agents if not a.escaped]) if self.agents else np.zeros((0, 2))
        active = [a for a in self.agents if not a.escaped and not a.casualty]

        for agent in active:
            direction = self.flow.direction(float(agent.position[0]), float(agent.position[1]))
            if np.linalg.norm(direction) < 1e-6:
                direction = self._fallback_direction(agent)

            smoke_level = self.smoke.at(float(agent.position[0]), float(agent.position[1]))
            agent.dose += smoke_level * dt
            slowdown = 1.0 - 0.65 * smoke_level
            desired = direction * agent.desired_speed * max(0.2, slowdown)

            # social repulsion from nearby agents
            if len(positions):
                delta = agent.position - positions
                dist = np.linalg.norm(delta, axis=1)
                near = (dist > 1e-6) & (dist < 0.9)
                if np.any(near):
                    weights = ((0.9 - dist[near]) / 0.9) ** 2
                    push = (delta[near] / dist[near][:, None]) * weights[:, None]
                    desired = desired + push.sum(axis=0) * (0.55 + 0.5 * self.params.panic)

            # wall avoidance
            desired = desired + self._wall_push(agent.position)

            # jostling under panic
            if self.params.panic > 0:
                desired = desired + self.rng.normal(0.0, 0.12 * self.params.panic, 2)

            speed = float(np.linalg.norm(desired))
            if speed > agent.desired_speed * 1.3:
                desired = desired / speed * agent.desired_speed * 1.3
            agent.velocity = 0.72 * agent.velocity + 0.28 * desired
            step_vec = agent.velocity * dt
            candidate = agent.position + step_vec
            if self.world.is_free(float(candidate[0]), float(candidate[1]), 0.18):
                agent.path_length += float(np.linalg.norm(step_vec))
                agent.position = candidate
            else:
                # slide along the wall
                for axis in (0, 1):
                    trial = agent.position.copy()
                    trial[axis] += step_vec[axis]
                    if self.world.is_free(float(trial[0]), float(trial[1]), 0.18):
                        agent.position = trial
                        break
                agent.velocity *= 0.4

            if agent.dose > 12.0:
                agent.casualty = True
                self.events.append({"t": round(self.sim_time, 2), "type": "casualty", "agent": agent.agent_id})

            for ex, ey in self.exit_points:
                if math.hypot(agent.position[0] - ex, agent.position[1] - ey) < 1.1:
                    agent.escaped = True
                    agent.escaped_at = self.sim_time
                    self.escape_times.append(self.sim_time)
                    self.events.append({"t": round(self.sim_time, 2), "type": "escaped", "agent": agent.agent_id})
                    break

        self._record_congestion()
        remaining = sum(1 for a in self.agents if not a.escaped and not a.casualty)
        if remaining == 0 or self.sim_time >= self.params.duration:
            self.finish()
        if len(self.timeline) == 0 or self.sim_time - self.timeline[-1]["t"] >= 0.5:
            self.timeline.append(
                {
                    "t": round(self.sim_time, 2),
                    "escaped": sum(1 for a in self.agents if a.escaped),
                    "remaining": remaining,
                    "casualties": sum(1 for a in self.agents if a.casualty),
                    "smoke": round(float(self.smoke.field.mean()), 4),
                }
            )
            if len(self.timeline) > 2000:
                del self.timeline[: len(self.timeline) - 2000]
        return self.snapshot()

    def _fallback_direction(self, agent: Agent) -> np.ndarray:
        if not self.exit_points:
            return np.zeros(2)
        target = min(self.exit_points, key=lambda e: math.hypot(e[0] - agent.position[0], e[1] - agent.position[1]))
        d = np.array([target[0] - agent.position[0], target[1] - agent.position[1]])
        n = float(np.linalg.norm(d))
        return d / n if n > 1e-6 else np.zeros(2)

    def _wall_push(self, position: np.ndarray) -> np.ndarray:
        push = np.zeros(2)
        for angle in (0.0, math.pi / 2, math.pi, -math.pi / 2):
            d = self.world.raycast((position[0], position[1]), angle, 0.8)
            if d < 0.45:
                push -= np.array([math.cos(angle), math.sin(angle)]) * (0.45 - d) * 2.2
        return push

    def _record_congestion(self) -> None:
        active = [a for a in self.agents if not a.escaped and not a.casualty]
        if not active:
            return
        slow = [a for a in active if float(np.linalg.norm(a.velocity)) < 0.25 * a.desired_speed]
        if len(slow) >= max(3, len(active) // 6):
            cx = float(np.mean([a.position[0] for a in slow]))
            cy = float(np.mean([a.position[1] for a in slow]))
            self.congestion_history.append(
                {"t": round(self.sim_time, 2), "x": round(cx, 2), "y": round(cy, 2), "count": len(slow)}
            )
            if len(self.congestion_history) > 500:
                del self.congestion_history[:-500]

    # ------------------------------------------------------------------
    def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        self.status = "completed"
        escaped = [a for a in self.agents if a.escaped]
        casualties = [a for a in self.agents if a.casualty]
        times = sorted(a.escaped_at for a in escaped)
        self.metrics = {
            "run_id": self.run_id,
            "scenario": self.params.scenario,
            "total_agents": self.total_agents,
            "escaped": len(escaped),
            "casualties": len(casualties),
            "stranded": self.total_agents - len(escaped) - len(casualties),
            "sim_time": round(self.sim_time, 2),
            "evacuation_time_p50": round(times[len(times) // 2], 2) if times else None,
            "evacuation_time_p95": round(times[int(len(times) * 0.95) - 1], 2) if len(times) > 1 else None,
            "evacuation_time_max": round(times[-1], 2) if times else None,
            "mean_path_length": round(float(np.mean([a.path_length for a in self.agents])), 2) if self.agents else 0.0,
            "mean_smoke_dose": round(float(np.mean([a.dose for a in self.agents])), 3) if self.agents else 0.0,
            "peak_smoke": round(float(self.smoke.field.max()), 3),
            "bottlenecks": self._bottlenecks(),
        }

    def _bottlenecks(self) -> list[dict]:
        """Cluster congestion samples into named hotspots."""
        if not self.congestion_history:
            return []
        clusters: list[dict] = []
        for sample in self.congestion_history:
            placed = False
            for cluster in clusters:
                if math.hypot(cluster["x"] - sample["x"], cluster["y"] - sample["y"]) < 1.5:
                    n = cluster["samples"]
                    cluster["x"] = (cluster["x"] * n + sample["x"]) / (n + 1)
                    cluster["y"] = (cluster["y"] * n + sample["y"]) / (n + 1)
                    cluster["samples"] = n + 1
                    cluster["peak"] = max(cluster["peak"], sample["count"])
                    placed = True
                    break
            if not placed:
                clusters.append({"x": sample["x"], "y": sample["y"], "samples": 1, "peak": sample["count"]})
        clusters.sort(key=lambda c: (c["peak"], c["samples"]), reverse=True)
        for c in clusters:
            c["x"] = round(c["x"], 2)
            c["y"] = round(c["y"], 2)
        return clusters[:5]

    def stop(self) -> dict:
        """Finalise the run. Idempotent: `stopped` records that it was reaped."""
        self.status = "stopped" if not self.finished else self.status
        self.finish()
        self.reaped = True
        return self.metrics

    # ------------------------------------------------------------------
    def snapshot(self, include_smoke: bool = True) -> dict:
        active = [a for a in self.agents if not a.escaped]
        payload = {
            "run_id": self.run_id,
            "scenario": self.params.scenario,
            "status": self.status,
            "paused": self.paused,
            "finished": self.finished,
            "sim_time": round(self.sim_time, 2),
            "duration": self.params.duration,
            "progress": round(min(1.0, self.sim_time / max(1e-6, self.params.duration)), 4),
            "agents": [a.as_dict() for a in active[:250]],
            "escaped": sum(1 for a in self.agents if a.escaped),
            "casualties": sum(1 for a in self.agents if a.casualty),
            "total_agents": self.total_agents,
            "fire_source": list(self.fire_source) if self.fire_source else None,
            "exits": [{"x": e[0], "y": e[1]} for e in self.exit_points],
            "bottlenecks": self._bottlenecks(),
            "metrics": self.metrics,
        }
        if include_smoke and self.params.smoke_density > 0:
            payload["smoke"] = self.smoke.as_dict()
        return payload

    def people_ground_truth(self) -> list[np.ndarray]:
        """Positions + speeds handed to the sensor simulators."""
        return [
            np.array([a.position[0], a.position[1], float(np.linalg.norm(a.velocity))])
            for a in self.agents
            if not a.escaped
        ]


class ScenarioEngine:
    """Owns the active run and its history."""

    def __init__(self, world: World | None = None) -> None:
        self.world = world or WORLD
        self.active: ScenarioRun | None = None
        self.history: list[dict] = []

    def start(self, params: ScenarioParams, run_id: str | None = None) -> ScenarioRun:
        if self.active and not self.active.finished:
            self.stop()
        self.active = ScenarioRun(params, self.world, run_id=run_id)
        return self.active

    def stop(self) -> dict | None:
        """Stop the active run.

        Returns ``None`` when nothing is running *or* the run was already
        stopped, so the REST layer can answer 409 instead of silently
        re-reporting a finished run and duplicating its history entry.
        """
        if not self.active or self.active.reaped:
            return None
        metrics = self.active.stop()
        if not self.history or self.history[-1].get("run_id") != metrics.get("run_id"):
            self.history.append(metrics)
            if len(self.history) > 50:
                del self.history[:-50]
        return metrics

    def pause(self, paused: bool = True) -> bool:
        if self.active:
            self.active.paused = paused
            self.active.status = "paused" if paused else "running"
            return True
        return False

    def step(self, dt: float) -> dict | None:
        if not self.active:
            return None
        speed = self.active.params.speed
        snapshot = self.active.step(dt * speed)
        if self.active.finished and self.active.metrics and (
            not self.history or self.history[-1].get("run_id") != self.active.run_id
        ):
            self.history.append(self.active.metrics)
        return snapshot

    def snapshot(self) -> dict | None:
        return self.active.snapshot() if self.active else None

    def people(self) -> list[np.ndarray]:
        return self.active.people_ground_truth() if self.active else []
