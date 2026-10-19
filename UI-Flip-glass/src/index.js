// SPDX-License-Identifier: GPL-3.0-only
// Voice Input Assistant contributors, 2026-10-19: new application adapter.
import { generateControlGlassDisplacementMap } from './mineradio-map.js';
export const flipGlassVersion = '0.2.0';
export const createGlassDisplacementMap = generateControlGlassDisplacementMap;
const surfaces = new WeakMap();
let serial = 0;
const ns = 'http://www.w3.org/2000/svg';
function svgNode(tag, attributes = {}) {
  const node = document.createElementNS(ns, tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  return node;
}
export function attachGlassSurface(element) {
  if (surfaces.has(element)) return surfaces.get(element);
  const id = `via-glass-${++serial}`;
  const svg = svgNode('svg', { width: 0, height: 0, 'aria-hidden': true, focusable: false });
  svg.style.cssText = 'position:absolute;pointer-events:none;overflow:hidden';
  const filter = svgNode('filter', { id, x: '-12%', y: '-28%', width: '124%', height: '156%', 'color-interpolation-filters': 'sRGB' });
  const map = svgNode('feImage', { width: '100%', height: '100%', preserveAspectRatio: 'none', result: 'map' });
  filter.append(map,
    svgNode('feDisplacementMap', { in: 'SourceGraphic', in2: 'map', scale: 90, xChannelSelector: 'R', yChannelSelector: 'B', result: 'displaced' }),
    svgNode('feGaussianBlur', { in: 'displaced', stdDeviation: 0.5, result: 'soft' }),
    svgNode('feComposite', { in: 'soft', in2: 'SourceGraphic', operator: 'over' }));
  svg.append(filter);
  element.append(svg);
  const previous = element.style.backdropFilter;
  let frame = 0;
  function update() {
    const rect = element.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return;
    map.setAttribute('href', createGlassDisplacementMap(rect.width, rect.height, parseFloat(getComputedStyle(element).borderRadius) || 24));
    element.style.backdropFilter = `url(#${id}) saturate(1)`;
  }
  function schedule() {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(update);
  }
  const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule);
  observer?.observe(element);
  const surface = { update, destroy() {
    observer?.disconnect(); cancelAnimationFrame(frame); svg.remove();
    element.style.backdropFilter = previous; surfaces.delete(element);
  } };
  surfaces.set(element, surface);
  schedule();
  return surface;
}
export function enhanceGlass(root = document) {
  if (!globalThis.CSS?.supports('backdrop-filter', 'url(#via-filter)')) return [];
  const selector = '[data-fg-glass]';
  const elements = [...root.querySelectorAll(selector)];
  if (root.matches?.(selector)) elements.unshift(root);
  return elements.map(attachGlassSurface);
}
export function showToast(message, duration = 2600) {
  let host = document.querySelector('[data-via-toasts]');
  if (!host) {
    host = document.createElement('div'); host.className = 'fg-toast-host';
    host.dataset.viaToasts = ''; host.setAttribute('role', 'status');
    host.setAttribute('aria-live', 'polite'); document.body.append(host);
  }
  const item = document.createElement('div'); item.className = 'fg-toast';
  item.textContent = String(message); host.append(item);
  const timer = setTimeout(() => { item.remove(); if (!host.childElementCount) host.remove(); }, Math.max(0, Number(duration) || 2600));
  return { dismiss() { clearTimeout(timer); item.remove(); if (!host.childElementCount) host.remove(); } };
}
export function initFlipGlass(root = document) {
  const active = enhanceGlass(root);
  return { destroy() { active.forEach(surface => surface.destroy()); } };
}
