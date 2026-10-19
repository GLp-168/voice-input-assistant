import { resolve } from 'node:path';
import { defineConfig } from 'vite';

export default defineConfig({
  base: './',
  build: {
    outDir: resolve(import.meta.dirname, '../src/voice_input/web_dist'),
    emptyOutDir: true,
    sourcemap: false,
    target: 'chrome120',
  },
});
