import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'

export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  // Only frontend/.env would be read; server credentials at project root are never loaded.
  envDir: fileURLToPath(new URL('.', import.meta.url)),
  plugins: [react()],
  server: { host: '127.0.0.1', port: 5173, proxy: { '/api': 'http://127.0.0.1:8091', '/health': 'http://127.0.0.1:8091' } },
  build: { outDir: 'dist', emptyOutDir: true },
})
