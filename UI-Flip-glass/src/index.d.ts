// SPDX-License-Identifier: GPL-3.0-only
export declare const flipGlassVersion: string;
export declare function createGlassDisplacementMap(width?: number, height?: number, radius?: number): string;
export interface GlassSurface { update(): void; destroy(): void; }
export declare function attachGlassSurface(element: HTMLElement): GlassSurface;
export declare function enhanceGlass(root?: Document | HTMLElement): GlassSurface[];
export declare function initFlipGlass(root?: Document | HTMLElement): { destroy(): void };
export declare function showToast(message: string, duration?: number): { dismiss(): void };
