// SPDX-License-Identifier: GPL-3.0-only
// New application bootstrap, 2026-10-19.
import './style.css';
import { initFlipGlass } from './index.js';
if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => initFlipGlass(), { once: true });
  else initFlipGlass();
}
export * from './index.js';
