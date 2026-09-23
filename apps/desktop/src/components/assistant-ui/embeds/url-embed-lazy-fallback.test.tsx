import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./youtube-embed', () => ({
  default: () => {
    throw new Error('Failed to fetch dynamically imported module: youtube-embed-hash.js')
  }
}))

vi.mock('./spotify-embed', () => ({
  default: () => {
    throw new Error('Failed to fetch dynamically imported module: spotify-embed-hash.js')
  }
}))

vi.mock('./social-embed', () => ({
  default: () => {
    throw new Error('Failed to fetch dynamically imported module: social-embed-hash.js')
  }
}))

vi.mock('./frame-embed', () => ({
  default: () => {
    throw new Error('Failed to fetch dynamically imported module: frame-embed-hash.js')
  }
}))

import { ErrorBoundary } from '@/components/error-boundary'
import { $embedAllowed, $embedMode } from '@/store/embed-consent'

import type { EmbedDescriptor } from './providers/types'
import { UrlEmbed } from './url-embed'

afterEach(cleanup)

function renderQuietly(node: Parameters<typeof render>[0]) {
  const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined)

  try {
    return render(node)
  } finally {
    spy.mockRestore()
  }
}

describe('UrlEmbed lazy chunk failure audit', () => {
  beforeEach(() => {
    $embedMode.set('always')
    $embedAllowed.set(['youtube', 'spotify', 'twitter', 'vimeo'])
  })

  it('youtube embed chunk failure renders EmbedFail without bubbling to outer boundary', async () => {
    const descriptor: EmbedDescriptor = {
      id: 'yt-123',
      label: 'YouTube Video',
      provider: 'youtube',
      renderer: 'frame',
      embedUrl: 'https://www.youtube-nocookie.com/embed/123',
      sourceUrl: 'https://youtube.com/watch?v=123'
    }

    const { container } = renderQuietly(
      <ErrorBoundary
        fallback={() => <div data-testid="outer-markdown-error">HugeTextFallback</div>}
        label="markdown-render"
      >
        <UrlEmbed descriptor={descriptor} />
      </ErrorBoundary>
    )

    await act(() => new Promise(resolve => setTimeout(resolve, 50)))

    expect(container.querySelector('[data-testid="outer-markdown-error"]')).toBeNull()
    expect(container.textContent).toContain('YouTube Video')
  })

  it('spotify embed chunk failure renders EmbedFail without bubbling to outer boundary', async () => {
    const descriptor: EmbedDescriptor = {
      id: 'sp-123',
      label: 'Spotify Track',
      provider: 'spotify',
      renderer: 'frame',
      embedUrl: 'https://open.spotify.com/embed/track/123',
      sourceUrl: 'https://open.spotify.com/track/123'
    }

    const { container } = renderQuietly(
      <ErrorBoundary
        fallback={() => <div data-testid="outer-markdown-error">HugeTextFallback</div>}
        label="markdown-render"
      >
        <UrlEmbed descriptor={descriptor} />
      </ErrorBoundary>
    )

    await act(() => new Promise(resolve => setTimeout(resolve, 50)))

    expect(container.querySelector('[data-testid="outer-markdown-error"]')).toBeNull()
    expect(container.textContent).toContain('Spotify Track')
  })

  it('social embed chunk failure renders EmbedFail without bubbling to outer boundary', async () => {
    const descriptor: EmbedDescriptor = {
      id: 'tw-123',
      label: 'X Post',
      provider: 'twitter',
      renderer: 'tweet',
      tweetId: '123',
      sourceUrl: 'https://x.com/user/status/123'
    }

    const { container } = renderQuietly(
      <ErrorBoundary
        fallback={() => <div data-testid="outer-markdown-error">HugeTextFallback</div>}
        label="markdown-render"
      >
        <UrlEmbed descriptor={descriptor} />
      </ErrorBoundary>
    )

    await act(() => new Promise(resolve => setTimeout(resolve, 50)))

    expect(container.querySelector('[data-testid="outer-markdown-error"]')).toBeNull()
    expect(container.textContent).toContain('X Post')
  })

  it('frame embed chunk failure renders EmbedFail without bubbling to outer boundary', async () => {
    const descriptor: EmbedDescriptor = {
      id: 'vm-123',
      label: 'Vimeo Video',
      provider: 'vimeo',
      renderer: 'frame',
      embedUrl: 'https://player.vimeo.com/video/123',
      sourceUrl: 'https://vimeo.com/123'
    }

    const { container } = renderQuietly(
      <ErrorBoundary
        fallback={() => <div data-testid="outer-markdown-error">HugeTextFallback</div>}
        label="markdown-render"
      >
        <UrlEmbed descriptor={descriptor} />
      </ErrorBoundary>
    )

    await act(() => new Promise(resolve => setTimeout(resolve, 50)))

    expect(container.querySelector('[data-testid="outer-markdown-error"]')).toBeNull()
    expect(container.textContent).toContain('Vimeo Video')
  })
})
