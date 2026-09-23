import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

// Regression coverage: a failed dynamic import of the lazily-loaded shiki-block
// chunk (e.g. background update/rebuild with new asset hashes) rejects the
// `React.lazy()` promise. React.Suspense only covers the *pending* state, so
// without a local ErrorBoundary the rejection throws past it to the message-level
// `markdown-render` ErrorBoundary and downgrades the ENTIRE message into
// HugeTextFallback.
vi.mock('./shiki-block', () => ({
  default: () => {
    throw new Error(
      'Failed to fetch dynamically imported module: file:///Applications/Hermes.app/Contents/Resources/app.asar.unpacked/dist/assets/shiki-block-BU-TxQ9Z.js'
    )
  }
}))

import { ErrorBoundary } from '@/components/error-boundary'

import { LazyShiki } from './shiki-highlighter'

afterEach(cleanup)

describe('LazyShiki survives failed lazy chunk fetch', () => {
  it('falls back to PlainShiki instead of bubbling error to markdown-render boundary', () => {
    const code = 'echo "hello world"'
    const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined)

    try {
      const { container } = render(
        <ErrorBoundary fallback={() => <div data-testid="outer-fallback">crashed</div>} label="markdown-render">
          <LazyShiki code={code} language="bash" />
        </ErrorBoundary>
      )

      expect(container.querySelector('[data-testid="outer-fallback"]')).toBeNull()
      expect(container.textContent).toContain('echo "hello world"')
    } finally {
      spy.mockRestore()
    }
  })
})
