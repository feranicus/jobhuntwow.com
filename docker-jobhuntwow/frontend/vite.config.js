import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Optimized for Docker On-Premise Architecture
export default defineConfig({
  plugins: [react()],
  base: '/', 
  build: {
    // Changed from '../dist' to 'dist' so it builds INSIDE /app/dist
    outDir: 'dist', 
    emptyOutDir: true,
  },
  server: {
    // Ensuring the dev server works correctly if you test inside the container
    host: true,
    port: 5173,
  }
});