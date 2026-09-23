import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

// Regression audit: dynamic imports of rich embed chunks (`mermaid-embed`,
// `svg-embed`, `listing-embed`) must be caught by `RichBoundary` and degrade
// to their fallback rather than bubbling past Suspense to `markdown-render`
// ErrorBoundary (HugeTextFallback).
//
// Throwing in the mocked component mirrors how React surfaces a rejected lazy chunk.
vi.mock('./mermaid-embed', () => ({
  default: () => {
    throw new Error('Failed to fetch dynamically imported module: mermaid-embed-hash.js')
  }
}))

vi.mock('./svg-embed', () => ({
  default: () => {
    throw new Error('Failed to fetch dynamically imported module: svg-embed-hash.js')
  }
}))

vi.mock('./listing-embed', () => ({
  default: () => {
    throw new Error('Failed to fetch dynamically imported module: listing-embed-hash.js')
  }
}))

import { ErrorBoundary } from '@/components/error-boundary'

import { RichCodeBlock } from './registry'

afterEach(cleanup)

function renderQuietly(node: Parameters<typeof render>[0]) {
  const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined)

  try {
    return render(node)
  } finally {
    spy.mockRestore()
  }
}

describe('RichCodeBlock lazy chunk failure audit', () => {
  it('mermaid chunk load failure degrades to fallback and does not bubble to outer boundary', () => {
    const { container } = renderQuietly(
      <ErrorBoundary
        fallback={() => <div data-testid="outer-markdown-error">HugeTextFallback</div>}
        label="markdown-render"
      >
        <RichCodeBlock
          code="graph TD\nA-->B"
          fallback={<div data-testid="shiki-code-fallback">fallback code block</div>}
          language="mermaid"
        />
      </ErrorBoundary>
    )

    expect(container.querySelector('[data-testid="outer-markdown-error"]')).toBeNull()
    expect(container.querySelector('[data-testid="shiki-code-fallback"]')).not.toBeNull()
    expect(container.textContent).toContain('fallback code block')
  })

  it('svg chunk load failure degrades to fallback and does not bubble to outer boundary', () => {
    const { container } = renderQuietly(
      <ErrorBoundary
        fallback={() => <div data-testid="outer-markdown-error">HugeTextFallback</div>}
        label="markdown-render"
      >
        <RichCodeBlock
          code="<svg><circle cx='10' cy='10' r='5'/></svg>"
          fallback={<div data-testid="shiki-code-fallback">fallback code block</div>}
          language="svg"
        />
      </ErrorBoundary>
    )

    expect(container.querySelector('[data-testid="outer-markdown-error"]')).toBeNull()
    expect(container.querySelector('[data-testid="shiki-code-fallback"]')).not.toBeNull()
    expect(container.textContent).toContain('fallback code block')
  })

  it('listing chunk load failure degrades to fallback and does not bubble to outer boundary', () => {
    const { container } = renderQuietly(
      <ErrorBoundary
        fallback={() => <div data-testid="outer-markdown-error">HugeTextFallback</div>}
        label="markdown-render"
      >
        <RichCodeBlock
          code="[{ id: '1', address: '123 Main St' }]"
          fallback={<div data-testid="shiki-code-fallback">fallback code block</div>}
          language="listing"
        />
      </ErrorBoundary>
    )

    expect(container.querySelector('[data-testid="outer-markdown-error"]')).toBeNull()
    expect(container.querySelector('[data-testid="shiki-code-fallback"]')).not.toBeNull()
    expect(container.textContent).toContain('fallback code block')
  })
})
