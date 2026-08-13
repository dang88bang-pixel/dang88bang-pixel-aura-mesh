/**
 * AURA 6.0 visualiser entry point.
 *
 * Wires the Babylon engine, the scene graph, the telemetry socket and the
 * sidebar together. Rendering is decoupled from telemetry: frames arrive at
 * ~10 Hz, the render loop runs at display rate, and the scene graph is only
 * touched when new data actually arrives.
 */

import {
  ArcRotateCamera,
  Color3,
  Color4,
  Engine,
  EngineFactory,
  HemisphericLight,
  DirectionalLight,
  Scene,
  Tools,
  Vector3,
} from '@babylonjs/core';

import { SceneManager } from './SceneManager.js';
import { DataFetcher } from './DataFetcher.js';
import { SimControls } from './SimControls.js';
import { PALETTE, hexToRgb } from './palette.js';

const canvas = document.getElementById('render-canvas');

// Engine selection.
//
// `EngineFactory.CreateAsync` probes `WebGPUEngine.IsSupportedAsync` and falls
// back to WebGL2 on its own. It is worth doing because the two clients differ:
//
//   * Command-post PC (Chrome/Edge on desktop) — WebGPU is available, and its
//     compute-shader sorting is materially faster for large point clouds.
//   * CT45P Map tab — an Android WebView on Android 11. WebGPU shipped for
//     Chrome on Android 12+ and was explicitly NOT enabled for Android
//     WebView, so this device takes the WebGL2 branch and always will.
//     See docs/3d_roadmap_assessment.md §2.
//
// So do not read this as "AURA runs on WebGPU". It is an opportunistic upgrade
// for the desktop client and a no-op on the handheld.
const engineOptions = {
  preserveDrawingBuffer: true,   // required for screenshot export
  stencil: true,
  antialias: true,
  powerPreference: 'high-performance',
};

/**
 * Resolve to the best engine this browser can actually give us.
 *
 * Note the absence of a top-level `await` in this module. Top-level await
 * makes the whole module async at parse time, which needs Chromium 89+;
 * Android 11 shipped WebView 85 and a Non-GMS CT45P has no Play Store to
 * update it through. Parsing would fail outright and the Map tab would show a
 * blank canvas with nothing in the log. Hence an explicit `boot()` instead.
 */
function createEngine() {
  // No `navigator.gpu` at all — the CT45P WebView case. Skip the probe and
  // avoid paying for a promise round-trip on the slowest device we target.
  if (typeof navigator === 'undefined' || !navigator.gpu || !EngineFactory?.CreateAsync) {
    return Promise.resolve(new Engine(canvas, true, engineOptions));
  }
  return EngineFactory.CreateAsync(canvas, engineOptions).catch((error) => {
    // An adapter that advertises itself and then fails to initialise is a real
    // failure mode on flaky drivers. Never let it take the viewport down.
    console.warn('[aura] WebGPU init failed, falling back to WebGL2:', error);
    return new Engine(canvas, true, engineOptions);
  });
}

function start(engine) {
  // Record which backend we actually got, so a field report can state it
  // rather than guess. `WebGPUEngine` does not subclass `Engine`, hence the
  // constructor-name check.
  const backend = engine.constructor?.name === 'WebGPUEngine' ? 'WebGPU' : 'WebGL2';

  const scene = new Scene(engine);
  const background = hexToRgb(PALETTE.background);
  scene.clearColor = new Color4(background.r, background.g, background.b, 1);
  // Cheap depth cue that makes a large point cloud readable
  scene.fogMode = Scene.FOGMODE_EXP2;
  scene.fogDensity = 0.012;
  scene.fogColor = new Color3(background.r, background.g, background.b);

  // --- camera ------------------------------------------------------------
  const camera = new ArcRotateCamera(
    'camera',
    -Math.PI / 2.2,
    Math.PI / 3.2,
    28,
    new Vector3(10, 1.2, 7),
    scene,
  );
  camera.attachControl(canvas, true);
  camera.lowerRadiusLimit = 3;
  camera.upperRadiusLimit = 90;
  // Stop the camera dropping below the floor plane
  camera.upperBetaLimit = Math.PI / 2.05;
  camera.wheelDeltaPercentage = 0.02;
  camera.pinchDeltaPercentage = 0.02;
  camera.panningSensibility = 120;
  camera.useBouncingBehavior = false;

  // --- lighting ----------------------------------------------------------
  const ambient = new HemisphericLight('ambient', new Vector3(0, 1, 0), scene);
  ambient.intensity = 0.75;
  ambient.groundColor = new Color3(0.06, 0.08, 0.12);

  const key = new DirectionalLight('key', new Vector3(-0.4, -1, 0.35), scene);
  key.intensity = 0.55;

  // --- scene graph, telemetry, controls ----------------------------------
  const sceneManager = new SceneManager(scene);

  let followMode = false;
  let latestFrame = null;
  let pendingFrame = null;

  const dataFetcher = new DataFetcher({
    onTelemetry: (frame) => { pendingFrame = frame; },
    onStatus: (status) => {
      const badge = document.getElementById('connection-badge');
      if (!badge) return;
      const map = {
        connected: ['VERBUNDEN', PALETTE.person],
        connecting: ['VERBINDE ...', PALETTE.device],
        reconnecting: ['NEUVERBINDUNG', PALETTE.device],
        disconnected: ['GETRENNT', PALETTE.hazard],
        error: ['FEHLER', PALETTE.hazard],
      };
      const [text, colour] = map[status.state] ?? [null, null];
      if (text) {
        badge.textContent = text;
        badge.style.color = colour;
      }
      if (status.payload?.demoMode) {
        const banner = document.getElementById('demo-banner');
        if (banner) banner.style.display = 'block';
      }
    },
  });

  const controls = new SimControls({
    sceneManager,
    dataFetcher,
    onExport: () => {
      Tools.CreateScreenshotUsingRenderTarget(engine, camera, { width: 1920, height: 1080 }, (data) => {
        const link = document.createElement('a');
        link.href = data;
        link.download = `aura-${new Date().toISOString().replace(/[:.]/g, '-')}.png`;
        link.click();
      });
    },
  });

  controls.onTimelineScrub = (frame) => {
    applyFrame(frame, { record: false });
  };

  document.getElementById('btn-follow')?.addEventListener('click', (event) => {
    followMode = !followMode;
    event.target.textContent = followMode ? 'Frei bewegen' : 'Position folgen';
    event.target.classList.toggle('active', followMode);
  });

  document.getElementById('btn-top')?.addEventListener('click', () => {
    camera.alpha = -Math.PI / 2;
    camera.beta = 0.06;      // not exactly 0: a degenerate up-vector flips the view
    camera.radius = 34;
  });

  document.getElementById('btn-reset-view')?.addEventListener('click', () => {
    camera.alpha = -Math.PI / 2.2;
    camera.beta = Math.PI / 3.2;
    camera.radius = 28;
    camera.target = new Vector3(10, 1.2, 7);
  });

  // --- frame application --------------------------------------------------
  function applyFrame(frame, { record = true } = {}) {
    latestFrame = frame;

    if (frame.map?.walls?.length) sceneManager.updateWalls(frame.map.walls);
    else if (frame.world?.walls?.length) sceneManager.updateWalls(frame.world.walls);

    if (frame.map?.points?.length) sceneManager.updatePointCloud(frame.map.points);

    sceneManager.updateSelf(frame.pose);
    sceneManager.updatePeople(frame.people);
    sceneManager.updateTokens(frame.world?.tokens);
    sceneManager.updateExits(frame.world?.exits ?? frame.scenario?.exits);
    sceneManager.updateScenario(frame.scenario);

    controls.updateReadouts(frame);
    if (record) controls.record(frame);

    if (followMode && frame.pose) {
      // Ease the target instead of snapping; a hard jump at 10 Hz is nauseating.
      camera.target = Vector3.Lerp(camera.target, new Vector3(frame.pose.x, 1.2, frame.pose.y), 0.15);
    }
  }

  // --- render loop --------------------------------------------------------
  let fpsAccumulator = 0;

  // One named function, registered in both places below. This used to be an
  // inline closure duplicated in the visibilitychange handler, and the copy
  // there omitted the `pendingFrame` drain — so after the tab was hidden and
  // shown again the viewport kept rendering but stopped applying telemetry,
  // silently freezing the scene on the last frame before the tab was hidden.
  function renderFrame() {
    if (pendingFrame) {
      applyFrame(pendingFrame);
      pendingFrame = null;
    }
    scene.render();

    if (++fpsAccumulator % 30 === 0) {
      const element = document.getElementById('readout-fps');
      if (element) {
        element.textContent =
          `${engine.getFps().toFixed(0)} FPS · ${dataFetcher.fps.toFixed(1)} Hz · ${backend}`;
      }
    }
  }

  engine.runRenderLoop(renderFrame);

  window.addEventListener('resize', () => engine.resize());

  // Pause rendering when the tab is hidden; a hidden canvas still burns GPU
  // and, on a CT45P in a holster, battery.
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) engine.stopRenderLoop();
    else engine.runRenderLoop(renderFrame);
  });

  dataFetcher.connect();
  controls.setStatus('Warte auf Telemetrie ...');

  // expose for debugging from the console
  window.aura = {
    scene, engine, camera, sceneManager, dataFetcher, controls, backend,
    frame: () => latestFrame,
  };
}

function boot() {
  createEngine().then(start).catch((error) => {
    console.error('[aura] visualiser failed to start:', error);
    const status = document.getElementById('connection-badge');
    if (status) {
      status.textContent = 'RENDERER-FEHLER';
      status.style.color = PALETTE.hazard;
    }
  });
}

boot();
