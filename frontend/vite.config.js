import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Используем '/' для деплоя на кастомный домен jobhuntwow.com
// base: '/jobhuntwow/' использовалось бы, если бы деплой шел на поддиректорию GitHub Pages
export default defineConfig({
  plugins: [react()],
  base: '/', 
  build: {
    outDir: '../dist', // Собираем билд в папку 'dist' в корне репозитория
    emptyOutDir: true,
  },
});