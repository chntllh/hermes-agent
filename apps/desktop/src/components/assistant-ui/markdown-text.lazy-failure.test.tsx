import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

// Mock dynamic chunks throwing during render to simulate network or asar chunk load failures
vi.mock('@/components/chat/shiki-block', () => ({
  default: () => {
    throw new Error('Failed to fetch dynamically imported module: shiki-block-hash.js')
  }
}))

vi.mock('./embeds/mermaid-embed', () => ({
  default: () => {
    throw new Error('Failed to fetch dynamically imported module: mermaid-embed-hash.js')
  }
}))

vi.mock('./embeds/svg-embed', () => ({
  default: () => {
    throw new Error('Failed to fetch dynamically imported module: svg-embed-hash.js')
  }
}))

vi.mock('./embeds/listing-embed', () => ({
  default: () => {
    throw new Error('Failed to fetch dynamically imported module: listing-embed-hash.js')
  }
}))

import { MarkdownTextContent } from './markdown-text'

afterEach(cleanup)

function renderQuietly(node: Parameters<typeof render>[0]) {
  const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined)

  try {
    return render(node)
  } finally {
    spy.mockRestore()
  }
}

function isHugeTextFallback(container: HTMLElement): boolean {
  const root = container.firstElementChild

  return Boolean(root && root.classList.contains('font-mono'))
}

describe('MarkdownTextContent under adversarial dynamic chunk failures', () => {
  it('does NOT trigger HugeTextFallback when shiki chunk fails to load', async () => {
    const text = [
      '# Code Example',
      '',
      'Here is some text explaining the code:',
      '',
      '```typescript',
      'const greeting = "Hello, world!";',
      'console.log(greeting);',
      '```',
      '',
      'And a closing paragraph with **bold** text.'
    ].join('\n')

    const { container } = renderQuietly(<MarkdownTextContent isRunning={false} text={text} />)

    // Settle lazy chunk resolution
    await act(() => new Promise(resolve => setTimeout(resolve, 50)))

    // Must NOT be HugeTextFallback
    expect(isHugeTextFallback(container)).toBe(false)

    // Surrounding rich elements must be present
    expect(await screen.findByRole('heading', { name: 'Code Example' })).toBeTruthy()
    expect(container.textContent).toContain('Here is some text explaining the code:')
    expect(container.textContent).toContain('And a closing paragraph with bold text.')

    // Code content is preserved via PlainShiki fallback
    expect(container.textContent).toContain('const greeting = "Hello, world!";')
  })

  it('does NOT trigger HugeTextFallback when both mermaid and shiki chunks fail simultaneously', async () => {
    const text = [
      '# Architecture Diagram',
      '',
      'System design overview:',
      '',
      '```mermaid',
      'graph TD',
      '  Client --> Gateway',
      '  Gateway --> Backend',
      '```',
      '',
      '```python',
      'def process():',
      '    return True',
      '```',
      '',
      'Follow-up analysis.'
    ].join('\n')

    const { container } = renderQuietly(<MarkdownTextContent isRunning={false} text={text} />)

    await act(() => new Promise(resolve => setTimeout(resolve, 50)))

    // Must NOT degrade the message to HugeTextFallback
    expect(isHugeTextFallback(container)).toBe(false)

    // Heading and prose intact
    expect(await screen.findByRole('heading', { name: 'Architecture Diagram' })).toBeTruthy()
    expect(container.textContent).toContain('System design overview:')
    expect(container.textContent).toContain('Follow-up analysis.')

    // Mermaid code falls back to SyntaxHighlighter, which falls back to PlainShiki
    expect(container.textContent).toContain('Client --> Gateway')
    // Python code falls back to PlainShiki
    expect(container.textContent).toContain('def process():')
  })

  it('does NOT trigger HugeTextFallback when svg and listing chunks fail to load', async () => {
    const text = [
      '# Rich Embeds Test',
      '',
      'SVG rendering:',
      '```svg',
      '<svg><rect width="100" height="100" fill="red"/></svg>',
      '```',
      '',
      'Listing rendering:',
      '```listing',
      '[{ "id": "1", "address": "10 Downing St", "price": "£1,000,000" }]',
      '```',
      '',
      'Closing remarks.'
    ].join('\n')

    const { container } = renderQuietly(<MarkdownTextContent isRunning={false} text={text} />)

    await act(() => new Promise(resolve => setTimeout(resolve, 50)))

    expect(isHugeTextFallback(container)).toBe(false)
    expect(await screen.findByRole('heading', { name: 'Rich Embeds Test' })).toBeTruthy()
    expect(container.textContent).toContain('Closing remarks.')
  })
})
