/**
 * Babylon.js scene graph for the AURA 6.0 visualiser.
 *
 * Performance notes that drove the design:
 *   * The point cloud is a single `PointsCloudSystem`, not thousands of
 *     meshes. Six thousand individual spheres would collapse to single-digit
 *     FPS on a tablet; one cloud stays at 60.
 *   * People, tokens and smoke use **thin instances** on one source mesh, so
 *     each category costs a single draw call regardless of count.
 *   * Buffers are reallocated only when the required capacity grows, which
 *     keeps a 10 Hz telemetry stream from thrashing the GC.
 */

import {
  Color3,
  Color4,
  DynamicTexture,
  Mesh,
  MeshBuilder,
  PointsCloudSystem,
  StandardMaterial,
  Matrix,
  Quaternion,
  Vector3,
} from '@babylonjs/core';

import { PALETTE, hexToRgb } from './palette.js';

const WALL_HEIGHT = 2.7;

export class SceneManager {
  constructor(scene) {
    this.scene = scene;
    this.layers = {
      pointCloud: true,
      walls: true,
      people: true,
      tokens: true,
      smoke: true,
      throughWall: true,
      trail: true,
      agents: true,
    };

    this.pointCloud = null;
    this.pointCloudMesh = null;
    this.pointCapacity = 0;
    this.wallMeshes = [];
    this.trailPoints = [];
    this.trailMesh = null;

    this.#createGround();
    this.#createSelfMarker();
    this.#createInstanceSources();
  }

  // ------------------------------------------------------------------
  // static scene furniture
  // ------------------------------------------------------------------
  #createGround() {
    const ground = MeshBuilder.CreateGround('ground', { width: 60, height: 60 }, this.scene);
    const material = new StandardMaterial('groundMaterial', this.scene);
    const rgb = hexToRgb(PALETTE.surface);
    material.diffuseColor = new Color3(rgb.r, rgb.g, rgb.b);
    material.specularColor = Color3.Black();
    material.alpha = 0.85;
    ground.material = material;
    ground.position.set(10, -0.01, 7);
    ground.receiveShadows = true;
    this.ground = ground;
  }

  #createSelfMarker() {
    const marker = MeshBuilder.CreateSphere('self', { diameter: 0.42, segments: 16 }, this.scene);
    const material = new StandardMaterial('selfMaterial', this.scene);
    const rgb = hexToRgb(PALETTE.self);
    material.emissiveColor = new Color3(rgb.r, rgb.g, rgb.b);
    material.diffuseColor = new Color3(rgb.r * 0.4, rgb.g * 0.4, rgb.b * 0.4);
    marker.material = material;
    marker.position.set(0, 1.4, 0);

    // heading cone so the operator's facing is readable at a glance
    const cone = MeshBuilder.CreateCylinder(
      'selfHeading',
      { height: 0.5, diameterTop: 0, diameterBottom: 0.26 },
      this.scene,
    );
    cone.material = material;
    cone.parent = marker;
    cone.rotation.z = -Math.PI / 2;
    cone.position.x = 0.4;

    this.selfMarker = marker;
    this.selfHeading = cone;
  }

  /**
   * One source mesh per category; every entity is a thin instance of it.
   * `thinInstanceSetBuffer` uploads all transforms in a single GPU call.
   */
  #createInstanceSources() {
    const makeSource = (name, mesh, hex, alpha = 1) => {
      const material = new StandardMaterial(`${name}Material`, this.scene);
      const rgb = hexToRgb(hex);
      material.emissiveColor = new Color3(rgb.r * 0.85, rgb.g * 0.85, rgb.b * 0.85);
      material.diffuseColor = new Color3(rgb.r, rgb.g, rgb.b);
      material.specularColor = Color3.Black();
      if (alpha < 1) {
        material.alpha = alpha;
        material.needDepthPrePass = true;
      }
      mesh.material = material;
      mesh.isVisible = false;
      mesh.alwaysSelectAsActiveMesh = true;
      return mesh;
    };

    this.personSource = makeSource(
      'person',
      MeshBuilder.CreateCapsule('personSource', { height: 1.75, radius: 0.26 }, this.scene),
      PALETTE.person,
    );
    this.tokenSource = makeSource(
      'token',
      MeshBuilder.CreateSphere('tokenSource', { diameter: 0.34, segments: 10 }, this.scene),
      PALETTE.device,
    );
    this.hazardSource = makeSource(
      'hazard',
      MeshBuilder.CreateTorus('hazardSource', { diameter: 1.3, thickness: 0.09, tessellation: 20 }, this.scene),
      PALETTE.hazard,
      0.85,
    );
    this.smokeSource = makeSource(
      'smoke',
      MeshBuilder.CreateSphere('smokeSource', { diameter: 0.62, segments: 6 }, this.scene),
      PALETTE.smoke,
      0.14,
    );
    this.agentSource = makeSource(
      'agent',
      MeshBuilder.CreateCylinder('agentSource', { height: 1.7, diameter: 0.42 }, this.scene),
      PALETTE.exit,
    );
    this.exitSource = makeSource(
      'exit',
      MeshBuilder.CreateBox('exitSource', { width: 1.2, height: 2.1, depth: 0.16 }, this.scene),
      PALETTE.exit,
      0.6,
    );
  }

  // ------------------------------------------------------------------
  // thin-instance helper
  // ------------------------------------------------------------------
  #setInstances(mesh, transforms) {
    if (!mesh) return;
    if (transforms.length === 0) {
      mesh.thinInstanceCount = 0;
      mesh.isVisible = false;
      return;
    }
    const required = transforms.length * 16;
    // Grow the buffer in steps and keep it; reallocating every frame at 10 Hz
    // produces noticeable GC pauses in long sessions.
    if (!mesh._auraBuffer || mesh._auraBuffer.length < required) {
      mesh._auraBuffer = new Float32Array(Math.ceil(required * 1.5));
      mesh._auraDirty = true;
    }
    const buffer = mesh._auraBuffer;
    transforms.forEach((transform, index) => {
      transform.copyToArray(buffer, index * 16);
    });
    if (mesh._auraDirty) {
      mesh.thinInstanceSetBuffer('matrix', buffer, 16, false);
      mesh._auraDirty = false;
    } else {
      mesh.thinInstanceBufferUpdated('matrix');
    }
    mesh.thinInstanceCount = transforms.length;
    mesh.isVisible = true;
  }

  // ------------------------------------------------------------------
  // point cloud
  // ------------------------------------------------------------------
  /** @param {Array<[number,number,number,number]>} points [x, y, z, intensity] */
  async updatePointCloud(points) {
    if (!this.layers.pointCloud || !points || points.length === 0) return;

    // Rebuild only when the capacity is exceeded; otherwise write in place.
    if (!this.pointCloud || points.length > this.pointCapacity) {
      if (this.pointCloudMesh) {
        this.pointCloudMesh.dispose();
        this.pointCloudMesh = null;
      }
      this.pointCapacity = Math.max(2048, Math.ceil(points.length * 1.4));
      const system = new PointsCloudSystem('cloud', 2, this.scene, { updatable: true });
      const structure = hexToRgb(PALETTE.structure);
      system.addPoints(this.pointCapacity, (particle, index) => {
        const point = points[index % points.length];
        // Agent frame is z-up; Babylon is y-up, so y and z swap here.
        particle.position = new Vector3(point[0], point[2], point[1]);
        const intensity = 0.5 + 0.5 * (point[3] ?? 0.8);
        particle.color = new Color4(
          structure.r * intensity,
          structure.g * intensity,
          structure.b * intensity,
          1,
        );
      });
      this.pointCloudMesh = await system.buildMeshAsync();
      this.pointCloud = system;
      this.pointCloudMesh.alwaysSelectAsActiveMesh = true;
    }

    const structure = hexToRgb(PALETTE.structure);
    this.pointCloud.updateParticle = (particle) => {
      const point = points[particle.idx];
      if (!point) {
        // Park unused particles far below the floor instead of resizing.
        particle.position.set(0, -1000, 0);
        return particle;
      }
      particle.position.set(point[0], point[2], point[1]);
      const intensity = 0.5 + 0.5 * (point[3] ?? 0.8);
      particle.color.set(structure.r * intensity, structure.g * intensity, structure.b * intensity, 1);
      return particle;
    };
    this.pointCloud.setParticles();
    this.pointCloudMesh.setEnabled(this.layers.pointCloud);
  }

  // ------------------------------------------------------------------
  // walls
  // ------------------------------------------------------------------
  updateWalls(walls) {
    if (!walls) return;
    const signature = JSON.stringify(walls.length) + ':' + (walls[0] ? JSON.stringify(walls[0]) : '');
    if (signature === this.wallSignature) {
      this.wallMeshes.forEach((mesh) => mesh.setEnabled(this.layers.walls));
      return;
    }
    this.wallSignature = signature;
    this.wallMeshes.forEach((mesh) => mesh.dispose());
    this.wallMeshes = [];

    const material = new StandardMaterial('wallMaterial', this.scene);
    const rgb = hexToRgb(PALETTE.structure);
    material.diffuseColor = new Color3(rgb.r * 0.55, rgb.g * 0.55, rgb.b * 0.6);
    material.emissiveColor = new Color3(rgb.r * 0.12, rgb.g * 0.12, rgb.b * 0.16);
    material.specularColor = Color3.Black();
    material.alpha = 0.72;
    material.backFaceCulling = false;

    walls.forEach((wall, index) => {
      const [x1, y1, x2, y2] = Array.isArray(wall)
        ? wall
        : [wall.x1, wall.y1, wall.x2, wall.y2];
      const length = Math.hypot(x2 - x1, y2 - y1);
      if (length < 1e-3) return;
      const box = MeshBuilder.CreateBox(
        `wall-${index}`,
        { width: length, height: WALL_HEIGHT, depth: 0.12 },
        this.scene,
      );
      box.material = material;
      box.position.set((x1 + x2) / 2, WALL_HEIGHT / 2, (y1 + y2) / 2);
      box.rotation.y = -Math.atan2(y2 - y1, x2 - x1);
      box.setEnabled(this.layers.walls);
      this.wallMeshes.push(box);
    });
  }

  // ------------------------------------------------------------------
  // dynamic entities
  // ------------------------------------------------------------------
  updateSelf(pose) {
    if (!pose) return;
    this.selfMarker.position.set(pose.x, 1.4, pose.y);
    this.selfMarker.rotation.y = -(pose.yaw ?? 0);

    if (this.layers.trail) {
      const last = this.trailPoints[this.trailPoints.length - 1];
      if (!last || Math.hypot(last.x - pose.x, last.z - pose.y) > 0.25) {
        this.trailPoints.push(new Vector3(pose.x, 0.05, pose.y));
        if (this.trailPoints.length > 600) this.trailPoints.shift();
        this.#redrawTrail();
      }
    }
  }

  #redrawTrail() {
    if (this.trailPoints.length < 2) return;
    const rgb = hexToRgb(PALETTE.trail);
    this.trailMesh = MeshBuilder.CreateLines(
      'trail',
      { points: this.trailPoints, instance: this.trailMesh, updatable: true },
      this.scene,
    );
    this.trailMesh.color = new Color3(rgb.r, rgb.g, rgb.b);
    this.trailMesh.setEnabled(this.layers.trail);
  }

  updatePeople(people) {
    const visible = this.layers.people ? people ?? [] : [];
    const upright = [];
    const halos = [];
    visible.forEach((person) => {
      upright.push(Matrix.Translation(person.x, 0.88, person.y));
      if (person.behind_wall && this.layers.throughWall) {
        halos.push(
          Matrix.Compose(
            new Vector3(1, 1, 1),
            Quaternion.RotationAxis(new Vector3(1, 0, 0), Math.PI / 2),
            new Vector3(person.x, 0.12, person.y),
          ),
        );
      }
    });
    this.#setInstances(this.personSource, upright);
    this.#setInstances(this.hazardSource, halos);
  }

  updateTokens(tokens) {
    const transforms = [];
    if (this.layers.tokens && tokens) {
      Object.values(tokens).forEach((token) => {
        transforms.push(Matrix.Translation(token.x, 1.9, token.y));
      });
    }
    this.#setInstances(this.tokenSource, transforms);
  }

  updateExits(exits) {
    const transforms = (exits ?? []).map((exit) => Matrix.Translation(exit.x, 1.05, exit.y));
    this.#setInstances(this.exitSource, transforms);
  }

  updateScenario(scenario) {
    if (!scenario) {
      this.#setInstances(this.agentSource, []);
      this.#setInstances(this.smokeSource, []);
      return;
    }
    const agents = this.layers.agents
      ? (scenario.agents ?? []).filter((a) => !a.escaped)
      : [];
    this.#setInstances(
      this.agentSource,
      agents.map((agent) => Matrix.Translation(agent.x, 0.85, agent.y)),
    );

    const cells = this.layers.smoke ? scenario.smoke?.cells ?? [] : [];
    // Only render visible smoke; the field has a long, invisible tail.
    this.#setInstances(
      this.smokeSource,
      cells
        .filter((cell) => cell[2] > 0.08)
        .slice(0, 900)
        .map((cell) => {
          const scale = 0.7 + cell[2] * 1.6;
          return Matrix.Compose(
            new Vector3(scale, scale, scale),
            Quaternion.Identity(),
            new Vector3(cell[0], 0.9 + cell[2], cell[1]),
          );
        }),
    );
  }

  setLayer(name, enabled) {
    if (!(name in this.layers)) return;
    this.layers[name] = enabled;
    if (name === 'walls') this.wallMeshes.forEach((mesh) => mesh.setEnabled(enabled));
    if (name === 'pointCloud' && this.pointCloudMesh) this.pointCloudMesh.setEnabled(enabled);
    if (name === 'trail' && this.trailMesh) this.trailMesh.setEnabled(enabled);
  }

  clearTrail() {
    this.trailPoints = [];
    if (this.trailMesh) {
      this.trailMesh.dispose();
      this.trailMesh = null;
    }
  }

  dispose() {
    this.wallMeshes.forEach((mesh) => mesh.dispose());
    this.pointCloudMesh?.dispose();
    this.trailMesh?.dispose();
  }
}
