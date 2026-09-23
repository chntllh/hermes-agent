import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

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

/**
 * Checks if the rendered container is HugeTextFallback.
 * HugeTextFallback renders with class 'font-mono' and does NOT have 'prose'.
 * Normal markdown renders with class 'prose' and does NOT have 'font-mono'.
 */
function isHugeTextFallback(container: HTMLElement): boolean {
  const root = container.firstElementChild

  return Boolean(root && root.classList.contains('font-mono'))
}

describe('MarkdownTextContent stress-testing & HugeTextFallback regression audit', () => {
  it('renders 120,000 chars (100K+) as rich markdown without triggering HugeTextFallback', async () => {
    // Generate ~120KB of structured markdown (headings, paragraphs, lists, code)
    const paragraphs: string[] = [
      '# Section Header\n\nThis is a stress test paragraph with **bold** and *italic* text.'
    ]

    const block = 'This is repetitive text that simulates a large, multi-paragraph document with varied formatting.\n\n'

    // 1,200 paragraphs of ~100 chars each ≈ 120,000 chars
    for (let i = 0; i < 1100; i++) {
      paragraphs.push(`### Subheading ${i}\n\n${block}- Item A\n- Item B\n\n`)
    }

    const fullText = paragraphs.join('\n')
    expect(fullText.length).toBeGreaterThan(100_000)
    expect(fullText.length).toBeLessThan(200_000)

    const { container } = renderQuietly(<MarkdownTextContent isRunning={false} text={fullText} />)

    expect(isHugeTextFallback(container)).toBe(false)
    // Rich elements exist
    expect(await screen.findByRole('heading', { name: 'Section Header' })).toBeTruthy()
    expect(container.querySelector('h3')).not.toBeNull()
    expect(container.querySelector('ul')).not.toBeNull()
  })

  it('strictly respects the MAX_MARKDOWN_CHARS (200,000) boundary', () => {
    // Exactly 200,000 characters: does not hit HugeTextFallback early
    const text200k = '# Heading\n\n' + 'a'.repeat(200_000 - 11)
    expect(text200k.length).toBe(200_000)

    const { container: container200k } = renderQuietly(<MarkdownTextContent isRunning={false} text={text200k} />)
    expect(isHugeTextFallback(container200k)).toBe(false)

    // 200,001 characters: triggers HugeTextFallback
    cleanup()
    const textOverLimit = '# Heading\n\n' + 'a'.repeat(200_001 - 11)
    expect(textOverLimit.length).toBe(200_001)

    const { container: containerOverLimit } = renderQuietly(
      <MarkdownTextContent isRunning={false} text={textOverLimit} />
    )

    expect(isHugeTextFallback(containerOverLimit)).toBe(true)
  })

  it('renders nested code fences without crashing or triggering HugeTextFallback', async () => {
    const nestedMarkdown = [
      '# Nested Fences Test',
      '',
      '````markdown',
      'Here is some markdown that contains a code block:',
      '```typescript',
      'const inner = "hello";',
      'console.log(inner);',
      '```',
      '````',
      '',
      'Regular prose after fence.'
    ].join('\n')

    const { container } = renderQuietly(<MarkdownTextContent isRunning={false} text={nestedMarkdown} />)

    expect(isHugeTextFallback(container)).toBe(false)
    expect(await screen.findByRole('heading', { name: 'Nested Fences Test' })).toBeTruthy()
    expect(container.textContent).toContain('Regular prose after fence.')
  })

  it('renders unclosed code fences without crashing or triggering HugeTextFallback', async () => {
    const unclosedFence = [
      '# Unclosed Fence',
      '',
      'Some text before.',
      '```python',
      'def compute(x):',
      '    return x * 42',
      '# Note: fence is deliberately not closed'
    ].join('\n')

    const { container } = renderQuietly(<MarkdownTextContent isRunning={false} text={unclosedFence} />)

    expect(isHugeTextFallback(container)).toBe(false)
    expect(await screen.findByRole('heading', { name: 'Unclosed Fence' })).toBeTruthy()
    expect(container.textContent).toContain('def compute(x):')
  })

  it('renders multiple alternating unclosed fences without triggering HugeTextFallback', () => {
    const alternating = [
      '```ts',
      'const a = 1;',
      '```',
      '```python',
      'b = 2',
      '```bash',
      'echo test',
      '```',
      '```rust',
      'let c = 3;'
    ].join('\n')

    const { container } = renderQuietly(<MarkdownTextContent isRunning={false} text={alternating} />)

    expect(isHugeTextFallback(container)).toBe(false)
    expect(container.textContent).toContain('const a = 1;')
    expect(container.textContent).toContain('b = 2')
    expect(container.textContent).toContain('echo test')
    expect(container.textContent).toContain('let c = 3;')
  })

  it('renders pathological math blocks without crashing or triggering HugeTextFallback', async () => {
    const pathologicalMath = [
      '# Math Stress Test',
      '',
      'Unclosed display math: $$ \\sum_{n=1}^\\infty \\frac{1}{n^2}',
      '',
      'Complex math: $$ \\frac{\\frac{\\frac{a}{b}}{\\frac{c}{d}}}{\\frac{\\frac{e}{f}}{\\frac{g}{h}}} $$',
      '',
      'Malformed TeX: $$ \\begin{matrix} a & b \\\\ c $$',
      '',
      'Unclosed inline math: $x + y = z and some more text.',
      '',
      'Prose after math.'
    ].join('\n')

    const { container } = renderQuietly(<MarkdownTextContent isRunning={false} text={pathologicalMath} />)

    expect(isHugeTextFallback(container)).toBe(false)
    expect(await screen.findByRole('heading', { name: 'Math Stress Test' })).toBeTruthy()
    expect(container.textContent).toContain('Prose after math.')
  })

  it('renders pathological tables without crashing or triggering HugeTextFallback', async () => {
    // Generate a table with mismatched columns and unclosed pipes
    const tableLines = [
      '# Table Test',
      '',
      '| Header 1 | Header 2 | Header 3 | Header 4 |',
      '| --- | --- | --- | --- |'
    ]

    for (let i = 0; i < 50; i++) {
      tableLines.push(`| val_${i}_1 | val_${i}_2 | val_${i}_3 |`) // deliberate missing cell
    }

    tableLines.push('| unclosed row without trailing pipe')

    const { container } = renderQuietly(<MarkdownTextContent isRunning={false} text={tableLines.join('\n')} />)

    expect(isHugeTextFallback(container)).toBe(false)
    expect(await screen.findByRole('heading', { name: 'Table Test' })).toBeTruthy()
  })

  it('catastrophic stack overflow payload (deeply nested blockquotes) specifically triggers HugeTextFallback while preserving text readability', () => {
    // Deeply nested block structure recurses in mdast->hast, causing a genuine call stack overflow
    const deepBlockquote = `${'> '.repeat(10_000)}still readable`
    expect(deepBlockquote.length).toBeLessThan(200_000)

    const { container } = renderQuietly(<MarkdownTextContent isRunning={false} text={deepBlockquote} />)

    // It catches the RangeError in ErrorBoundary label="markdown-render" and renders HugeTextFallback
    expect(isHugeTextFallback(container)).toBe(true)
    // The text is preserved and readable
    expect(container.textContent).toContain('still readable')
  })
})
