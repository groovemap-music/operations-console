import { beforeEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));

const source = readFileSync(resolve(here, '../static/admin.js'), 'utf8')
    .replace(/document\.addEventListener\(\s*['"]DOMContentLoaded['"][\s\S]*$/, '');
const html = readFileSync(resolve(here, '../static/admin.html'), 'utf8');

describe('Valkey storage panel', () => {
    let app;
    beforeEach(() => {
        document.body.innerHTML = html;
        vm.runInThisContext(`(function() { ${source}; globalThis.__StorageAdmin = AdminDashboard; })();`);
        app = Object.create(globalThis.__StorageAdmin.prototype);
    });

    it.each(['valkey', 'redis'])('renders the %s storage payload in the Valkey panel', async (key) => {
        app.authFetch = vi.fn().mockResolvedValue({
            ok: true,
            json: async () => ({ [key]: {
                status: 'ok', memory_used: '12 MB', memory_peak: '18 MB',
                total_keys: 1200, keys_by_prefix: { 'revoked:jti:': 4 },
            } }),
        });
        await app.fetchStorage();
        expect(app.authFetch).toHaveBeenCalledWith('/admin/api/storage');
        expect(document.querySelector('[data-target="storage-valkey-body"]').textContent).toContain('Valkey');
        expect(document.getElementById('valkey-status-badge').textContent).toBe('ok');
        expect(document.getElementById('valkey-mem-used').textContent).toBe('12 MB');
        expect(document.getElementById('valkey-mem-peak').textContent).toBe('18 MB');
        expect(document.getElementById('valkey-total-keys').textContent).toBe(Number(1200).toLocaleString());
        expect(document.getElementById('valkey-keys-body').textContent).toContain('revoked:jti:');
        expect(document.getElementById('valkey-keys-body').textContent).toContain('4');
        expect(document.querySelector('[id*="redis"]')).toBeNull();
        expect(document.getElementById('storage-loading').style.display).toBe('none');
    });

    it('prefers Valkey when both payload keys exist', async () => {
        app.authFetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({
            valkey: { status: 'error' }, redis: { status: 'ok', memory_used: 'old data' },
        }) });
        await app.fetchStorage();
        expect(document.getElementById('valkey-status-badge').textContent).toBe('error');
        expect(document.getElementById('valkey-mem-used').textContent).toBe('—');
    });
});
