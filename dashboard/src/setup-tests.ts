import '@testing-library/jest-dom/vitest'

// Recharts sizes itself with ResizeObserver, which jsdom does not implement. Stubbing it here
// means the panel tests in T31-T33 do not each have to discover the same gap.
if (!('ResizeObserver' in globalThis)) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
}
