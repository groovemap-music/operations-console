// Node's experimental Web Storage globals can shadow jsdom's working stores.
if (typeof globalThis._localStorage !== 'undefined' && globalThis._localStorage instanceof Storage) {
    Object.defineProperty(globalThis, 'localStorage', {
        configurable: true,
        get() {
            return globalThis._localStorage;
        },
    });
}
if (typeof globalThis._sessionStorage !== 'undefined' && globalThis._sessionStorage instanceof Storage) {
    Object.defineProperty(globalThis, 'sessionStorage', {
        configurable: true,
        get() {
            return globalThis._sessionStorage;
        },
    });
}
