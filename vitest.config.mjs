import { defineConfig } from 'vitest/config';

export default defineConfig({
    test: {
        environment: 'jsdom',
        include: ['dashboard/__tests__/**/*.test.js'],
        setupFiles: ['./vitest.setup.js'],
    },
});
