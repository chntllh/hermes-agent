import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { PRIMARY_SESSION_VIEW, type SessionView, SessionViewProvider } from '@/app/chat/session-view'
import { $subagentsBySession } from '@/store/subagents'
import { openSessionInNewWindow } from '@/store/windows'
import type * as WindowsStore from '@/store/windows'

import { DelegateTool } from './delegate'

vi.mock('@/store/windows', async importOriginal => {
  const actual = await importOriginal<typeof WindowsStore>()

  return {
    ...actual,
    openSessionInNewWindow: vi.fn()
  }
})

function mockView(connectionId: string | null = null): SessionView {
  return {
    ...PRIMARY_SESSION_VIEW,
    $connectionId: atom(connectionId),
    $runtimeId: atom('parent-session-1')
  }
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  $subagentsBySession.set({})
})

describe('DelegateTool', () => {
  it('passes parent session connectionId from useSessionView to openSessionInNewWindow (#120213)', () => {
    $subagentsBySession.set({
      'parent-session-1': [
        {
          activity: [],
          goal: 'Inspect worker',
          id: 'sub-1',
          sessionId: 'child-session-123',
          status: 'running',
          stream: []
        } as never
      ]
    })

    render(
      <SessionViewProvider value={mockView('remote-rig')}>
        <DelegateTool args={{ tasks: [{ goal: 'Inspect worker' }] }} toolCallId="call-1" />
      </SessionViewProvider>
    )

    const button = screen.getByRole('button', { name: 'Inspect worker' })
    fireEvent.click(button)

    expect(openSessionInNewWindow).toHaveBeenCalledWith('child-session-123', {
      connectionId: 'remote-rig',
      watch: true
    })
  })

  it('passes undefined connectionId when parent session is local (#120213)', () => {
    $subagentsBySession.set({
      'parent-session-1': [
        {
          activity: [],
          goal: 'Inspect worker',
          id: 'sub-1',
          sessionId: 'child-session-456',
          status: 'running',
          stream: []
        } as never
      ]
    })

    render(
      <SessionViewProvider value={mockView(null)}>
        <DelegateTool args={{ tasks: [{ goal: 'Inspect worker' }] }} toolCallId="call-1" />
      </SessionViewProvider>
    )

    const button = screen.getByRole('button', { name: 'Inspect worker' })
    fireEvent.click(button)

    expect(openSessionInNewWindow).toHaveBeenCalledWith('child-session-456', {
      connectionId: undefined,
      watch: true
    })
  })
})
