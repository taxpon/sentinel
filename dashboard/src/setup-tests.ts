import { configure } from '@testing-library/dom'
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

// Recharts measures text by writing it into a hidden <span id="recharts_measurement_span"> that it
// leaves attached to document.body. It is aria-hidden and positioned off-screen, but Testing
// Library still matches its text, so `getByText` on a value a chart just drew finds two nodes and
// throws "found multiple elements". Ignoring it here — the default is 'script, style' — means every
// panel test queries the chart rather than the measuring tape.
configure({ defaultIgnore: 'script, style, #recharts_measurement_span' })
