import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
    plugins: [react()],
    server: {
        port: 5173,
        proxy: {
            '/jobs': 'http://localhost:8000',
            '/results': 'http://localhost:8000',
            '/health': 'http://localhost:8000',
            '/clients': 'http://localhost:8000',
            '/sessions': 'http://localhost:8000',
            '/presets': 'http://localhost:8000',
            '/ws': {
                target: 'ws://localhost:8000',
                ws: true,
            },
        },
    },
    build: {
        outDir: 'dist',
        sourcemap: false,
    },
});
