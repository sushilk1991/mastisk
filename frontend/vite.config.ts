import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { VitePWA } from 'vite-plugin-pwa';
import { resolve } from 'node:path';

// Build output goes into the Python package so the wheel ships it
const outDir = resolve(__dirname, '..', 'src', 'mastisk', 'pwa');

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['favicon.svg'],
      devOptions: { enabled: false },
      manifest: {
        name: 'Mastisk',
        short_name: 'Mastisk',
        description: 'Personal knowledge wiki with 24/7 research agents',
        theme_color: '#1a1a1a',
        background_color: '#fafafa',
        display: 'standalone',
        orientation: 'portrait',
        icons: [
          { src: 'icons/icon-192.png', sizes: '192x192', type: 'image/png' },
          { src: 'icons/icon-512.png', sizes: '512x512', type: 'image/png' },
          { src: 'icons/icon-maskable.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
        ],
      },
      workbox: {
        skipWaiting: true,
        clientsClaim: true,
        cleanupOutdatedCaches: true,
        runtimeCaching: [
          {
            urlPattern: /^\/api\/articles\/.*$/,
            handler: 'StaleWhileRevalidate',
            options: { cacheName: 'articles' },
          },
          {
            urlPattern: /^\/api\/(sidebar|digest|feed)$/,
            handler: 'NetworkFirst',
            options: { cacheName: 'views', networkTimeoutSeconds: 2 },
          },
          {
            urlPattern: /^\/api\/graph\/compact$/,
            handler: 'StaleWhileRevalidate',
            options: { cacheName: 'graph' },
          },
          {
            urlPattern: /^\/api\/ask$/,
            handler: 'NetworkOnly',
          },
        ],
      },
    }),
  ],
  build: {
    outDir,
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:5555', changeOrigin: true },
    },
  },
});
