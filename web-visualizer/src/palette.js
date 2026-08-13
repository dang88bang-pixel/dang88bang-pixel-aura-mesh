/**
 * Semantic colour code for the whole AURA platform.
 *
 * Kept identical in three places on purpose — an operator on a CT45P, an
 * incident commander on a laptop and an analyst reading an exported report
 * must all see "green = living being" without a legend:
 *   - android-app/app/src/main/res/values/colors.xml
 *   - edge-agent/aura/voxel.py  (LABEL_* constants)
 *   - this file
 */

export const LABEL = {
  EMPTY: 0,
  STRUCTURE: 1,
  PERSON: 2,
  DEVICE: 3,
  HAZARD: 4,
  EXIT: 5,
};

export const PALETTE = {
  structure: '#6A7A8A',
  person: '#00FF88',
  device: '#FFCC00',
  hazard: '#FF4444',
  exit: '#FF8800',

  background: '#0A0D14',
  surface: '#141A24',
  grid: '#1E2A38',
  primary: '#2E7DD1',
  text: '#E6ECF2',
  textDim: '#8A96A6',

  self: '#2E7DD1',
  throughWall: '#FF4444',
  smoke: '#C8C8D2',
  trail: '#2E7DD1',
};

export const LABEL_COLOR = {
  [LABEL.EMPTY]: PALETTE.background,
  [LABEL.STRUCTURE]: PALETTE.structure,
  [LABEL.PERSON]: PALETTE.person,
  [LABEL.DEVICE]: PALETTE.device,
  [LABEL.HAZARD]: PALETTE.hazard,
  [LABEL.EXIT]: PALETTE.exit,
};

export const LABEL_NAME = {
  [LABEL.EMPTY]: 'Leer',
  [LABEL.STRUCTURE]: 'Struktur',
  [LABEL.PERSON]: 'Person',
  [LABEL.DEVICE]: 'Gerät',
  [LABEL.HAZARD]: 'Gefahr',
  [LABEL.EXIT]: 'Ausgang',
};

/** '#RRGGBB' -> { r, g, b } in 0..1, the range Babylon's Color3 expects. */
export function hexToRgb(hex) {
  const value = hex.replace('#', '');
  return {
    r: parseInt(value.slice(0, 2), 16) / 255,
    g: parseInt(value.slice(2, 4), 16) / 255,
    b: parseInt(value.slice(4, 6), 16) / 255,
  };
}
