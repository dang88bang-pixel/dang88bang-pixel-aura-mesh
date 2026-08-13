/**
 * Server-side demo scene.
 *
 * Produces telemetry frames in exactly the shape the Python agent emits, so
 * the visualiser can be developed, demoed and screenshotted with no hardware
 * and no agent running. The geometry matches `edge-agent/aura/world.py`.
 */

const WALLS = [
  // outer shell (gap on the south wall = main entrance, gap east = fire exit)
  [0, 0, 9, 0], [11, 0, 20, 0],
  [0, 14, 20, 14],
  [0, 0, 0, 14],
  [20, 0, 20, 6], [20, 8, 20, 14],
  // north-west office
  [0.2, 8, 3, 8], [4.2, 8, 7, 8], [0.2, 13.8, 7, 13.8], [0.2, 8, 0.2, 13.8], [7, 8, 7, 13.8],
  // north-east lab
  [12, 8, 15, 8], [16.2, 8, 19.8, 8], [12, 13.8, 19.8, 13.8], [12, 8, 12, 13.8], [19.8, 8, 19.8, 13.8],
  // south-west storage
  [0.2, 0.2, 5.5, 0.2], [0.2, 5.5, 5.5, 5.5], [0.2, 0.2, 0.2, 5.5],
  [5.5, 0.2, 5.5, 2], [5.5, 3.2, 5.5, 5.5],
  // server closet
  [13.5, 2, 17.5, 2], [13.5, 5.5, 17.5, 5.5], [17.5, 2, 17.5, 5.5],
  [13.5, 2, 13.5, 3], [13.5, 4.2, 13.5, 5.5],
];

const EXITS = [
  { name: 'Haupteingang', x: 10.0, y: -0.4 },
  { name: 'Notausgang Ost', x: 20.4, y: 7.0 },
];

const TOKENS = {
  'AA:BB:CC:00:01': { label: 'Token-Flur', x: 10.0, y: 6.5 },
  'AA:BB:CC:00:02': { label: 'Token-Buero', x: 3.5, y: 11.0 },
  'AA:BB:CC:00:03': { label: 'Token-Labor', x: 16.0, y: 11.0 },
  'AA:BB:CC:00:04': { label: 'Token-Lager', x: 2.8, y: 2.8 },
};

const ANCHORS = {
  'ANCHOR-A': [0.3, 0.3, 2.4],
  'ANCHOR-B': [19.7, 0.3, 2.4],
  'ANCHOR-C': [19.7, 13.7, 2.4],
  'ANCHOR-D': [0.3, 13.7, 2.4],
};

const PATH = [
  [2.5, 2.5], [8.0, 2.5], [10.0, 6.5], [3.6, 6.5], [3.6, 10.5],
  [10.0, 6.8], [15.6, 6.5], [15.6, 10.5], [10.0, 6.5], [2.5, 2.5],
];

function raycast(ox, oy, angle, maxRange) {
  const dx = Math.cos(angle);
  const dy = Math.sin(angle);
  let best = maxRange;
  for (const [x1, y1, x2, y2] of WALLS) {
    const sx = x2 - x1;
    const sy = y2 - y1;
    const denominator = dx * sy - dy * sx;
    if (Math.abs(denominator) < 1e-12) continue;
    const t = ((x1 - ox) * sy - (y1 - oy) * sx) / denominator;
    const u = ((x1 - ox) * dy - (y1 - oy) * dx) / denominator;
    if (t > 1e-3 && t < best && u >= 0 && u <= 1) best = t;
  }
  return best;
}

function wallsBetween(ax, ay, bx, by) {
  const rx = bx - ax;
  const ry = by - ay;
  let count = 0;
  for (const [x1, y1, x2, y2] of WALLS) {
    const sx = x2 - x1;
    const sy = y2 - y1;
    const denominator = rx * sy - ry * sx;
    if (Math.abs(denominator) < 1e-12) continue;
    const t = ((x1 - ax) * sy - (y1 - ay) * sx) / denominator;
    const u = ((x1 - ax) * ry - (y1 - ay) * rx) / denominator;
    if (t > 0 && t < 1 && u >= 0 && u <= 1) count++;
  }
  return count;
}

export class DemoSimulator {
  constructor() {
    this.t0 = Date.now();
    this.waypoint = 0;
    this.position = [...PATH[0]];
    this.yaw = 0;
    this.iteration = 0;
    this.cloud = [];
    this.people = this.#spawnPeople(6);
    this.scenarioActive = false;
    this.scenarioTime = 0;
    this.battery = 92;
  }

  #spawnPeople(count) {
    const spots = [[3.5, 10.5], [16.0, 10.5], [10.0, 4.0], [2.8, 3.0], [15.5, 3.5], [8.0, 9.0]];
    return spots.slice(0, count).map((p, i) => ({
      id: `p-${i + 1}`,
      x: p[0],
      y: p[1],
      phase: Math.random() * Math.PI * 2,
      respiration: 0.24 + Math.random() * 0.1,
    }));
  }

  #advance(dt) {
    const target = PATH[(this.waypoint + 1) % PATH.length];
    const dx = target[0] - this.position[0];
    const dy = target[1] - this.position[1];
    const distance = Math.hypot(dx, dy);
    if (distance < 0.25) {
      this.waypoint = (this.waypoint + 1) % PATH.length;
      return;
    }
    const desiredYaw = Math.atan2(dy, dx);
    let yawError = desiredYaw - this.yaw;
    while (yawError > Math.PI) yawError -= 2 * Math.PI;
    while (yawError < -Math.PI) yawError += 2 * Math.PI;
    this.yaw += Math.max(-1.8 * dt, Math.min(1.8 * dt, yawError));
    const step = Math.min(0.85 * dt, distance);
    this.position[0] += (dx / distance) * step;
    this.position[1] += (dy / distance) * step;
  }

  step() {
    const dt = 0.1;
    this.iteration++;
    this.#advance(dt);
    this.battery = Math.max(0, this.battery - 0.0008);

    const [px, py] = this.position;
    const elapsed = (Date.now() - this.t0) / 1000;

    // --- LiDAR sweep + accumulated cloud ---------------------------
    const beams = 180;
    const angles = [];
    const distances = [];
    for (let i = 0; i < beams; i++) {
      const bodyAngle = -Math.PI + (2 * Math.PI * i) / beams;
      const distance = raycast(px, py, this.yaw + bodyAngle, 16) + (Math.random() - 0.5) * 0.02;
      if (distance >= 15.9) continue;
      angles.push(Number(bodyAngle.toFixed(4)));
      distances.push(Number(distance.toFixed(3)));
      if (i % 3 === 0) {
        const a = this.yaw + bodyAngle;
        this.cloud.push([
          Number((px + distance * Math.cos(a)).toFixed(2)),
          Number((py + distance * Math.sin(a)).toFixed(2)),
          Number((0.4 + Math.random() * 2.2).toFixed(2)),
          0.8,
        ]);
      }
    }
    if (this.cloud.length > 9000) this.cloud.splice(0, this.cloud.length - 9000);

    // --- BLE tokens: log-distance path loss + wall attenuation ------
    const rssi = {};
    for (const [address, token] of Object.entries(TOKENS)) {
      const distance = Math.max(0.35, Math.hypot(token.x - px, token.y - py));
      const walls = wallsBetween(px, py, token.x, token.y);
      rssi[address] = Math.round(
        -59 - 10 * 2.4 * Math.log10(distance) - 4.5 * walls + (Math.random() - 0.5) * 3,
      );
    }

    // --- UWB ranging -------------------------------------------------
    const ranges = {};
    const los = {};
    for (const [name, [ax, ay, az]] of Object.entries(ANCHORS)) {
      const trueRange = Math.hypot(px - ax, py - ay, 1.4 - az);
      const walls = wallsBetween(px, py, ax, ay);
      ranges[name] = Number((trueRange + 0.15 * walls + (Math.random() - 0.5) * 0.1).toFixed(3));
      los[name] = walls === 0;
    }

    // --- people: mmWave/thermal detections + through-wall vitals -----
    const detected = [];
    const throughWall = [];
    for (const person of this.people) {
      const wobbleX = 0.35 * Math.sin(elapsed * 0.4 + person.phase);
      const wobbleY = 0.35 * Math.cos(elapsed * 0.3 + person.phase);
      const x = person.x + wobbleX;
      const y = person.y + wobbleY;
      const distance = Math.hypot(x - px, y - py);
      if (distance > 11) continue;
      const walls = wallsBetween(px, py, x, y);
      const entry = {
        track_id: person.id,
        x: Number(x.toFixed(2)),
        y: Number(y.toFixed(2)),
        speed: Number((0.2 + 0.3 * Math.abs(Math.sin(elapsed + person.phase))).toFixed(2)),
        confidence: Number(Math.max(0.3, 1 - distance / 14 - walls * 0.15).toFixed(2)),
        behind_wall: walls > 0,
        sources: walls > 0 ? ['uwb'] : ['mmwave', 'thermal'],
      };
      detected.push(entry);
      if (walls > 0 && distance < 9) {
        throughWall.push({
          detection_id: `twd-${person.id}`,
          x: entry.x,
          y: entry.y,
          range: Number(distance.toFixed(2)),
          respiration_bpm: Number((person.respiration * 60).toFixed(1)),
          confidence: entry.confidence,
          behind_wall: true,
        });
      }
    }

    // --- scenario ----------------------------------------------------
    let scenario = null;
    if (this.scenarioActive) {
      this.scenarioTime += dt;
      scenario = this.#scenarioFrame();
    }

    const nearest = throughWall[0];
    return {
      type: 'telemetry',
      timestamp: Date.now() / 1000,
      iteration: this.iteration,
      demo: true,
      pose: { x: Number(px.toFixed(3)), y: Number(py.toFixed(3)), yaw: Number(this.yaw.toFixed(4)) },
      ekf: {
        position: [Number(px.toFixed(3)), Number(py.toFixed(3)), 1.4],
        velocity: [0, 0, 0],
        orientation_euler: [0.02, -0.01, Number(this.yaw.toFixed(4))],
        position_sigma: [0.12, 0.13, 0.2],
        covariance_diagonal: [0.014, 0.017, 0.04, 0.01, 0.01, 0.01, 0.002, 0.002, 0.003],
        converged: true,
      },
      lidar: { angles, distances, count: angles.length },
      map: {
        points: this.cloud.slice(-6000),
        walls: WALLS.map(([x1, y1, x2, y2]) => ({ x1, y1, x2, y2 })),
        stats: { cells_occupied: this.cloud.length, coverage: Math.min(0.6, this.iteration / 3000) },
      },
      ble: {
        rssi,
        beacons: Object.entries(rssi).map(([address, value]) => ({
          address,
          rssi: value,
          name: TOKENS[address].label,
        })),
      },
      uwb: { ranges, los },
      people: detected,
      through_wall: throughWall,
      vitals: nearest
        ? {
            presence: true,
            respiration_bpm: nearest.respiration_bpm,
            heart_bpm: Number((62 + 8 * Math.sin(elapsed / 7)).toFixed(1)),
            confidence: nearest.confidence,
          }
        : { presence: false, respiration_bpm: 0, heart_bpm: 0, confidence: 0 },
      world: { walls: WALLS, exits: EXITS, tokens: TOKENS, anchors: ANCHORS },
      scenario,
      device: {
        battery: Number(this.battery.toFixed(1)),
        temperature: Number((33 + 2 * Math.sin(elapsed / 60)).toFixed(1)),
        loop_hz: 10,
        uptime: Number(elapsed.toFixed(0)),
      },
    };
  }

  #scenarioFrame() {
    const agents = [];
    const count = 24;
    for (let i = 0; i < count; i++) {
      const progress = Math.min(1, (this.scenarioTime * 0.05 + i * 0.01));
      const start = [3 + (i % 6) * 2.4, 10.5 - Math.floor(i / 6) * 1.4];
      const target = [10, 0.5];
      agents.push({
        id: i,
        x: Number((start[0] + (target[0] - start[0]) * progress).toFixed(2)),
        y: Number((start[1] + (target[1] - start[1]) * progress).toFixed(2)),
        escaped: progress >= 1,
        casualty: false,
        group: 'civilian',
      });
    }
    const escaped = agents.filter((a) => a.escaped).length;
    const smokeCells = [];
    const radius = Math.min(6, this.scenarioTime * 0.35);
    for (let a = 0; a < 40; a++) {
      for (let r = 0.5; r < radius; r += 0.8) {
        const angle = (a / 40) * Math.PI * 2;
        smokeCells.push([
          Number((10 + r * Math.cos(angle)).toFixed(2)),
          Number((4 + r * Math.sin(angle)).toFixed(2)),
          Number(Math.max(0.05, 1 - r / radius).toFixed(2)),
        ]);
      }
    }
    return {
      run_id: 'demo-run',
      scenario: 'evacuation',
      status: escaped === agents.length ? 'completed' : 'running',
      sim_time: Number(this.scenarioTime.toFixed(1)),
      duration: 180,
      progress: Math.min(1, this.scenarioTime / 180),
      agents,
      escaped,
      casualties: 0,
      total_agents: agents.length,
      fire_source: [10, 4],
      exits: EXITS,
      smoke: { resolution: 0.5, cells: smokeCells, max_concentration: 0.9 },
      bottlenecks: this.scenarioTime > 20 ? [{ x: 10, y: 1.5, peak: 7, samples: 30 }] : [],
      metrics: {},
    };
  }

  startScenario() {
    this.scenarioActive = true;
    this.scenarioTime = 0;
  }

  stopScenario() {
    this.scenarioActive = false;
  }
}
