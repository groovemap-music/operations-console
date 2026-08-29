import { defineConfig } from 'vitest/config';

export default defineConfig({
    test: {
        environment: 'jsdom',
        include: ['dashboard/__tests__/**/*.test.js'],
        setupFiles: ['./vitest.setup.js'],
        coverage: {
            provider: 'v8',
            include: ['dashboard/static/**/*.js'],
            reporter: ['text', 'json', 'lcov'],
            reportsDirectory: 'coverage',
        },
    },
});
