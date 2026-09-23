import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

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

describe('LazyShiki error boundary adversarial audit & regression coverage', () => {
  let reportRendererErrorMock: ReturnType<typeof vi.fn>
  let consoleErrorSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    reportRendererErrorMock = vi.fn()
    window.hermesDesktop = {
      ...(window.hermesDesktop || {}),
      reportRendererError: reportRendererErrorMock
    } as unknown as typeof window.hermesDesktop
    consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined)
  })

  afterEach(() => {
    cleanup()
    consoleErrorSpy.mockRestore()
  })

  it('falls back to PlainShiki instead of bubbling error to markdown-render boundary', async () => {
    const code = 'echo "hello world"'

    let renderResult: ReturnType<typeof render>
    await act(async () => {
      renderResult = render(
        <ErrorBoundary fallback={() => <div data-testid="outer-fallback">crashed</div>} label="markdown-render">
          <LazyShiki code={code} language="bash" />
        </ErrorBoundary>
      )
    })

    expect(renderResult!.container.querySelector('[data-testid="outer-fallback"]')).toBeNull()
    expect(renderResult!.container.textContent).toContain('echo "hello world"')
    expect(reportRendererErrorMock).toHaveBeenCalledTimes(1)
  })

  it('invokes reportRendererError exactly once when 500 tokens stream into a single code block', async () => {
    let currentCode = 'const x = 0;'
    let renderResult: ReturnType<typeof render>

    await act(async () => {
      renderResult = render(<LazyShiki code={currentCode} language="typescript" />)
    })

    // Initial render caught the error and called reportRendererError exactly once
    expect(reportRendererErrorMock).toHaveBeenCalledTimes(1)
    expect(reportRendererErrorMock).toHaveBeenLastCalledWith(
      expect.objectContaining({
        boundary: 'shiki-block',
        message: expect.stringContaining('Failed to fetch dynamically imported module')
      })
    )

    // Stream 500 tokens into the code block
    for (let i = 1; i <= 500; i++) {
      currentCode += `\nconst token_${i} = ${i};`
      await act(async () => {
        renderResult.rerender(<LazyShiki code={currentCode} language="typescript" />)
      })
    }

    // Must STILL be called exactly once; does NOT flood logs on streaming updates
    expect(reportRendererErrorMock).toHaveBeenCalledTimes(1)
    expect(renderResult!.container.textContent).toContain('const token_500 = 500;')
  })

  it('ensures every token update during rapid streaming renders into the DOM without freezing or latching stale code', async () => {
    const tokens = [
      'function ',
      'streamExample',
      '(a, ',
      'b) ',
      '{\n',
      '  const sum = ',
      'a + b;\n',
      '  return ',
      'sum;\n',
      '}'
    ]

    let currentCode = ''
    let renderResult: ReturnType<typeof render>

    await act(async () => {
      renderResult = render(<LazyShiki code={currentCode} language="typescript" />)
    })

    const codeNode = renderResult!.container.querySelector('code')
    expect(codeNode).not.toBeNull()

    // Stream token-by-token and assert that every single update lands in the DOM
    for (const token of tokens) {
      currentCode += token
      await act(async () => {
        renderResult.rerender(<LazyShiki code={currentCode} language="typescript" />)
      })

      expect(renderResult!.container.querySelector('code')?.textContent).toBe(currentCode)
    }

    // Additional 50 rapid token updates with DOM verification at every step
    for (let i = 1; i <= 50; i++) {
      currentCode += `\n// token ${i}`
      await act(async () => {
        renderResult.rerender(<LazyShiki code={currentCode} language="typescript" />)
      })
      expect(renderResult!.container.querySelector('code')?.textContent).toBe(currentCode)
    }

    // DOM node is reconciled in-place without remount churn
    expect(renderResult!.container.querySelector('code')).toBe(codeNode)
    // Error boundary reported the failure exactly once and never on token streaming
    expect(reportRendererErrorMock).toHaveBeenCalledTimes(1)
  })

  it('invokes reportRendererError once per distinct code block on screen (20 code blocks = 20 calls)', async () => {
    const blocks = Array.from({ length: 20 }, (_, i) => `// block ${i}\nconsole.log(${i});`)

    let renderResult: ReturnType<typeof render>
    await act(async () => {
      renderResult = render(
        <div>
          {blocks.map((code, idx) => (
            <LazyShiki code={code} key={idx} language="javascript" />
          ))}
        </div>
      )
    })

    // Each code block has its own ErrorBoundary instance, so each catches once
    expect(reportRendererErrorMock).toHaveBeenCalledTimes(20)

    // Now re-render all 20 blocks with new streaming code updates across all blocks
    const updatedBlocks = blocks.map(code => `${code}\n// appended`)
    await act(async () => {
      renderResult.rerender(
        <div>
          {updatedBlocks.map((code, idx) => (
            <LazyShiki code={code} key={idx} language="javascript" />
          ))}
        </div>
      )
    })

    // Invocation count must NOT increase on re-render; stays at 20
    expect(reportRendererErrorMock).toHaveBeenCalledTimes(20)
  })
})
