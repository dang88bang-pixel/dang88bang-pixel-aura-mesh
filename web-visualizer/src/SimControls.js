import { PALETTE, QUALITY_BADGES, qualityForSigma } from './palette.js';

/**
 * Sidebar wiring: layer toggles, scenario controls, live readouts and export.
 * Kept free of Babylon imports so the DOM layer can be reasoned about (and
 * unit-tested) without a WebGL context.
 */
export class SimControls {
  constructor({ sceneManager, dataFetcher, onExport }) {
    this.sceneManager = sceneManager;
    this.dataFetcher = dataFetcher;
    this.onExport = onExport ?? (() => {});
    this.paused = false;
    this.history = [];
    this.maxHistory = 600;
    this.#bind();
  }

  #bind() {
    document.querySelectorAll('[data-layer]').forEach((input) => {
      input.addEventListener('change', () => {
        this.sceneManager.setLayer(input.dataset.layer, input.checked);
      });
    });

    const bindSlider = (id, outputId, format = (v) => v) => {
      const slider = document.getElementById(id);
      const output = document.getElementById(outputId);
      if (!slider || !output) return;
      const sync = () => { output.textContent = format(slider.value); };
      slider.addEventListener('input', sync);
      sync();
    };
    bindSlider('people-slider', 'people-value');
    bindSlider('smoke-slider', 'smoke-value', (v) => Number(v).toFixed(2));
    bindSlider('panic-slider', 'panic-value', (v) => Number(v).toFixed(2));
    bindSlider('speed-slider', 'speed-value', (v) => `${Number(v).toFixed(1)}x`);

    document.getElementById('btn-start')?.addEventListener('click', () => this.startScenario());
    document.getElementById('btn-stop')?.addEventListener('click', () => this.stopScenario());
    document.getElementById('btn-pause')?.addEventListener('click', (event) => {
      this.paused = !this.paused;
      event.target.textContent = this.paused ? 'Weiter' : 'Pause';
      this.dataFetcher.send('scenario.pause', { paused: this.paused });
    });
    document.getElementById('btn-clear-trail')?.addEventListener('click', () => {
      this.sceneManager.clearTrail();
    });
    document.getElementById('btn-export-gltf')?.addEventListener('click', () => {
      window.open('/api/v1/agent/export/gltf', '_blank');
    });
    document.getElementById('btn-export-json')?.addEventListener('click', () => {
      window.open('/api/v1/agent/export/json', '_blank');
    });
    document.getElementById('btn-screenshot')?.addEventListener('click', () => this.onExport());

    const timeline = document.getElementById('timeline');
    timeline?.addEventListener('input', () => {
      const index = Number(timeline.value);
      const frame = this.history[index];
      if (frame) this.onTimelineScrub?.(frame);
    });
  }

  startScenario() {
    const payload = {
      scenario: document.getElementById('scenario-select')?.value ?? 'evacuation',
      people: Number(document.getElementById('people-slider')?.value ?? 24),
      smoke_density: Number(document.getElementById('smoke-slider')?.value ?? 0.35),
      panic: Number(document.getElementById('panic-slider')?.value ?? 0.3),
      speed: Number(document.getElementById('speed-slider')?.value ?? 1),
      duration: 180,
    };
    if (!this.dataFetcher.send('scenario.start', payload)) {
      this.setStatus('Kein Agent verbunden - Demo-Szenario läuft weiter.');
    }
  }

  stopScenario() {
    this.dataFetcher.send('scenario.stop', {});
  }

  record(frame) {
    this.history.push(frame);
    if (this.history.length > this.maxHistory) this.history.shift();
    const timeline = document.getElementById('timeline');
    if (timeline) {
      timeline.max = String(Math.max(0, this.history.length - 1));
      if (!timeline.dataset.scrubbing) timeline.value = timeline.max;
    }
  }

  setStatus(text) {
    const element = document.getElementById('status-text');
    if (element) element.textContent = text;
  }

  /** Right-hand readouts: EKF, sensors, vitals, device health. */
  updateReadouts(frame) {
    const set = (id, value) => {
      const element = document.getElementById(id);
      if (element) element.textContent = value;
    };

    const ekf = frame.ekf ?? {};
    const sigma = ekf.position_sigma ?? [0, 0, 0];
    set('readout-position',
      `${(ekf.position?.[0] ?? 0).toFixed(2)}, ${(ekf.position?.[1] ?? 0).toFixed(2)}, ${(ekf.position?.[2] ?? 0).toFixed(2)} m`);
    set('readout-sigma', `±${Math.max(...sigma).toFixed(3)} m`);
    set('readout-yaw', `${(((frame.pose?.yaw ?? 0) * 180) / Math.PI).toFixed(1)}°`);

    const fixBadge = document.getElementById('readout-fix');
    if (fixBadge) {
      // Graded, not binary. A boolean cannot distinguish a 0.8 m estimate
      // from one that has drifted kilometres - both are "not converged", and
      // both used to render identically. See docs/open_issues_research.md.
      const sigma = Math.max(...(ekf.position_sigma ?? [Infinity]));
      const quality = ekf.quality ?? qualityForSigma(sigma);
      const age = ekf.seconds_since_aiding;
      const badge = QUALITY_BADGES[quality] ?? QUALITY_BADGES.lost;
      fixBadge.textContent = Number.isFinite(age) && age > 5
        ? `${badge.label} · ${age.toFixed(0)} s ohne Stützung`
        : badge.label;
      fixBadge.style.color = badge.colour;
      fixBadge.title = `1-sigma ${Number.isFinite(sigma) ? sigma.toFixed(2) : '?'} m`;
    }

    set('readout-points', String(frame.map?.stats?.cells_occupied ?? frame.map?.points?.length ?? 0));
    set('readout-people', String(frame.people?.length ?? 0));
    set('readout-throughwall', String(frame.through_wall?.length ?? 0));

    const vitals = frame.vitals ?? {};
    set('readout-respiration', vitals.presence ? `${vitals.respiration_bpm.toFixed(0)}/min` : '--');
    set('readout-heart', vitals.presence && vitals.heart_bpm ? `${vitals.heart_bpm.toFixed(0)} bpm` : '--');

    const device = frame.device ?? {};
    set('readout-battery', device.battery != null ? `${device.battery.toFixed(0)}%` : '--');
    set('readout-temperature', device.temperature != null ? `${device.temperature.toFixed(1)} °C` : '--');
    set('readout-rate', device.loop_hz != null ? `${device.loop_hz} Hz` : '--');

    const scenario = frame.scenario;
    const progress = document.getElementById('scenario-progress');
    if (progress) progress.value = scenario ? (scenario.progress ?? 0) * 100 : 0;
    set('readout-scenario', scenario
      ? `${scenario.scenario} · ${scenario.escaped}/${scenario.total_agents} raus · t=${scenario.sim_time}s`
      : 'kein Szenario aktiv');

    const bottleneck = scenario?.bottlenecks?.[0];
    set('readout-bottleneck', bottleneck ? `(${bottleneck.x}, ${bottleneck.y}) · ${bottleneck.peak} Pers.` : '--');

    // RSSI bars
    const rssiContainer = document.getElementById('rssi-list');
    if (rssiContainer && frame.ble?.beacons) {
      rssiContainer.innerHTML = frame.ble.beacons.map((beacon) => {
        const fraction = Math.max(0, Math.min(1, (beacon.rssi + 100) / 60));
        const colour = fraction > 0.66 ? PALETTE.person : fraction > 0.33 ? PALETTE.device : PALETTE.hazard;
        return `<div class="rssi-row">
            <span class="rssi-label">${beacon.name || beacon.address}</span>
            <span class="rssi-value">${beacon.rssi} dBm</span>
            <div class="rssi-track"><div class="rssi-fill" style="width:${fraction * 100}%;background:${colour}"></div></div>
          </div>`;
      }).join('');
    }

    // Covariance sparkline
    const canvas = document.getElementById('covariance-chart');
    if (canvas && ekf.covariance_diagonal) {
      this.#drawCovariance(canvas, ekf.covariance_diagonal.slice(0, 9));
    }
  }

  #drawCovariance(canvas, values) {
    const context = canvas.getContext('2d');
    if (!context) return;
    const { width, height } = canvas;
    context.clearRect(0, 0, width, height);
    context.fillStyle = PALETTE.surface;
    context.fillRect(0, 0, width, height);

    const max = Math.max(...values, 1e-6);
    const barWidth = width / values.length;
    values.forEach((value, index) => {
      const barHeight = (value / max) * (height - 6);
      context.fillStyle = index < 3 ? PALETTE.primary : index < 6 ? PALETTE.device : PALETTE.textDim;
      context.fillRect(index * barWidth + 1, height - barHeight, barWidth - 2, barHeight);
    });
  }
}
