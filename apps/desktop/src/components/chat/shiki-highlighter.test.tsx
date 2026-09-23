import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { chunkByLines, exceedsHighlightBudget, LazyShiki, SyntaxHighlighter } from '@/components/chat/shiki-highlighter'
import { PlainShiki } from '@/components/chat/shiki-plain'

const components = {
  Pre: ({ children, className }: ComponentProps<'pre'>) => (
    <pre className={className} data-testid="streamdown-pre">
      {children}
    </pre>
  ),
  Code: ({ children, className }: ComponentProps<'code'>) => (
    <code className={className} data-testid="streamdown-code">
      {children}
    </code>
  )
}

let testScrollHeight = 400

class DynamicResizeObserver {
  constructor(private readonly callback: ResizeObserverCallback) {}

  observe(target: Element) {
    Object.defineProperty(target, 'scrollHeight', {
      configurable: true,
      get: () => testScrollHeight
    })
    this.callback([{ target } as ResizeObserverEntry], this as unknown as ResizeObserver)
  }

  unobserve() {}
  disconnect() {}
}

describe('exceedsHighlightBudget', () => {
  it('highlights normal-sized blocks', () => {
    expect(exceedsHighlightBudget('const x = 1\n'.repeat(100))).toBe(false)
  })

  it('skips highlighting past the line budget', () => {
    expect(exceedsHighlightBudget('x\n'.repeat(5_000))).toBe(true)
  })

  it('skips highlighting past the char budget on few lines', () => {
    expect(exceedsHighlightBudget('a'.repeat(200_000))).toBe(true)
  })
})

describe('chunkByLines', () => {
  it('keeps a small block as a single chunk', () => {
    const code = 'a\nb\nc'
    expect(chunkByLines(code, 200)).toEqual([{ text: code, lines: 3 }])
  })

  it('splits a large block and reconstructs it losslessly', () => {
    const code = Array.from({ length: 1000 }, (_, i) => `line ${i}`).join('\n')
    const chunks = chunkByLines(code, 200)

    expect(chunks).toHaveLength(5)
    expect(chunks.map(chunk => chunk.text).join('\n')).toBe(code)
    expect(chunks.reduce((sum, chunk) => sum + chunk.lines, 0)).toBe(1000)
  })

  it('handles empty strings cleanly', () => {
    expect(chunkByLines('', 200)).toEqual([{ text: '', lines: 1 }])
  })

  it('preserves trailing newlines across chunk boundaries', () => {
    const code = 'line1\nline2\nline3\n'
    const chunks = chunkByLines(code, 2)

    expect(chunks).toHaveLength(2)
    expect(chunks[0]).toEqual({ text: 'line1\nline2', lines: 2 })
    expect(chunks[1]).toEqual({ text: 'line3\n', lines: 2 })
    expect(chunks.map(c => c.text).join('\n')).toBe(code)
  })

  it('handles exact chunk multiples without trailing empty chunk', () => {
    const code = Array.from({ length: 400 }, (_, i) => `line ${i}`).join('\n')
    const chunks = chunkByLines(code, 200)

    expect(chunks).toHaveLength(2)
    expect(chunks[0].lines).toBe(200)
    expect(chunks[1].lines).toBe(200)
  })
})

describe('SyntaxHighlighter under fallback & degradation', () => {
  let writeTextMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    writeTextMock = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', {
      clipboard: { writeText: writeTextMock }
    })
    vi.stubGlobal('ResizeObserver', DynamicResizeObserver)
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  describe('CopyButton functionality', () => {
    it('receives the full unescaped code under plain/defer mode', async () => {
      const code = 'echo "hello from bash"\nls -la\n<script>alert("xss")</script>'
      render(<SyntaxHighlighter code={code} components={components} defer={true} language="bash" />)

      const copyButton = screen.getByRole('button', { name: /copy/i })
      expect(copyButton).toBeDefined()

      fireEvent.click(copyButton)
      expect(writeTextMock).toHaveBeenCalledWith(code)
    })

    it('receives the full code even when budget is exceeded', async () => {
      const largeCode = 'const x = 1;\n'.repeat(3_500)
      render(<SyntaxHighlighter code={largeCode} components={components} language="javascript" />)

      const copyButton = screen.getByRole('button', { name: /copy/i })
      fireEvent.click(copyButton)
      expect(writeTextMock).toHaveBeenCalledWith(largeCode)
    })

    it('returns null when code is empty or whitespace-only', () => {
      const { container: c1 } = render(<SyntaxHighlighter code="" components={components} language="javascript" />)

      expect(c1.innerHTML).toBe('')

      const { container: c2 } = render(
        <SyntaxHighlighter code={'   \n\t   '} components={components} language="javascript" />
      )

      expect(c2.innerHTML).toBe('')
    })
  })

  describe('Language badge / header design verification', () => {
    it('does not render a language badge or header row by intentional design', () => {
      const code = 'console.log("hello");'

      const { container } = render(
        <SyntaxHighlighter code={code} components={components} defer={true} language="javascript" />
      )

      // CodeCard is background-only — no header row, no language label
      expect(container.querySelector('[data-slot="code-card"]')).toBeDefined()
      expect(container.textContent).not.toContain('javascript')
      expect(container.querySelector('[data-slot="code-card-header"]')).toBeNull()
      expect(container.querySelector('.language-badge')).toBeNull()
    })
  })

  describe('ExpandableBlock behavior with tall and wide blocks', () => {
    it('renders expand/collapse toggle for blocks exceeding 121px height (>35 lines)', () => {
      const tallCode = Array.from({ length: 40 }, (_, i) => `console.log(${i});`).join('\n')

      const { container } = render(
        <SyntaxHighlighter code={tallCode} components={components} defer={true} language="javascript" />
      )

      const toggle = screen.getByRole('button', { name: 'Expand' })
      expect(toggle.getAttribute('aria-expanded')).toBe('false')

      // Click to expand
      fireEvent.click(toggle)
      expect(screen.getByRole('button', { name: 'Collapse' }).getAttribute('aria-expanded')).toBe('true')

      const scroller = container.querySelector('.scrollbar-overlay')
      expect(scroller?.classList.contains('max-h-[40dvh]')).toBe(true)

      // Click to collapse
      fireEvent.click(screen.getByRole('button', { name: 'Collapse' }))
      expect(screen.getByRole('button', { name: 'Expand' }).getAttribute('aria-expanded')).toBe('false')
      expect(scroller?.classList.contains('max-h-[7.5rem]')).toBe(true)
    })

    it('does not render toggle if scrollHeight <= 121px (e.g. single 2000-char line that scrolls horizontally)', () => {
      testScrollHeight = 32

      const singleLineWide = 'a'.repeat(2000)
      render(<SyntaxHighlighter code={singleLineWide} components={components} defer={true} language="text" />)

      expect(screen.queryByRole('button', { name: 'Expand' })).toBeNull()
      expect(screen.queryByRole('button', { name: 'Collapse' })).toBeNull()
    })
  })

  describe('Theme variables and flash-free styling in PlainShiki', () => {
    it('uses transparent background on PlainShiki pre and inherits theme tokens', () => {
      const { container } = render(<PlainShiki code="const theme = 'system';" />)

      const pre = container.querySelector('pre.shiki') as HTMLElement
      expect(pre).not.toBeNull()
      expect(pre.style.backgroundColor).toBe('transparent')
      expect(pre.style.margin).toBe('0px')
      expect(container.querySelector('.rs-root')).not.toBeNull()
    })

    it('CodeCard and CodeCardBody apply CSS variable bindings without hardcoded colors', () => {
      const { container } = render(
        <SyntaxHighlighter code="const x = 42;" components={components} defer={true} language="typescript" />
      )

      const card = container.querySelector('[data-slot="code-card"]') as HTMLElement
      expect(card.className).toContain('bg-(--ui-bg-editor)')
      expect(card.className).toContain('[--expandable-fade-from:var(--ui-bg-editor)]')

      const body = container.querySelector('[data-slot="code-card-body"]') as HTMLElement
      expect(body.className).toContain('text-foreground/90')
    })
  })

  describe('Malformed, missing, or adversarial language tags', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined)

    afterEach(() => {
      errorSpy.mockClear()
    })

    it('handles empty string language="" without throwing', () => {
      expect(() => {
        render(<SyntaxHighlighter code="echo 123" components={components} defer={true} language="" />)
      }).not.toThrow()
      expect(screen.getByText('echo 123')).toBeDefined()
    })

    it('handles undefined language without throwing', () => {
      expect(() => {
        render(
          <SyntaxHighlighter
            code="echo 456"
            components={components}
            defer={true}
            language={undefined as unknown as string}
          />
        )
      }).not.toThrow()
      expect(screen.getByText('echo 456')).toBeDefined()
    })

    it('handles directory traversal language="../../../etc/passwd" safely', () => {
      expect(() => {
        render(
          <SyntaxHighlighter
            code="root:x:0:0:root:/root:/bin/bash"
            components={components}
            defer={true}
            language="../../../etc/passwd"
          />
        )
      }).not.toThrow()
      expect(screen.getByText('root:x:0:0:root:/root:/bin/bash')).toBeDefined()
    })

    it('handles newline injection language="bash\\nexec" safely', () => {
      expect(() => {
        render(<SyntaxHighlighter code="id" components={components} defer={true} language="bash\nexec" />)
      }).not.toThrow()
      expect(screen.getByText('id')).toBeDefined()
    })

    it('handles prototype pollution language tags like "toString" safely in LazyShiki', () => {
      expect(() => {
        render(<LazyShiki code="foo bar baz" language="toString" />)
      }).not.toThrow()
      expect(screen.getByText('foo bar baz')).toBeDefined()
    })
  })
})
