import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const development = mode === 'development';
  const controlPlane = loadEnv(mode, '.', '').VITE_CONTROL_PLANE_URL?.trim()
    || 'http://127.0.0.1:8844';
  return {
    plugins: [react()],
    resolve: { preserveSymlinks: true },
    ...(development ? {
      server: {
        port: 5180,
        strictPort: true,
        proxy: { '/v1': { target: controlPlane, changeOrigin: true } },
      },
    } : {}),
    build: { target: 'es2022', sourcemap: true, manifest: true },
  };
});
