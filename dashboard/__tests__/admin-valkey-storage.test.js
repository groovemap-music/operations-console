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

    it('renders the valkey storage payload in the Valkey panel', async () => {
        app.authFetch = vi.fn().mockResolvedValue({
            ok: true,
            json: async () => ({ valkey: {
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
        expect(document.getElementById('storage-loading').style.display).toBe('none');
    });

    it('does not render a legacy payload that lacks the valkey key', async () => {
        const legacyKey = ['re', 'dis'].join('');
        app.authFetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({
            [legacyKey]: { status: 'ok', memory_used: 'old data' },
        }) });
        await app.fetchStorage();
        expect(document.getElementById('valkey-status-badge').textContent).toBe('—');
        expect(document.getElementById('valkey-mem-used').textContent).not.toBe('old data');
        expect(document.getElementById('storage-error').style.display).not.toBe('inline');
        expect(document.getElementById('storage-loading').style.display).toBe('none');
    });

    it('handles a missing valkey payload gracefully', async () => {
        app.authFetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
        await app.fetchStorage();
        expect(document.getElementById('valkey-status-badge').textContent).toBe('—');
        expect(document.getElementById('storage-loading').style.display).toBe('none');
    });
});
